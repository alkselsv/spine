from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from spine.application.diagnostics import DiagnosticContext, DiagnosticEventRegistry
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.outbox import OpaqueObjectReference, OutboxEventRegistry
from spine.application.persistence.projection import (
    PROJECTION_OUTBOX_SCHEMA_VERSION,
    PROJECTION_RECOVERY_REQUESTED_OUTBOX_EVENT_TYPE,
    PROJECTION_SUCCESSOR_REQUESTED_OUTBOX_EVENT_TYPE,
    ProjectionRecoveryAction,
    ProjectionRecoveryRequestedOutboxPayload,
    ProjectionSuccessorRequestedOutboxPayload,
    register_projection_outbox_events,
)
from spine.application.persistence.delivery import OutboxDeliveryRouteRegistry
from spine.application.projection_control import ProjectionRecoveryRequestService
from spine.application.outbox import TerminalOutboxConsumerFailure
from spine.application.telemetry import (
    OperationalComponent,
    OperationalObservationEmitter,
    OperationalObservationPolicy,
    OperationalObservationRegistry,
    RecordingOperationalObservationSink,
    SafeOperationalFailureCode,
)
from spine.application.projection_recovery_execution import (
    CanonicalProjectionRecoveryPlan,
    MappingProjectionRecoveryResultRecorder,
    ProjectionRecoveryRequestConsumer,
    ProjectionRecoveryRequestEnvelope,
)
from spine.infrastructure.persistence.projection_composition import (
    PROJECTION_REQUEST_CONSUMER_NAME,
    register_projection_delivery_routes,
)
from spine.application.projection_result_mapping import ProjectionResultMappingBridge
from spine.application.testing import DeterministicCanonicalProjectionGateway
from spine.infrastructure.diagnostics import RecordingDiagnosticSink
from spine.memory.contracts import RebuildCommand
from spine.memory.fake import DeterministicContextProjection, DeterministicPhysicalStore
from spine.workers import OutboxConsumerBinding, create_outbox_worker
from tests.application.test_projection_result_mapping import (
    _adapter_receipt,
    _command,
    _context,
)
from tests.contracts.persistence.conftest import create_in_memory_adapter
from tests.contracts.persistence.ids import synthetic_uuid
from tests.contracts.persistence.test_idempotency_repository_contract import (
    persist_environment,
    persist_workspace,
)


NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
WORKSPACE = UUID(int=9101)
ENVIRONMENT = UUID(int=9102)


class _Resolver:
    def __init__(self, command: RebuildCommand) -> None:
        self.command = command
        self.requests: list[ProjectionRecoveryRequestEnvelope] = []

    async def resolve(
        self, request: ProjectionRecoveryRequestEnvelope
    ) -> CanonicalProjectionRecoveryPlan:
        self.requests.append(request)
        return CanonicalProjectionRecoveryPlan(
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            command_id=request.payload.command_id,
            action=(
                ProjectionRecoveryAction.SUCCESSOR
                if isinstance(
                    request.payload, ProjectionSuccessorRequestedOutboxPayload
                )
                else request.payload.action
            ),
            request_digest=request.payload.command_digest,
            command=self.command,
        )


class _Recorder:
    def __init__(self) -> None:
        self.results = []

    async def prepare(self, plan: CanonicalProjectionRecoveryPlan) -> bool:
        return bool(self.results)

    async def record(self, plan, result):
        mapping = (plan, result)
        self.results.append(mapping)
        return mapping


class _Completion:
    def __init__(self) -> None:
        self.mappings = []

    async def complete(self, mapping) -> None:
        self.mappings.append(mapping)


class _ReceiptBuilder:
    async def build(self, command, result):
        return _adapter_receipt(command).model_copy(update={"result": result})


def _rebuild_command() -> RebuildCommand:
    ordinary = _command(workspace_id=WORKSPACE, environment_id=ENVIRONMENT)
    return RebuildCommand(**ordinary.model_dump(exclude={"operation"}))


def _intent():
    registry = OutboxEventRegistry()
    register_projection_outbox_events(registry)
    command_id = UUID(int=9201)
    return registry.build_intent(
        event_id=UUID(int=9202),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        event_type=PROJECTION_RECOVERY_REQUESTED_OUTBOX_EVENT_TYPE,
        schema_version=PROJECTION_OUTBOX_SCHEMA_VERSION,
        payload=ProjectionRecoveryRequestedOutboxPayload(
            command_id=command_id,
            projection_kind="context",
            action=ProjectionRecoveryAction.REBUILD,
            command_digest="9" * 64,
        ),
        aggregate=OpaqueObjectReference(
            object_type="projection_recovery_request",
            object_id=command_id,
            schema_version=1,
        ),
        producer_deduplication_id=f"projection-recovery:{command_id}",
        trace_id=UUID(int=9203),
    )


def _successor_intent():
    registry = OutboxEventRegistry()
    register_projection_outbox_events(registry)
    command_id = UUID(int=9251)
    return registry.build_intent(
        event_id=UUID(int=9252),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        event_type=PROJECTION_SUCCESSOR_REQUESTED_OUTBOX_EVENT_TYPE,
        schema_version=PROJECTION_OUTBOX_SCHEMA_VERSION,
        payload=ProjectionSuccessorRequestedOutboxPayload(
            command_id=command_id,
            projection_kind="context",
            canonical_boundary_id=UUID(int=9253),
            canonical_boundary_digest="a" * 64,
            accepted_through_generation=2,
            command_digest="9" * 64,
        ),
        aggregate=OpaqueObjectReference(
            object_type="projection_successor_request",
            object_id=command_id,
            schema_version=1,
        ),
        producer_deduplication_id=f"projection-successor:{command_id}",
        trace_id=UUID(int=9254),
    )


def test_projection_delivery_routes_include_operator_recovery_requests() -> None:
    routes = OutboxDeliveryRouteRegistry(version="issue364.v1")

    register_projection_delivery_routes(routes)
    routes.seal()

    route = routes.resolve(
        PROJECTION_RECOVERY_REQUESTED_OUTBOX_EVENT_TYPE,
        PROJECTION_OUTBOX_SCHEMA_VERSION,
    )
    assert route.consumer_name == PROJECTION_REQUEST_CONSUMER_NAME
    successor_route = routes.resolve(
        PROJECTION_SUCCESSOR_REQUESTED_OUTBOX_EVENT_TYPE,
        PROJECTION_OUTBOX_SCHEMA_VERSION,
    )
    assert successor_route.consumer_name == PROJECTION_REQUEST_CONSUMER_NAME


@pytest.mark.asyncio
async def test_successor_intent_uses_rebuild_worker_without_active_recovery_payload() -> None:
    intent = _successor_intent()
    command = _rebuild_command()
    resolver = _Resolver(command)
    projection = DeterministicContextProjection()
    recorder = _Recorder()
    completion = _Completion()
    consumer = ProjectionRecoveryRequestConsumer(
        canonical_resolver=resolver,
        projection=projection,
        result_recorder=recorder,
        completion=completion,
    )

    await consumer.consume(
        intent,
        DiagnosticContext(
            trace_id=intent.trace_id,
            correlation_id=intent.correlation_id,
            causation_id=intent.event_id,
        ),
    )

    assert resolver.requests[0].payload.canonical_boundary_id == UUID(int=9253)
    assert projection.recorded_commands == (command,)
    assert completion.mappings == recorder.results


@pytest.mark.asyncio
async def test_recovery_request_dispatch_rebuilds_once_and_records_canonical_result() -> None:
    adapter = await create_in_memory_adapter()
    await persist_workspace(adapter, WORKSPACE)
    await persist_environment(adapter, WORKSPACE, ENVIRONMENT)
    context = adapter.ingestion_context(
        WORKSPACE, ENVIRONMENT, "request_projection_recovery"
    )
    request_service = ProjectionRecoveryRequestService(
        uow_factory=adapter.uow_factory,
        outbox_events=adapter.outbox_events,
    )
    accepted = await request_service.request(
        context=context,
        projection_kind="context",
        action=ProjectionRecoveryAction.REBUILD,
        idempotency_key=IdempotencyKey("projection-recovery-364"),
    )
    command = _rebuild_command()
    resolver = _Resolver(command)
    projection = DeterministicContextProjection()
    gateway = DeterministicCanonicalProjectionGateway((_context(command),))
    recorder = MappingProjectionRecoveryResultRecorder(
        receipt_builder=_ReceiptBuilder(),
        result_bridge=ProjectionResultMappingBridge(
            gateway,
            id_source=lambda: UUID(int=9105),
        ),
    )
    consumer = ProjectionRecoveryRequestConsumer(
        canonical_resolver=resolver,
        projection=projection,
        result_recorder=recorder,
    )
    diagnostic_events = DiagnosticEventRegistry.with_default_families()
    worker = create_outbox_worker(
        repository=adapter.delivery_repository,
        consumer_bindings=(
            OutboxConsumerBinding(
                consumer_name=PROJECTION_REQUEST_CONSUMER_NAME,
                consumer=consumer,
            ),
        ),
        audit_events=adapter.audit_events,
        diagnostic_events=diagnostic_events,
        diagnostic_sink=RecordingDiagnosticSink(diagnostic_events),
        service_principal_id=synthetic_uuid(9103),
        clock=lambda: NOW,
        audit_event_id_source=lambda: synthetic_uuid(9104),
        retry_delay=lambda attempt: timedelta(seconds=attempt),
        maximum_retry_delay=timedelta(minutes=5),
        lease_duration=timedelta(seconds=30),
        handler_timeout=timedelta(seconds=5),
        maximum_attempts=3,
    )

    report = await worker.dispatch_batch(
        limit=1,
        worker_identity="projection_recovery_worker",
    )
    replay_report = await worker.dispatch_batch(
        limit=1,
        worker_identity="projection_recovery_worker",
    )

    assert report.delivered == 1
    assert replay_report.claimed == 0
    assert resolver.requests == [
        ProjectionRecoveryRequestEnvelope(
            event_id=resolver.requests[0].event_id,
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            payload=resolver.requests[0].payload,
        )
    ]
    assert resolver.requests[0].payload.command_id == accepted.command_id
    assert projection.recorded_commands == (command,)
    replay = await gateway.replay_for(command)
    assert replay is not None
    assert replay.handoff.command == command
    assert replay.handoff.receipt_command.receipt.command_identity == command.command_identity


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "plan_override",
    (
        {"workspace_id": UUID(int=9999)},
        {"environment_id": UUID(int=9999)},
        {"request_digest": "8" * 64},
    ),
    ids=("workspace", "environment", "request-digest"),
)
async def test_foreign_or_changed_recovery_plan_is_rejected_before_physical_work(
    plan_override: dict[str, object],
) -> None:
    intent = _intent()
    command = _rebuild_command()

    class Resolver(_Resolver):
        async def resolve(self, request):
            plan = await super().resolve(request)
            return plan.model_copy(update=plan_override)

    projection = DeterministicContextProjection()
    recorder = _Recorder()
    consumer = ProjectionRecoveryRequestConsumer(
        canonical_resolver=Resolver(command),
        projection=projection,
        result_recorder=recorder,
    )

    with pytest.raises(TerminalOutboxConsumerFailure) as captured:
        await consumer.consume(
            intent,
            DiagnosticContext(
                trace_id=intent.trace_id,
                correlation_id=intent.correlation_id,
                causation_id=intent.event_id,
            ),
        )

    assert captured.value.code == "projection.recovery.context_invalid"
    assert projection.recorded_commands == ()
    assert recorder.results == []


@pytest.mark.asyncio
async def test_retryable_physical_rebuild_redelivery_has_one_canonical_result() -> None:
    adapter = await create_in_memory_adapter()
    await persist_workspace(adapter, WORKSPACE)
    await persist_environment(adapter, WORKSPACE, ENVIRONMENT)
    context = adapter.ingestion_context(
        WORKSPACE, ENVIRONMENT, "request_projection_recovery"
    )
    request_service = ProjectionRecoveryRequestService(
        uow_factory=adapter.uow_factory,
        outbox_events=adapter.outbox_events,
    )
    await request_service.request(
        context=context,
        projection_kind="context",
        action=ProjectionRecoveryAction.REBUILD,
        idempotency_key=IdempotencyKey("projection-recovery-retry-364"),
    )
    command = _rebuild_command()
    physical_store = DeterministicPhysicalStore(
        fail_once_for={f"{command.command_identity}:1"}
    )
    projection = DeterministicContextProjection(physical_store=physical_store)
    gateway = DeterministicCanonicalProjectionGateway((_context(command),))
    recorder = MappingProjectionRecoveryResultRecorder(
        receipt_builder=_ReceiptBuilder(),
        result_bridge=ProjectionResultMappingBridge(
            gateway,
            id_source=lambda: UUID(int=9301),
        ),
    )
    diagnostic_events = DiagnosticEventRegistry.with_default_families()
    current_time = [NOW]
    audit_ids = iter((synthetic_uuid(9303), synthetic_uuid(9304)))
    worker = create_outbox_worker(
        repository=adapter.delivery_repository,
        consumer_bindings=(
            OutboxConsumerBinding(
                consumer_name=PROJECTION_REQUEST_CONSUMER_NAME,
                consumer=ProjectionRecoveryRequestConsumer(
                    canonical_resolver=_Resolver(command),
                    projection=projection,
                    result_recorder=recorder,
                ),
            ),
        ),
        audit_events=adapter.audit_events,
        diagnostic_events=diagnostic_events,
        diagnostic_sink=RecordingDiagnosticSink(diagnostic_events),
        service_principal_id=synthetic_uuid(9302),
        clock=lambda: current_time[0],
        audit_event_id_source=audit_ids.__next__,
        retry_delay=lambda attempt: timedelta(seconds=attempt),
        maximum_retry_delay=timedelta(minutes=5),
        lease_duration=timedelta(seconds=30),
        handler_timeout=timedelta(seconds=5),
        maximum_attempts=3,
    )

    first = await worker.dispatch_batch(
        limit=1,
        worker_identity="projection_recovery_worker",
    )
    current_time[0] += timedelta(seconds=2)
    second = await worker.dispatch_batch(
        limit=1,
        worker_identity="projection_recovery_worker",
    )

    assert first.retry_scheduled == 1
    assert second.delivered == 1
    assert physical_store.attempts_for(f"{command.command_identity}:1") == 2
    replay = await gateway.replay_for(command)
    assert replay is not None


@pytest.mark.asyncio
async def test_recovery_consumer_emits_content_free_projection_observation() -> None:
    intent = _intent()
    command = _rebuild_command()
    registry = OperationalObservationRegistry()
    sink = RecordingOperationalObservationSink(registry)
    observer = OperationalObservationEmitter(
        registry=registry,
        sink=sink,
        policy=OperationalObservationPolicy(),
        clock=lambda: NOW,
        observation_id_source=lambda: UUID(int=9401),
    )
    consumer = ProjectionRecoveryRequestConsumer(
        canonical_resolver=_Resolver(command),
        projection=DeterministicContextProjection(),
        result_recorder=_Recorder(),
        operational_observer=observer,
    )

    await consumer.consume(
        intent,
        DiagnosticContext(
            trace_id=intent.trace_id,
            correlation_id=intent.correlation_id,
            causation_id=intent.event_id,
        ),
    )

    assert len(sink.observations) == 1
    observation = sink.observations[0]
    assert observation.component is OperationalComponent.PROJECTION
    assert observation.workspace_id == WORKSPACE
    assert observation.environment_id == ENVIRONMENT
    assert observation.trace_id == intent.trace_id
    assert observation.run_id == command.projection_run_id


@pytest.mark.asyncio
async def test_rejected_rebuild_emits_only_bounded_failure_evidence() -> None:
    intent = _intent()
    command = _rebuild_command().model_copy(
        update={
            "source_revisions": (
                _rebuild_command().source_revisions[0].model_copy(
                    update={"status": "tombstoned"}
                ),
            )
        }
    )
    registry = OperationalObservationRegistry()
    sink = RecordingOperationalObservationSink(registry)
    observer = OperationalObservationEmitter(
        registry=registry,
        sink=sink,
        policy=OperationalObservationPolicy(),
        clock=lambda: NOW,
        observation_id_source=lambda: UUID(int=9402),
    )
    consumer = ProjectionRecoveryRequestConsumer(
        canonical_resolver=_Resolver(command),
        projection=DeterministicContextProjection(),
        result_recorder=_Recorder(),
        operational_observer=observer,
    )

    with pytest.raises(TerminalOutboxConsumerFailure) as captured:
        await consumer.consume(
            intent,
            DiagnosticContext(
                trace_id=intent.trace_id,
                correlation_id=intent.correlation_id,
                causation_id=intent.event_id,
            ),
        )

    assert captured.value.code == "projection.recovery.physical_rejected"
    assert len(sink.observations) == 1
    assert (
        sink.observations[0].safe_failure_code
        is SafeOperationalFailureCode.PROJECTION_UNAVAILABLE
    )
