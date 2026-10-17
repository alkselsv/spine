from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.diagnostics import DiagnosticContext
from spine.application.telemetry import (
    InMemoryOperationalObservationStore,
    ObservedAnswerQuestionRunRepository,
    ObservedOutboxWriter,
    OperationalComponent,
    OperationalObservationEmitter,
    OperationalObservationOutcome,
    OperationalObservationPolicy,
    OperationalObservationQuery,
    OperationalObservationReader,
    OperationalObservationRegistry,
    OperationalOperation,
    OperationalStage,
    OperationalTelemetryAccessDenied,
    RecordingOperationalObservationSink,
    SafeOperationalFailureCode,
)
from spine.application.persistence.outbox import OutboxIntent
from spine.auth import AuthorizationRole, RequestedAuthorizationScope
from spine.application.diagnostics import DiagnosticEventRegistry
from spine.infrastructure.diagnostics import RecordingDiagnosticSink
from spine.infrastructure.telemetry import DiagnosticOperationalObservationSink


NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000361")
ENVIRONMENT_ID = UUID("30000000-0000-0000-0000-000000000361")
TRACE_ID = UUID("50000000-0000-0000-0000-0000000003e8")


def context(trace_id: UUID = TRACE_ID) -> DiagnosticContext:
    return DiagnosticContext(
        trace_id=trace_id,
        correlation_id=UUID("51000000-0000-0000-0000-000000000361"),
        causation_id=UUID("52000000-0000-0000-0000-000000000361"),
    )


def administrator(workspace_id: UUID = WORKSPACE_ID) -> SimpleNamespace:
    return SimpleNamespace(
        roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        scope=RequestedAuthorizationScope(
            workspace_id=workspace_id,
            environment_id=ENVIRONMENT_ID,
        ),
    )


def build_observation(
    registry: OperationalObservationRegistry,
    *,
    observed_at: datetime = NOW,
    outcome: OperationalObservationOutcome = OperationalObservationOutcome.SUCCEEDED,
    failure_code: SafeOperationalFailureCode | None = None,
):
    return registry.build(
        observation_id=UUID("70000000-0000-0000-0000-000000000361"),
        observed_at=observed_at,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        diagnostic_context=context(),
        component=OperationalComponent.RETRIEVAL,
        stage=OperationalStage.RETRIEVAL,
        operation=OperationalOperation.CONTEXT_RETRIEVAL,
        outcome=outcome,
        safe_failure_code=failure_code,
        duration_ms=12,
        attempt_number=1,
        run_id=UUID("71000000-0000-0000-0000-000000000361"),
        attempt_id=UUID("72000000-0000-0000-0000-000000000361"),
        message_id=UUID("73000000-0000-0000-0000-000000000361"),
    )


def test_observation_schema_is_closed_frozen_and_content_free() -> None:
    registry = OperationalObservationRegistry()
    observation = build_observation(registry)

    assert observation.schema_version == "operational-observation:v1"
    assert observation.metric_dimensions().model_dump() == {
        "component": "retrieval",
        "stage": "retrieval",
        "operation": "context_retrieval",
        "outcome": "succeeded",
        "safe_failure_code": None,
    }
    assert "trace_id" not in observation.metric_dimensions().model_dump()
    with pytest.raises(ValidationError):
        type(observation).model_validate(
            {**observation.model_dump(mode="python"), "question": "protected"}
        )
    with pytest.raises(ValidationError):
        observation.component = OperationalComponent.API  # type: ignore[misc]


def test_failure_requires_closed_safe_code_and_success_rejects_it() -> None:
    registry = OperationalObservationRegistry()

    with pytest.raises(ValidationError):
        build_observation(
            registry,
            outcome=OperationalObservationOutcome.FAILED,
        )
    with pytest.raises(ValidationError):
        build_observation(
            registry,
            failure_code=SafeOperationalFailureCode.INTERNAL_FAILURE,
        )
    with pytest.raises(ValueError):
        SafeOperationalFailureCode("provider said the prompt was secret")


@pytest.mark.asyncio
async def test_emitter_samples_success_by_trace_and_never_samples_errors() -> None:
    registry = OperationalObservationRegistry()
    sink = RecordingOperationalObservationSink(registry)
    emitter = OperationalObservationEmitter(
        registry=registry,
        sink=sink,
        policy=OperationalObservationPolicy(),
        clock=lambda: NOW,
        observation_id_source=lambda: UUID(
            "70000000-0000-0000-0000-000000000362"
        ),
    )

    sampled = await emitter.emit(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        diagnostic_context=context(UUID(int=1000)),
        component=OperationalComponent.API,
        stage=OperationalStage.ACCEPTANCE,
        operation=OperationalOperation.HTTP_REQUEST,
        outcome=OperationalObservationOutcome.SUCCEEDED,
    )
    dropped = await emitter.emit(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        diagnostic_context=context(UUID(int=1015)),
        component=OperationalComponent.API,
        stage=OperationalStage.ACCEPTANCE,
        operation=OperationalOperation.HTTP_REQUEST,
        outcome=OperationalObservationOutcome.SUCCEEDED,
    )
    error = await emitter.emit(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        diagnostic_context=context(UUID(int=1015)),
        component=OperationalComponent.GENERATION,
        stage=OperationalStage.GENERATION,
        operation=OperationalOperation.ANSWER_GENERATION,
        outcome=OperationalObservationOutcome.FAILED,
        safe_failure_code=SafeOperationalFailureCode.GENERATION_UNAVAILABLE,
    )

    assert (sampled, dropped, error) == (True, False, True)
    assert [item.outcome for item in sink.observations] == [
        OperationalObservationOutcome.SUCCEEDED,
        OperationalObservationOutcome.SUCCEEDED,
        OperationalObservationOutcome.FAILED,
    ]
    assert [item.trace_id for item in sink.observations[1:]] == [
        UUID(int=1015),
        UUID(int=1015),
    ]


@pytest.mark.asyncio
async def test_sink_failure_cannot_fail_the_business_boundary() -> None:
    class BrokenSink:
        async def emit(self, observation: object) -> None:
            del observation
            raise RuntimeError("sink unavailable and secret provider payload")

    emitter = OperationalObservationEmitter(
        registry=OperationalObservationRegistry(),
        sink=BrokenSink(),
        policy=OperationalObservationPolicy(),
        clock=lambda: NOW,
        observation_id_source=lambda: UUID(int=1),
    )

    emitted = await emitter.emit(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        diagnostic_context=context(UUID(int=1000)),
        component=OperationalComponent.RUN,
        stage=OperationalStage.PERSISTENCE,
        operation=OperationalOperation.RUN_PERSISTENCE,
        outcome=OperationalObservationOutcome.SUCCEEDED,
    )

    assert emitted is False


@pytest.mark.asyncio
async def test_emitter_fails_closed_before_sink_for_malformed_trusted_context() -> None:
    emitter = OperationalObservationEmitter(
        registry=OperationalObservationRegistry(),
        sink=RecordingOperationalObservationSink(OperationalObservationRegistry()),
        policy=OperationalObservationPolicy(),
        clock=lambda: NOW,
        observation_id_source=lambda: UUID(int=1),
    )

    with pytest.raises(ValueError, match="trusted diagnostic context"):
        await emitter.emit(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            diagnostic_context=object(),  # type: ignore[arg-type]
            component=OperationalComponent.API,
            stage=OperationalStage.ACCEPTANCE,
            operation=OperationalOperation.HTTP_REQUEST,
            outcome=OperationalObservationOutcome.SUCCEEDED,
        )


@pytest.mark.asyncio
async def test_emitter_never_swallows_task_cancellation() -> None:
    class CancelledSink:
        async def emit(self, observation: object) -> None:
            del observation
            raise asyncio.CancelledError

    emitter = OperationalObservationEmitter(
        registry=OperationalObservationRegistry(),
        sink=CancelledSink(),
        policy=OperationalObservationPolicy(),
        clock=lambda: NOW,
        observation_id_source=lambda: UUID(int=1),
    )

    with pytest.raises(asyncio.CancelledError):
        await emitter.emit(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            diagnostic_context=context(UUID(int=1000)),
            component=OperationalComponent.API,
            stage=OperationalStage.ACCEPTANCE,
            operation=OperationalOperation.HTTP_REQUEST,
            outcome=OperationalObservationOutcome.SUCCEEDED,
        )


@pytest.mark.asyncio
async def test_reader_enforces_scope_retention_and_legal_hold() -> None:
    registry = OperationalObservationRegistry()
    store = InMemoryOperationalObservationStore(registry)
    reader = OperationalObservationReader(store=store, clock=lambda: NOW)
    expired = build_observation(
        registry,
        observed_at=NOW - timedelta(days=31),
    )
    current = build_observation(registry)
    await store.emit(expired)
    await store.emit(current)
    store.place_legal_hold(expired.observation_id)
    query = OperationalObservationQuery(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
    )

    page = await reader.read(query=query, authority=administrator())

    assert page.observations == (expired, current)
    with pytest.raises(OperationalTelemetryAccessDenied):
        await reader.read(
            query=query,
            authority=administrator(UUID("20000000-0000-0000-0000-000000000999")),
        )
    store.release_legal_hold(expired.observation_id)
    page = await reader.read(query=query, authority=administrator())
    assert page.observations == (current,)


def test_policy_is_explicit_versioned_and_exporter_disabled() -> None:
    policy = OperationalObservationPolicy()

    assert policy.schema_version == "operational-telemetry-policy:v1"
    assert policy.retention_days == 30
    assert policy.error_sample_percent == 100
    assert policy.successful_trace_sample_percent == 10
    assert policy.exporter == "disabled"


@pytest.mark.asyncio
async def test_diagnostic_bridge_preserves_trusted_context_for_each_closed_boundary_label() -> None:
    registry = OperationalObservationRegistry()
    diagnostic_registry = DiagnosticEventRegistry.with_default_families()
    diagnostic_sink = RecordingDiagnosticSink(diagnostic_registry)
    bridge = DiagnosticOperationalObservationSink(
        observation_registry=registry,
        diagnostic_registry=diagnostic_registry,
        diagnostic_sink=diagnostic_sink,
    )
    emitter = OperationalObservationEmitter(
        registry=registry,
        sink=bridge,
        policy=OperationalObservationPolicy(),
        clock=lambda: NOW,
        observation_id_source=iter(UUID(int=value) for value in range(1, 9)).__next__,
    )
    trusted = context(UUID(int=1000))
    hops = (
        (OperationalComponent.API, OperationalStage.ACCEPTANCE, OperationalOperation.HTTP_REQUEST),
        (OperationalComponent.OUTBOX, OperationalStage.ENQUEUE, OperationalOperation.OUTBOX_ENQUEUE),
        (OperationalComponent.WORKER, OperationalStage.DISPATCH, OperationalOperation.OUTBOX_DELIVERY),
        (OperationalComponent.PROJECTION, OperationalStage.PROJECTION, OperationalOperation.PROJECTION_BUILD),
        (OperationalComponent.RETRIEVAL, OperationalStage.RETRIEVAL, OperationalOperation.CONTEXT_RETRIEVAL),
        (OperationalComponent.GENERATION, OperationalStage.GENERATION, OperationalOperation.ANSWER_GENERATION),
        (OperationalComponent.RUN, OperationalStage.PERSISTENCE, OperationalOperation.RUN_PERSISTENCE),
        (OperationalComponent.SSE, OperationalStage.DELIVERY, OperationalOperation.SSE_DELIVERY),
    )

    for component, stage, operation in hops:
        assert await emitter.emit(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            diagnostic_context=trusted,
            component=component,
            stage=stage,
            operation=operation,
            outcome=OperationalObservationOutcome.SUCCEEDED,
        )

    assert [event.payload.component for event in diagnostic_sink.events] == [
        component.value for component, _, _ in hops
    ]
    assert {event.trace_id for event in diagnostic_sink.events} == {trusted.trace_id}
    assert {event.correlation_id for event in diagnostic_sink.events} == {
        trusted.correlation_id
    }
    assert {event.causation_id for event in diagnostic_sink.events} == {
        trusted.causation_id
    }
    serialized = "".join(event.model_dump_json() for event in diagnostic_sink.events)
    for protected in (
        "customer document text",
        "secret-token-value",
        "provider stack trace",
        "chain of thought",
    ):
        assert protected not in serialized


def test_registry_rejects_substituted_trace_context() -> None:
    registry = OperationalObservationRegistry()
    observation = build_observation(registry)
    substituted = observation.model_copy(update={"trace_id": UUID(int=999)})

    with pytest.raises(ValueError, match="context is invalid"):
        registry.validate(substituted)


@pytest.mark.asyncio
async def test_observed_outbox_writer_records_the_actual_enqueue_handoff() -> None:
    event_id = UUID(int=900)

    class Writer:
        async def append(self, intent: OutboxIntent) -> UUID:
            assert intent.event_id == event_id
            return event_id

    registry = OperationalObservationRegistry()
    sink = RecordingOperationalObservationSink(registry)
    observer = OperationalObservationEmitter(
        registry=registry,
        sink=sink,
        policy=OperationalObservationPolicy(),
        clock=lambda: NOW,
        observation_id_source=lambda: UUID(int=901),
    )
    intent = OutboxIntent(
        event_id=event_id,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        event_type="telemetry.test",
        schema_version=1,
        payload=OperationalObservationPolicy(),
        trace_id=UUID(int=1000),
        correlation_id=UUID(int=902),
        causation_id=UUID(int=903),
    )

    persisted_id = await ObservedOutboxWriter(Writer(), observer).append(intent)

    assert persisted_id == event_id
    assert len(sink.observations) == 1
    observation = sink.observations[0]
    assert observation.component is OperationalComponent.OUTBOX
    assert observation.operation is OperationalOperation.OUTBOX_ENQUEUE
    assert observation.trace_id == intent.trace_id
    assert observation.correlation_id == intent.correlation_id
    assert observation.causation_id == intent.causation_id


@pytest.mark.asyncio
async def test_observed_run_repository_forwards_scoped_resolution() -> None:
    expected = object()
    calls: dict[str, UUID] = {}

    class Repository:
        async def resolve_run_in_scope(self, *, workspace_id, environment_id, run_id):
            calls.update(
                workspace_id=workspace_id,
                environment_id=environment_id,
                run_id=run_id,
            )
            return expected

    result = await ObservedAnswerQuestionRunRepository(Repository(), object()).resolve_run_in_scope(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        run_id=UUID(int=999),
    )

    assert result is expected
    assert calls == {
        "workspace_id": WORKSPACE_ID,
        "environment_id": ENVIRONMENT_ID,
        "run_id": UUID(int=999),
    }
