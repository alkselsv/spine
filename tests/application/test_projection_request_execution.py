from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from spine.application.diagnostics import DiagnosticContext, DiagnosticEventRegistry
from spine.application.document_ingestion import (
    PROJECTION_REQUEST_EVENT_TYPE,
    PROJECTION_REQUEST_SCHEMA_VERSION,
    ProjectionRequestPayload,
)
from spine.application.outbox import (
    RetryableOutboxConsumerFailure,
    TerminalOutboxConsumerFailure,
)
from spine.application.persistence.outbox import (
    OpaqueObjectReference,
    OutboxEventRegistry,
    OutboxIntent,
)
from spine.application.persistence.unit_of_work_activity import (
    track_unit_of_work_activity,
)
from spine.application.projection_request_execution import (
    CanonicalProjectionRequestContext,
    ProjectionRequestConsumer,
    ProjectionRequestEnvelope,
)
from spine.application.projection_result_mapping import (
    AdapterProjectionReceipt,
    ProjectionMappingConflictError,
    ProjectionMappingError,
    ProjectionResultMappingBridge,
)
from spine.application.telemetry import (
    OperationalComponent,
    OperationalObservationEmitter,
    OperationalObservationPolicy,
    OperationalObservationRegistry,
    RecordingOperationalObservationSink,
)
from spine.application.testing import DeterministicCanonicalProjectionGateway
from spine.domain.projections import ProjectionCandidateState
from spine.infrastructure.diagnostics import RecordingDiagnosticSink
from spine.memory.contracts import ProjectCommand, ProjectionResult
from spine.memory.fake import (
    DeterministicContextProjection,
    DeterministicPhysicalStore,
)
from spine.workers import OutboxConsumerBinding, create_outbox_worker
from tests.contracts.persistence.conftest import create_in_memory_adapter
from tests.contracts.persistence.ids import synthetic_uuid
from tests.contracts.persistence.test_idempotency_repository_contract import (
    persist_environment,
    persist_workspace,
)

from tests.application.test_projection_result_mapping import (
    ENVIRONMENT,
    NOW,
    PROCESSING_RECEIPT,
    REVISION,
    RUN,
    WORKSPACE,
    _adapter_receipt,
    _command,
    _context,
)


class _CanonicalResolver:
    def __init__(self, command: ProjectCommand) -> None:
        self.command = command
        self.requests: list[ProjectionRequestEnvelope] = []

    async def resolve(
        self,
        request: ProjectionRequestEnvelope,
    ) -> CanonicalProjectionRequestContext:
        self.requests.append(request)
        return _canonical_context(self.command)


class _OpaqueAfterResolutionResolver(_CanonicalResolver):
    async def revalidate_before_physical_projection(self, request, command):
        del request, command
        raise ProjectionMappingError("opaque current acceptance")


class _AdapterReceiptBuilder:
    async def build(
        self,
        command: ProjectCommand,
        result: ProjectionResult,
    ) -> AdapterProjectionReceipt:
        return _adapter_receipt(command).model_copy(update={"result": result})


class _FailOnceAdapterReceiptBuilder(_AdapterReceiptBuilder):
    def __init__(self) -> None:
        self.attempts = 0

    async def build(
        self,
        command: ProjectCommand,
        result: ProjectionResult,
    ) -> AdapterProjectionReceipt:
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("protected document text")
        return await super().build(command, result)


class _FailAfterPhysicalProjection:
    def __init__(self, projection: DeterministicContextProjection) -> None:
        self.projection = projection
        self.attempts = 0

    async def project(self, command: ProjectCommand) -> ProjectionResult:
        result = await self.projection.project(command)
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("protected provider detail")
        return result


class _ConflictingPublicationCompletion:
    async def complete(self, mapping: object) -> None:
        del mapping
        raise ProjectionMappingConflictError("protected document text")


def _canonical_context(command: ProjectCommand) -> CanonicalProjectionRequestContext:
    assert len(command.source_revisions) == 1
    return CanonicalProjectionRequestContext(
        workspace_id=command.workspace_id,
        environment_id=command.environment_id,
        projection_run_id=command.projection_run_id,
        projection_candidate=command.projection_candidate,
        projection_config=command.projection_config,
        canonical_boundary=command.canonical_boundary,
        source_revisions=command.source_revisions,
        physical_namespace=command.physical_namespace,
        command_identity=command.command_identity,
        command_digest=command.command_digest,
    )


def _intent(*, trace_id: UUID = UUID(int=103)) -> OutboxIntent:
    registry = OutboxEventRegistry()
    registry.register(
        event_type=PROJECTION_REQUEST_EVENT_TYPE,
        schema_version=PROJECTION_REQUEST_SCHEMA_VERSION,
        payload_type=ProjectionRequestPayload,
    )
    event_id = UUID(int=101)
    payload = ProjectionRequestPayload(
        ingestion_run_id=RUN,
        ingestion_run_source_id=UUID(int=102),
        source_revision_id=REVISION,
        processing_receipt_id=PROCESSING_RECEIPT,
        processor_kind="document-ingestion",
        parser_version="parser:v1",
        parser_config_reference="parser-default:v1",
        locator_contract_version="locator:v1",
    )
    return registry.build_intent(
        event_id=event_id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        event_type=PROJECTION_REQUEST_EVENT_TYPE,
        schema_version=PROJECTION_REQUEST_SCHEMA_VERSION,
        payload=payload,
        aggregate=OpaqueObjectReference(
            object_type="ingestion_run_source",
            object_id=payload.ingestion_run_source_id,
            schema_version=1,
        ),
        producer_deduplication_id=(
            f"projection-request:{payload.ingestion_run_source_id}"
        ),
        trace_id=trace_id,
        correlation_id=RUN,
        causation_id=UUID(int=104),
    )


@pytest.mark.asyncio
async def test_committed_projection_request_records_canonical_handoff() -> None:
    command = _command()
    resolver = _CanonicalResolver(command)
    projection = DeterministicContextProjection()
    gateway = DeterministicCanonicalProjectionGateway((_context(command),))
    consumer = ProjectionRequestConsumer(
        canonical_resolver=resolver,
        projection=projection,
        receipt_builder=_AdapterReceiptBuilder(),
        result_bridge=ProjectionResultMappingBridge(
            gateway,
            id_source=lambda: UUID(int=105),
        ),
    )
    intent = _intent()
    diagnostic_context = DiagnosticContext(
        trace_id=intent.trace_id,
        correlation_id=intent.correlation_id,
        causation_id=intent.event_id,
    )

    outcome = await consumer.consume(intent, diagnostic_context)
    replay = await gateway.replay_for(command)

    assert outcome.diagnostic_context == diagnostic_context
    assert resolver.requests == [
        ProjectionRequestEnvelope(
            event_id=UUID(int=101),
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            payload=intent.payload,
        )
    ]
    assert projection.recorded_commands == (command,)
    assert replay is not None
    assert replay.handoff.command == command
    assert replay.handoff.receipt_command.receipt.workspace_id == WORKSPACE
    assert replay.handoff.receipt_command.receipt.environment_id == ENVIRONMENT
    assert replay.handoff.receipt_command.receipt.projection_run_id == RUN
    assert (
        replay.handoff.receipt_command.receipt.command_identity
        == command.command_identity
    )
    assert replay.handoff.receipt_command.receipt.command_digest == command.command_digest


@pytest.mark.asyncio
async def test_current_turning_opaque_after_resolution_never_reaches_projection_adapter() -> None:
    command = _command()
    projection = DeterministicContextProjection()
    consumer = ProjectionRequestConsumer(
        canonical_resolver=_OpaqueAfterResolutionResolver(command),
        projection=projection,
        receipt_builder=_AdapterReceiptBuilder(),
        result_bridge=ProjectionResultMappingBridge(
            DeterministicCanonicalProjectionGateway((_context(command),)),
            id_source=lambda: UUID(int=105),
        ),
    )
    intent = _intent()

    with pytest.raises(
        TerminalOutboxConsumerFailure,
        match="projection.request.canonical_rejected",
    ):
        await consumer.consume(
            intent,
            DiagnosticContext(
                trace_id=intent.trace_id,
                correlation_id=intent.correlation_id,
                causation_id=intent.event_id,
            ),
        )

    assert projection.recorded_commands == ()


@pytest.mark.asyncio
async def test_projection_request_emits_safe_observation_with_worker_trace() -> None:
    command = _command()
    registry = OperationalObservationRegistry()
    sink = RecordingOperationalObservationSink(registry)
    observer = OperationalObservationEmitter(
        registry=registry,
        sink=sink,
        policy=OperationalObservationPolicy(),
        clock=lambda: NOW,
        observation_id_source=lambda: UUID(int=361),
    )
    consumer = ProjectionRequestConsumer(
        canonical_resolver=_CanonicalResolver(command),
        projection=DeterministicContextProjection(),
        receipt_builder=_AdapterReceiptBuilder(),
        result_bridge=ProjectionResultMappingBridge(
            DeterministicCanonicalProjectionGateway((_context(command),)),
            id_source=lambda: UUID(int=362),
        ),
        operational_observer=observer,
    )
    intent = _intent()
    diagnostic_context = DiagnosticContext(
        trace_id=intent.trace_id,
        correlation_id=intent.correlation_id,
        causation_id=intent.event_id,
    )

    await consumer.consume(intent, diagnostic_context)

    assert len(sink.observations) == 1
    observation = sink.observations[0]
    assert observation.component is OperationalComponent.PROJECTION
    assert observation.trace_id == intent.trace_id
    assert observation.correlation_id == intent.correlation_id
    assert observation.causation_id == intent.event_id
    assert observation.workspace_id == intent.workspace_id
    assert observation.environment_id == intent.environment_id


@pytest.mark.asyncio
async def test_duplicate_projection_request_replays_one_logical_handoff() -> None:
    command = _command()
    projection = DeterministicContextProjection()
    gateway = DeterministicCanonicalProjectionGateway((_context(command),))
    consumer = ProjectionRequestConsumer(
        canonical_resolver=_CanonicalResolver(command),
        projection=projection,
        receipt_builder=_AdapterReceiptBuilder(),
        result_bridge=ProjectionResultMappingBridge(
            gateway,
            id_source=lambda: UUID(int=106),
        ),
    )
    intent = _intent()
    diagnostic_context = DiagnosticContext(
        trace_id=intent.trace_id,
        correlation_id=intent.correlation_id,
        causation_id=intent.event_id,
    )

    await consumer.consume(intent, diagnostic_context)
    await consumer.consume(intent, diagnostic_context)

    assert projection.recorded_commands == (command,)
    replay = await gateway.replay_for(command)
    assert replay is not None
    assert replay.replay is True


@pytest.mark.asyncio
async def test_partial_handoff_failure_retries_without_duplicate_physical_effect() -> None:
    command = _command()
    projection = DeterministicContextProjection()
    gateway = DeterministicCanonicalProjectionGateway((_context(command),))
    receipt_builder = _FailOnceAdapterReceiptBuilder()
    consumer = ProjectionRequestConsumer(
        canonical_resolver=_CanonicalResolver(command),
        projection=projection,
        receipt_builder=receipt_builder,
        result_bridge=ProjectionResultMappingBridge(
            gateway,
            id_source=lambda: UUID(int=107),
        ),
    )
    intent = _intent()
    diagnostic_context = DiagnosticContext(
        trace_id=intent.trace_id,
        correlation_id=intent.correlation_id,
        causation_id=intent.event_id,
    )

    with pytest.raises(RetryableOutboxConsumerFailure) as captured:
        await consumer.consume(intent, diagnostic_context)
    await consumer.consume(intent, diagnostic_context)

    assert captured.value.code == "projection.request.handoff_retryable"
    assert receipt_builder.attempts == 2
    assert projection.recorded_commands == (command,)
    replay = await gateway.replay_for(command)
    assert replay is not None


@pytest.mark.asyncio
async def test_same_command_identity_with_changed_digest_is_integrity_conflict(
    caplog: pytest.LogCaptureFixture,
) -> None:
    command = _command()
    resolver = _CanonicalResolver(command)
    projection = DeterministicContextProjection()
    gateway = DeterministicCanonicalProjectionGateway((_context(command),))
    consumer = ProjectionRequestConsumer(
        canonical_resolver=resolver,
        projection=projection,
        receipt_builder=_AdapterReceiptBuilder(),
        result_bridge=ProjectionResultMappingBridge(
            gateway,
            id_source=lambda: UUID(int=108),
        ),
    )
    intent = _intent()
    diagnostic_context = DiagnosticContext(
        trace_id=intent.trace_id,
        correlation_id=intent.correlation_id,
        causation_id=intent.event_id,
    )
    await consumer.consume(intent, diagnostic_context)
    resolver.command = _command(digest="d" * 64)

    with pytest.raises(TerminalOutboxConsumerFailure) as captured:
        await consumer.consume(intent, diagnostic_context)

    assert captured.value.code == "projection.request.integrity_conflict"
    assert "protected text" not in str(captured.value)
    replay = await gateway.replay_for(command)
    assert replay is not None
    assert replay.handoff.command.command_digest == command.command_digest
    assert "Projection request integrity conflict at preparation" in caplog.text
    assert "protected text" not in caplog.text


@pytest.mark.asyncio
async def test_publication_conflict_logs_only_content_free_stage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    command = _command()
    consumer = ProjectionRequestConsumer(
        canonical_resolver=_CanonicalResolver(command),
        projection=DeterministicContextProjection(),
        receipt_builder=_AdapterReceiptBuilder(),
        result_bridge=ProjectionResultMappingBridge(
            DeterministicCanonicalProjectionGateway((_context(command),)),
            id_source=lambda: UUID(int=118),
        ),
        completion=_ConflictingPublicationCompletion(),
    )
    intent = _intent()

    with pytest.raises(TerminalOutboxConsumerFailure) as captured:
        await consumer.consume(
            intent,
            DiagnosticContext(
                trace_id=intent.trace_id,
                correlation_id=intent.correlation_id,
                causation_id=intent.event_id,
            ),
        )

    assert captured.value.code == "projection.request.integrity_conflict"
    assert "Projection request integrity conflict at publication_completion" in caplog.text
    assert "protected document text" not in caplog.text


@pytest.mark.asyncio
async def test_partial_physical_failure_resumes_same_command_before_handoff() -> None:
    command = _command()
    physical_store = DeterministicPhysicalStore(
        fail_once_for={f"{command.command_identity}:1"}
    )
    projection = DeterministicContextProjection(physical_store=physical_store)
    gateway = DeterministicCanonicalProjectionGateway((_context(command),))
    consumer = ProjectionRequestConsumer(
        canonical_resolver=_CanonicalResolver(command),
        projection=projection,
        receipt_builder=_AdapterReceiptBuilder(),
        result_bridge=ProjectionResultMappingBridge(
            gateway,
            id_source=lambda: UUID(int=109),
        ),
    )
    intent = _intent()
    diagnostic_context = DiagnosticContext(
        trace_id=intent.trace_id,
        correlation_id=intent.correlation_id,
        causation_id=intent.event_id,
    )

    with pytest.raises(RetryableOutboxConsumerFailure) as captured:
        await consumer.consume(intent, diagnostic_context)
    await consumer.consume(intent, diagnostic_context)

    assert captured.value.code == "projection.request.physical_retryable"
    assert physical_store.attempts_for(f"{command.command_identity}:1") == 2
    assert projection.recorded_commands == (command,)
    replay = await gateway.replay_for(command)
    assert replay is not None


@pytest.mark.asyncio
async def test_open_canonical_unit_of_work_blocks_physical_projection() -> None:
    command = _command()
    projection = DeterministicContextProjection()

    class LeakingResolver(_CanonicalResolver):
        async def resolve(self, request: ProjectionRequestEnvelope):
            context = track_unit_of_work_activity()
            context.__enter__()
            self.context = context
            return await super().resolve(request)

    resolver = LeakingResolver(command)
    consumer = ProjectionRequestConsumer(
        canonical_resolver=resolver,
        projection=projection,
        receipt_builder=_AdapterReceiptBuilder(),
        result_bridge=ProjectionResultMappingBridge(
            DeterministicCanonicalProjectionGateway((_context(command),)),
            id_source=lambda: UUID(int=117),
        ),
    )
    intent = _intent()
    diagnostic_context = DiagnosticContext(
        trace_id=intent.trace_id,
        correlation_id=intent.correlation_id,
        causation_id=intent.event_id,
    )

    try:
        with pytest.raises(TerminalOutboxConsumerFailure) as captured:
            await consumer.consume(intent, diagnostic_context)
    finally:
        resolver.context.__exit__(None, None, None)

    assert captured.value.code == "projection.request.canonical_uow_open"
    assert projection.recorded_commands == ()


@pytest.mark.parametrize(
    "foreign_command",
    (
        _command(workspace_id=UUID(int=999)),
        _command(environment_id=UUID(int=999)),
    ),
    ids=("workspace", "environment"),
)
@pytest.mark.asyncio
async def test_foreign_canonical_context_is_rejected_before_physical_projection(
    foreign_command: ProjectCommand,
) -> None:
    projection = DeterministicContextProjection()
    consumer = ProjectionRequestConsumer(
        canonical_resolver=_CanonicalResolver(foreign_command),
        projection=projection,
        receipt_builder=_AdapterReceiptBuilder(),
        result_bridge=ProjectionResultMappingBridge(
            DeterministicCanonicalProjectionGateway((_context(foreign_command),)),
            id_source=lambda: UUID(int=110),
        ),
    )
    intent = _intent()
    diagnostic_context = DiagnosticContext(
        trace_id=intent.trace_id,
        correlation_id=intent.correlation_id,
        causation_id=intent.event_id,
    )

    with pytest.raises(TerminalOutboxConsumerFailure) as captured:
        await consumer.consume(intent, diagnostic_context)

    assert captured.value.code == "projection.request.context_invalid"
    assert projection.recorded_commands == ()


@pytest.mark.asyncio
async def test_mismatched_dispatch_context_is_rejected_before_resolution() -> None:
    command = _command()
    resolver = _CanonicalResolver(command)
    projection = DeterministicContextProjection()
    consumer = ProjectionRequestConsumer(
        canonical_resolver=resolver,
        projection=projection,
        receipt_builder=_AdapterReceiptBuilder(),
        result_bridge=ProjectionResultMappingBridge(
            DeterministicCanonicalProjectionGateway((_context(command),)),
            id_source=lambda: UUID(int=115),
        ),
    )
    intent = _intent()
    mismatched_context = DiagnosticContext(
        trace_id=intent.trace_id,
        correlation_id=intent.correlation_id,
        causation_id=UUID(int=999),
    )

    with pytest.raises(TerminalOutboxConsumerFailure) as captured:
        await consumer.consume(intent, mismatched_context)

    assert captured.value.code == "projection.request.invalid"
    assert resolver.requests == []
    assert projection.recorded_commands == ()


@pytest.mark.asyncio
async def test_mismatched_outbox_lineage_is_rejected_before_resolution() -> None:
    command = _command()
    resolver = _CanonicalResolver(command)
    projection = DeterministicContextProjection()
    consumer = ProjectionRequestConsumer(
        canonical_resolver=resolver,
        projection=projection,
        receipt_builder=_AdapterReceiptBuilder(),
        result_bridge=ProjectionResultMappingBridge(
            DeterministicCanonicalProjectionGateway((_context(command),)),
            id_source=lambda: UUID(int=116),
        ),
    )
    intent = _intent().model_copy(
        update={"producer_deduplication_id": "projection-request:wrong-item"}
    )
    diagnostic_context = DiagnosticContext(
        trace_id=intent.trace_id,
        correlation_id=intent.correlation_id,
        causation_id=intent.event_id,
    )

    with pytest.raises(TerminalOutboxConsumerFailure) as captured:
        await consumer.consume(intent, diagnostic_context)

    assert captured.value.code == "projection.request.lineage_invalid"
    assert resolver.requests == []
    assert projection.recorded_commands == ()


@pytest.mark.asyncio
async def test_non_retryable_physical_rejection_returns_safe_terminal_code() -> None:
    command = _command().model_copy(
        update={
            "source_revisions": (
                _command().source_revisions[0].model_copy(update={"status": "stale"}),
            )
        }
    )
    projection = DeterministicContextProjection()
    consumer = ProjectionRequestConsumer(
        canonical_resolver=_CanonicalResolver(command),
        projection=projection,
        receipt_builder=_AdapterReceiptBuilder(),
        result_bridge=ProjectionResultMappingBridge(
            DeterministicCanonicalProjectionGateway((_context(command),)),
            id_source=lambda: UUID(int=117),
        ),
    )
    intent = _intent()
    diagnostic_context = DiagnosticContext(
        trace_id=intent.trace_id,
        correlation_id=intent.correlation_id,
        causation_id=intent.event_id,
    )

    with pytest.raises(TerminalOutboxConsumerFailure) as captured:
        await consumer.consume(intent, diagnostic_context)

    assert captured.value.code == "projection.request.physical_rejected"
    assert str(captured.value) == "projection.request.physical_rejected"


@pytest.mark.asyncio
async def test_missing_canonical_handoff_context_returns_safe_terminal_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    command = _command()
    projection = DeterministicContextProjection()
    consumer = ProjectionRequestConsumer(
        canonical_resolver=_CanonicalResolver(command),
        projection=projection,
        receipt_builder=_AdapterReceiptBuilder(),
        result_bridge=ProjectionResultMappingBridge(
            DeterministicCanonicalProjectionGateway(),
            id_source=lambda: UUID(int=111),
        ),
    )
    intent = _intent()
    diagnostic_context = DiagnosticContext(
        trace_id=intent.trace_id,
        correlation_id=intent.correlation_id,
        causation_id=intent.event_id,
    )

    with pytest.raises(TerminalOutboxConsumerFailure) as captured:
        await consumer.consume(intent, diagnostic_context)

    assert captured.value.code == "projection.request.canonical_rejected"
    assert str(captured.value) == "projection.request.canonical_rejected"
    assert "canonical mapping rejected at preparation" in caplog.text


@pytest.mark.asyncio
async def test_active_namespace_is_rejected_before_physical_projection() -> None:
    command = _command(active=True)
    projection = DeterministicContextProjection()
    consumer = ProjectionRequestConsumer(
        canonical_resolver=_CanonicalResolver(command),
        projection=projection,
        receipt_builder=_AdapterReceiptBuilder(),
        result_bridge=ProjectionResultMappingBridge(
            DeterministicCanonicalProjectionGateway((_context(command),)),
            id_source=lambda: UUID(int=112),
        ),
    )
    intent = _intent()
    diagnostic_context = DiagnosticContext(
        trace_id=intent.trace_id,
        correlation_id=intent.correlation_id,
        causation_id=intent.event_id,
    )

    with pytest.raises(TerminalOutboxConsumerFailure) as captured:
        await consumer.consume(intent, diagnostic_context)

    assert captured.value.code == "projection.request.canonical_rejected"
    assert projection.recorded_commands == ()


@pytest.mark.asyncio
async def test_lost_physical_response_retries_same_command_without_leaking_error() -> None:
    command = _command()
    physical_projection = DeterministicContextProjection()
    projection = _FailAfterPhysicalProjection(physical_projection)
    gateway = DeterministicCanonicalProjectionGateway((_context(command),))
    consumer = ProjectionRequestConsumer(
        canonical_resolver=_CanonicalResolver(command),
        projection=projection,  # type: ignore[arg-type]
        receipt_builder=_AdapterReceiptBuilder(),
        result_bridge=ProjectionResultMappingBridge(
            gateway,
            id_source=lambda: UUID(int=113),
        ),
    )
    intent = _intent()
    diagnostic_context = DiagnosticContext(
        trace_id=intent.trace_id,
        correlation_id=intent.correlation_id,
        causation_id=intent.event_id,
    )

    with pytest.raises(RetryableOutboxConsumerFailure) as captured:
        await consumer.consume(intent, diagnostic_context)
    await consumer.consume(intent, diagnostic_context)

    assert captured.value.code == "projection.request.physical_retryable"
    assert str(captured.value) == "projection.request.physical_retryable"
    assert physical_projection.recorded_commands == (command,)
    replay = await gateway.replay_for(command)
    assert replay is not None


@pytest.mark.asyncio
async def test_worker_consumes_projection_request_only_after_outbox_commit() -> None:
    adapter = await create_in_memory_adapter()
    await persist_workspace(adapter, WORKSPACE)
    await persist_environment(adapter, WORKSPACE, ENVIRONMENT)
    command = _command()
    projection = DeterministicContextProjection()
    consumer = ProjectionRequestConsumer(
        canonical_resolver=_CanonicalResolver(command),
        projection=projection,
        receipt_builder=_AdapterReceiptBuilder(),
        result_bridge=ProjectionResultMappingBridge(
            DeterministicCanonicalProjectionGateway((_context(command),)),
            id_source=lambda: UUID(int=114),
        ),
    )
    diagnostic_events = DiagnosticEventRegistry.with_default_families()
    worker = create_outbox_worker(
        repository=adapter.delivery_repository,
        consumer_bindings=(
            OutboxConsumerBinding(
                consumer_name="projection_request",
                consumer=consumer,
            ),
        ),
        audit_events=adapter.audit_events,
        diagnostic_events=diagnostic_events,
        diagnostic_sink=RecordingDiagnosticSink(diagnostic_events),
        service_principal_id=synthetic_uuid(6801),
        clock=lambda: datetime(2026, 10, 14, 12, tzinfo=timezone.utc),
        audit_event_id_source=lambda: synthetic_uuid(6802),
        retry_delay=lambda attempt: timedelta(seconds=attempt),
        maximum_retry_delay=timedelta(minutes=5),
        lease_duration=timedelta(seconds=30),
        handler_timeout=timedelta(seconds=5),
        maximum_attempts=3,
    )
    persistence_context = adapter.environment_context(WORKSPACE, ENVIRONMENT)
    async with adapter.uow_factory(persistence_context) as uow:
        await uow.outbox.append(_intent(trace_id=persistence_context.trace_id))

    before_commit = await worker.dispatch_batch(
        limit=1,
        worker_identity="projection_worker",
    )
    assert before_commit.claimed == 0
    assert projection.recorded_commands == ()

    async with adapter.uow_factory(persistence_context) as uow:
        await uow.outbox.append(_intent(trace_id=persistence_context.trace_id))
        await uow.commit()
    after_commit = await worker.dispatch_batch(
        limit=1,
        worker_identity="projection_worker",
    )

    assert after_commit.delivered == 1
    assert projection.recorded_commands == (command,)


@pytest.mark.asyncio
async def test_duplicate_after_candidate_sealing_returns_recorded_handoff() -> None:
    command = _command()
    context = _context(command)
    gateway = DeterministicCanonicalProjectionGateway((context,))
    projection = DeterministicContextProjection()
    consumer = ProjectionRequestConsumer(
        canonical_resolver=_CanonicalResolver(command),
        projection=projection,
        receipt_builder=_AdapterReceiptBuilder(),
        result_bridge=ProjectionResultMappingBridge(
            gateway,
            id_source=lambda: UUID(int=118),
        ),
    )
    intent = _intent()
    diagnostic_context = DiagnosticContext(
        trace_id=intent.trace_id,
        correlation_id=intent.correlation_id,
        causation_id=intent.event_id,
    )
    await consumer.consume(intent, diagnostic_context)
    gateway.add_context(
        context.model_copy(
            update={
                "run": context.run.model_copy(
                    update={
                        "candidate_state": ProjectionCandidateState.SEALED,
                        "candidate_evidence_digest": "8" * 64,
                        "candidate_sealed_at": NOW,
                    }
                )
            }
        )
    )

    outcome = await consumer.consume(intent, diagnostic_context)

    assert outcome.diagnostic_context == diagnostic_context
    assert projection.recorded_commands == (command,)
