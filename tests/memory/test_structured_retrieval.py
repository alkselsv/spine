from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.diagnostics import DiagnosticContext
from spine.application.telemetry import (
    OperationalComponent,
    OperationalObservationEmitter,
    OperationalObservationPolicy,
    OperationalObservationRegistry,
    RecordingOperationalObservationSink,
)

from spine.memory.contracts import (
    EvidenceReference,
    ProjectionChunk,
    ProjectionOutcome,
    ProjectionRetrievalResult,
    RetrievalScore,
    StableLocator,
)
from spine.memory.read_plan import (
    PhysicalReadPlan,
    PhysicalReadPlanBuilder,
    PhysicalReadPlanResolution,
)
from spine.memory.retrieval import PhysicalReadPlanExecutor

from test_physical_read_plan import _context


def _evidence() -> EvidenceReference:
    return EvidenceReference(
        evidence_id="evidence-1",
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        source_object_id=UUID(int=43),
        source_revision_id=UUID(int=44),
        locator=StableLocator(kind="page", value="1"),
        evidence_digest="f" * 64,
    )


@dataclass
class _Provider:
    calls: list[object] = field(default_factory=list)

    async def retrieve(self, command: object) -> ProjectionRetrievalResult:
        self.calls.append(command)
        partition = command.read_context.projection_partition_id  # type: ignore[attr-defined]
        return ProjectionRetrievalResult(
            outcome=ProjectionOutcome.SUCCEEDED,
            command_identity=command.command_identity,  # type: ignore[attr-defined]
            command_digest=command.command_digest,  # type: ignore[attr-defined]
            chunks=(
                ProjectionChunk(
                    chunk_id=f"chunk-{partition}",
                    text="same evidence",
                    evidence=(_evidence(),),
                    score=RetrievalScore(
                        value=0.5,
                        metric="cosine",
                        direction="higher_is_better",
                        provider_contract_version="fake:v1",
                    ),
                ),
            ),
        )


@dataclass
class _PartialProvider(_Provider):
    async def retrieve(self, command: object) -> ProjectionRetrievalResult:
        if len(self.calls) == 1:
            self.calls.append(command)
            raise RuntimeError("synthetic provider failure")
        return await super().retrieve(command)


@dataclass
class _DuplicateProvider(_Provider):
    async def retrieve(self, command: object) -> ProjectionRetrievalResult:
        self.calls.append(command)
        return ProjectionRetrievalResult(
            outcome=ProjectionOutcome.SUCCEEDED,
            command_identity=command.command_identity,  # type: ignore[attr-defined]
            command_digest=command.command_digest,  # type: ignore[attr-defined]
            chunks=(
                ProjectionChunk(
                    chunk_id="same",
                    text="same evidence" if len(self.calls) == 1 else "different evidence",
                    evidence=(_evidence(),),
                ),
            ),
        )


@dataclass
class _CancellingProvider(_Provider):
    async def retrieve(self, command: object) -> ProjectionRetrievalResult:
        self.calls.append(command)
        raise asyncio.CancelledError


def _two_partition_plan() -> PhysicalReadPlan:
    context, registry = _context()
    resolution = PhysicalReadPlanBuilder(namespace_registry=registry).build(context)
    assert resolution.plan is not None
    operation = resolution.plan.operations[0]
    second = operation.model_copy(
        update={
            "projection_partition": operation.projection_partition.model_copy(
                update={
                    "projection_partition_id": UUID(int=50),
                    "partition_identity": "secondary",
                }
            ),
            "projection_snapshot_id": operation.projection_snapshot_id,
            "physical_namespace": operation.physical_namespace.model_copy(
                update={"namespace_digest": "e" * 64}
            ),
            "source_object_ids": operation.source_object_ids,
            "source_revision_ids": operation.source_revision_ids,
        }
    )
    return resolution.plan.model_copy(update={"operations": (operation, second)})


@pytest.mark.asyncio
async def test_executor_fans_out_and_merges_structured_results_deterministically() -> None:
    provider = _Provider()
    result = await PhysicalReadPlanExecutor(provider).execute(_two_partition_plan())

    assert result.outcome is ProjectionOutcome.SUCCEEDED
    assert len(provider.calls) == 2
    assert [item.chunk_id for item in result.chunks] == [
        "chunk-00000000-0000-0000-0000-000000000028",
        "chunk-00000000-0000-0000-0000-000000000032",
    ]
    assert all(item.read_context is not None for item in result.chunks)
    assert all(item.score is not None for item in result.chunks)
    assert len(result.read_contexts) == 2


@pytest.mark.asyncio
async def test_executor_emits_retrieval_observation_with_trusted_trace() -> None:
    registry = OperationalObservationRegistry()
    sink = RecordingOperationalObservationSink(registry)
    observer = OperationalObservationEmitter(
        registry=registry,
        sink=sink,
        policy=OperationalObservationPolicy(),
        clock=lambda: datetime(2026, 10, 16, tzinfo=timezone.utc),
        observation_id_source=lambda: UUID(int=361),
    )
    executor = PhysicalReadPlanExecutor(
        _Provider(),
        operational_observer=observer,
    )
    diagnostic_context = DiagnosticContext(trace_id=UUID(int=1000))

    await executor.execute(
        _two_partition_plan(),
        diagnostic_context=diagnostic_context,
    )

    assert len(sink.observations) == 1
    assert sink.observations[0].component is OperationalComponent.RETRIEVAL
    assert sink.observations[0].trace_id == diagnostic_context.trace_id


@pytest.mark.asyncio
async def test_executor_rejects_unsafe_resolution_without_provider_call() -> None:
    context, registry = _context()
    unsafe_context = context.model_copy(
        update={
            "source_isolation": context.source_isolation.model_copy(
                update={"pre_retrieval_enforced": False}
            )
        }
    )
    resolution: PhysicalReadPlanResolution = PhysicalReadPlanBuilder(
        namespace_registry=registry
    ).build(unsafe_context)
    provider = _Provider()
    observation_registry = OperationalObservationRegistry()
    observation_sink = RecordingOperationalObservationSink(observation_registry)
    observer = OperationalObservationEmitter(
        registry=observation_registry,
        sink=observation_sink,
        policy=OperationalObservationPolicy(),
        clock=lambda: datetime(2026, 10, 16, tzinfo=timezone.utc),
        observation_id_source=lambda: UUID(int=362),
    )

    result = await PhysicalReadPlanExecutor(
        provider, operational_observer=observer
    ).execute(resolution)

    assert result.outcome is ProjectionOutcome.FAILED
    assert provider.calls == []
    assert result.failure is not None
    assert result.failure.code.value == "source_isolation_unproven"
    assert len(observation_sink.observations) == 1
    assert observation_sink.observations[0].trace_id == context.diagnostic_context.trace_id


@pytest.mark.asyncio
async def test_executor_rejects_substituted_diagnostic_context_before_provider_read() -> None:
    provider = _Provider()
    executor = PhysicalReadPlanExecutor(
        provider,
        operational_observer=OperationalObservationEmitter(
            registry=OperationalObservationRegistry(),
            sink=RecordingOperationalObservationSink(
                OperationalObservationRegistry()
            ),
            policy=OperationalObservationPolicy(),
            clock=lambda: datetime(2026, 10, 16, tzinfo=timezone.utc),
            observation_id_source=lambda: UUID(int=363),
        ),
    )

    with pytest.raises(ValueError, match="substituted"):
        await executor.execute(
            _two_partition_plan(),
            diagnostic_context=DiagnosticContext(trace_id=UUID(int=999)),
        )

    assert provider.calls == []


@pytest.mark.asyncio
async def test_executor_reports_partial_provider_failure_without_failed_lane_content() -> None:
    provider = _PartialProvider()

    executor = PhysicalReadPlanExecutor(provider)
    plan = _two_partition_plan()
    result = await executor.execute(plan)

    assert result.outcome is ProjectionOutcome.PARTIAL
    assert len(result.chunks) == 1
    assert len(result.partial_failures) == 1
    assert result.failure is not None
    assert result.failure.code.value == "partial_failure"

    retry = await executor.execute(plan)

    assert retry.outcome is ProjectionOutcome.SUCCEEDED
    assert len(provider.calls) == 4


@pytest.mark.asyncio
async def test_executor_keeps_incompatible_duplicate_provenance_as_separate_items() -> None:
    provider = _DuplicateProvider()

    result = await PhysicalReadPlanExecutor(provider).execute(_two_partition_plan())

    assert [item.text for item in result.chunks] == [
        "same evidence",
        "different evidence",
    ]
    assert all(len(item.read_contexts) == 1 for item in result.chunks)


@pytest.mark.asyncio
async def test_executor_propagates_cancellation_before_starting_next_partition() -> None:
    provider = _CancellingProvider()

    with pytest.raises(asyncio.CancelledError):
        await PhysicalReadPlanExecutor(provider).execute(_two_partition_plan())

    assert len(provider.calls) == 1
