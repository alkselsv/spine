from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
import asyncio
from uuid import UUID

import pytest

from spine.application.diagnostics import (
    DiagnosticContext,
    DiagnosticEventRegistry,
    OutboxDeliveryDiagnosticState,
)
from spine.application.outbox.dispatch import (
    DispatchOutcomeCode,
    OutboxConsumerRegistry,
    OutboxConsumerSuccess,
    OutboxDispatchRepository,
    OutboxDispatcher,
    RetryableOutboxConsumerFailure,
    TerminalOutboxConsumerFailure,
)
from spine.application.telemetry import (
    OperationalComponent,
    OperationalObservationEmitter,
    OperationalObservationPolicy,
    OperationalObservationRegistry,
    RecordingOperationalObservationSink,
)
from spine.application.persistence.delivery import OutboxDeliveryStatus
from spine.application.persistence.errors import UnexpectedPersistenceError
from spine.application.persistence.outbox import OutboxIntent
from spine.application.document_ingestion import (
    PROJECTION_REQUEST_EVENT_TYPE,
    PROJECTION_REQUEST_SCHEMA_VERSION,
    ProjectionRequestPayload,
)
from spine.domain.audit import AuditOutcome
from spine.infrastructure.diagnostics import RecordingDiagnosticSink
from tests.contracts.persistence.conftest import create_in_memory_adapter
from tests.contracts.persistence.adapter import DeliveryPersistenceAdapter
from tests.contracts.persistence.ids import synthetic_uuid
from tests.contracts.persistence.test_outbox_writer_contract import intent, workspace
from tests.contracts.persistence.outbox_events import WorkspaceCreatedPayload
from tests.contracts.persistence.test_idempotency_repository_contract import (
    persist_environment,
    persist_workspace,
)


class IdempotentRecordingConsumer:
    def __init__(self) -> None:
        self.effects: set[object] = set()
        self.contexts: list[DiagnosticContext] = []

    async def consume(
        self,
        intent: OutboxIntent,
        diagnostic_context: DiagnosticContext,
    ) -> OutboxConsumerSuccess:
        self.effects.add(intent.event_id)
        self.contexts.append(diagnostic_context)
        return OutboxConsumerSuccess(diagnostic_context=diagnostic_context)


class RetryableConsumer:
    async def consume(
        self,
        intent: OutboxIntent,
        diagnostic_context: DiagnosticContext,
    ) -> OutboxConsumerSuccess:
        del intent, diagnostic_context
        raise RetryableOutboxConsumerFailure("consumer.unavailable")


class TerminalConsumer:
    async def consume(
        self,
        intent: OutboxIntent,
        diagnostic_context: DiagnosticContext,
    ) -> OutboxConsumerSuccess:
        del intent, diagnostic_context
        raise TerminalOutboxConsumerFailure("consumer.rejected")


class TimingOutConsumer:
    async def consume(
        self,
        intent: OutboxIntent,
        diagnostic_context: DiagnosticContext,
    ) -> OutboxConsumerSuccess:
        del intent, diagnostic_context
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class MismatchedContextConsumer:
    async def consume(
        self,
        intent: OutboxIntent,
        diagnostic_context: DiagnosticContext,
    ) -> OutboxConsumerSuccess:
        del intent
        return OutboxConsumerSuccess(
            diagnostic_context=diagnostic_context.nested(synthetic_uuid(6699))
        )


async def _append_events(
    adapter: DeliveryPersistenceAdapter,
    *,
    workspace_id: UUID,
    event_ids: Iterable[UUID],
) -> None:
    context = adapter.workspace_context(workspace_id)
    async with adapter.uow_factory(context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        for event_id in event_ids:
            await uow.outbox.append(
                intent(
                    adapter,
                    workspace_id=workspace_id,
                    event_id=event_id,
                    trace_id=context.trace_id,
                    producer_deduplication_id=None,
                )
            )
        await uow.commit()


def _build_dispatcher(
    adapter: DeliveryPersistenceAdapter,
    *,
    consumers: OutboxConsumerRegistry,
    clock: Callable[[], datetime],
    audit_id_start: int,
    repository: OutboxDispatchRepository | None = None,
    retry_delay: Callable[[int], timedelta] = lambda attempt: timedelta(seconds=5),
    maximum_retry_delay: timedelta = timedelta(minutes=5),
    handler_timeout: timedelta = timedelta(seconds=5),
    operational_observer: OperationalObservationEmitter | None = None,
) -> tuple[OutboxDispatcher, RecordingDiagnosticSink, list[UUID]]:
    diagnostics = DiagnosticEventRegistry.with_default_families()
    diagnostic_sink = RecordingDiagnosticSink(diagnostics)
    audit_event_ids: list[UUID] = []

    def next_audit_event_id() -> UUID:
        value = synthetic_uuid(audit_id_start + len(audit_event_ids))
        audit_event_ids.append(value)
        return value

    return (
        OutboxDispatcher(
            repository=repository or adapter.delivery_repository,
            consumers=consumers,
            audit_events=adapter.audit_events,
            diagnostic_events=diagnostics,
            diagnostic_sink=diagnostic_sink,
            service_principal_id=synthetic_uuid(audit_id_start - 1),
            clock=clock,
            audit_event_id_source=next_audit_event_id,
            retry_delay=retry_delay,
            maximum_retry_delay=maximum_retry_delay,
            lease_duration=timedelta(seconds=30),
            handler_timeout=handler_timeout,
            maximum_attempts=3,
            operational_observer=operational_observer,
        ),
        diagnostic_sink,
        audit_event_ids,
    )


def test_consumer_registry_rejects_name_outside_delivery_contract() -> None:
    consumers = OutboxConsumerRegistry()

    with pytest.raises(ValueError, match="bounded identifier"):
        consumers.register("a" * 65, IdempotentRecordingConsumer())


@pytest.mark.asyncio
async def test_success_commits_delivery_audit_before_emitting_diagnostic() -> None:
    adapter = await create_in_memory_adapter()
    workspace_id = synthetic_uuid(6601)
    event_id = synthetic_uuid(6602)
    correlation_id = synthetic_uuid(6603)
    source_causation_id = synthetic_uuid(6604)
    context = adapter.workspace_context(workspace_id)
    outbox_intent = intent(
        adapter,
        workspace_id=workspace_id,
        event_id=event_id,
        trace_id=context.trace_id,
        producer_deduplication_id=None,
    ).model_copy(
        update={
            "correlation_id": correlation_id,
            "causation_id": source_causation_id,
        }
    )
    async with adapter.uow_factory(context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.outbox.append(outbox_intent)
        await uow.commit()

    consumer = IdempotentRecordingConsumer()
    consumers = OutboxConsumerRegistry()
    consumers.register("workspace_projection", consumer)
    diagnostic_events = DiagnosticEventRegistry.with_default_families()
    diagnostic_sink = RecordingDiagnosticSink(diagnostic_events)
    claimed_at = datetime(2026, 10, 12, 9, tzinfo=timezone.utc)
    completed_at = claimed_at + timedelta(seconds=1)
    clock = iter((claimed_at, completed_at)).__next__
    audit_event_id = synthetic_uuid(6605)
    dispatcher = OutboxDispatcher(
        repository=adapter.delivery_repository,
        consumers=consumers,
        audit_events=adapter.audit_events,
        diagnostic_events=diagnostic_events,
        diagnostic_sink=diagnostic_sink,
        service_principal_id=synthetic_uuid(6606),
        clock=clock,
        audit_event_id_source=lambda: audit_event_id,
        retry_delay=lambda attempt_number: timedelta(seconds=attempt_number * 5),
        maximum_retry_delay=timedelta(minutes=5),
        lease_duration=timedelta(seconds=30),
        handler_timeout=timedelta(seconds=5),
        maximum_attempts=3,
    )

    report = await dispatcher.dispatch_batch(limit=1, worker_identity="worker_alpha")

    assert report.claimed == 1
    assert report.delivered == 1
    assert report.retry_scheduled == 0
    assert report.quarantined == 0
    assert report.outcome_codes == (DispatchOutcomeCode.DELIVERED,)
    assert consumer.effects == {event_id}
    assert consumer.contexts == [
        DiagnosticContext(
            trace_id=context.trace_id,
            correlation_id=correlation_id,
            causation_id=event_id,
        )
    ]
    delivery = await adapter.delivery_repository.resolve(event_id)
    assert delivery is not None
    assert delivery.status is OutboxDeliveryStatus.DELIVERED
    audit = await adapter.read_audit_event(
        adapter.worker_workspace_context(workspace_id),
        audit_event_id,
    )
    assert audit is not None
    assert audit.outcome is AuditOutcome.SUCCEEDED
    assert audit.trace_id == context.trace_id
    assert audit.correlation_id == correlation_id
    assert audit.causation_id == event_id
    assert len(diagnostic_sink.events) == 1
    diagnostic = diagnostic_sink.events[0]
    assert diagnostic.payload.delivery_state is OutboxDeliveryDiagnosticState.DELIVERED
    assert diagnostic.causation_id == event_id


@pytest.mark.asyncio
async def test_retryable_failure_clamps_injected_backoff_without_raw_error() -> None:
    adapter = await create_in_memory_adapter()
    workspace_id = synthetic_uuid(6610)
    event_id = synthetic_uuid(6611)
    context = adapter.workspace_context(workspace_id)
    async with adapter.uow_factory(context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.outbox.append(
            intent(
                adapter,
                workspace_id=workspace_id,
                event_id=event_id,
                trace_id=context.trace_id,
                producer_deduplication_id=None,
            )
        )
        await uow.commit()
    consumers = OutboxConsumerRegistry()
    consumers.register("workspace_projection", RetryableConsumer())
    diagnostic_events = DiagnosticEventRegistry.with_default_families()
    diagnostic_sink = RecordingDiagnosticSink(diagnostic_events)
    claimed_at = datetime(2026, 10, 12, 10, tzinfo=timezone.utc)
    failed_at = claimed_at + timedelta(seconds=1)
    audit_event_id = synthetic_uuid(6612)
    dispatcher = OutboxDispatcher(
        repository=adapter.delivery_repository,
        consumers=consumers,
        audit_events=adapter.audit_events,
        diagnostic_events=diagnostic_events,
        diagnostic_sink=diagnostic_sink,
        service_principal_id=synthetic_uuid(6613),
        clock=iter((claimed_at, failed_at)).__next__,
        audit_event_id_source=lambda: audit_event_id,
        retry_delay=lambda attempt_number: timedelta(hours=attempt_number),
        maximum_retry_delay=timedelta(seconds=5),
        lease_duration=timedelta(seconds=30),
        handler_timeout=timedelta(seconds=5),
        maximum_attempts=3,
    )

    report = await dispatcher.dispatch_batch(limit=1, worker_identity="worker_alpha")

    assert report.retry_scheduled == 1
    assert report.outcome_codes == (DispatchOutcomeCode.RETRY_SCHEDULED,)
    delivery = await adapter.delivery_repository.resolve(event_id)
    assert delivery is not None
    assert delivery.status is OutboxDeliveryStatus.RETRY_SCHEDULED
    assert delivery.next_attempt_at == failed_at + timedelta(seconds=5)
    assert delivery.last_error_code == "consumer.unavailable"
    audit = await adapter.read_audit_event(
        adapter.worker_workspace_context(workspace_id),
        audit_event_id,
    )
    assert audit is not None
    assert audit.outcome is AuditOutcome.RETRY_SCHEDULED
    assert audit.reason == "consumer.unavailable"
    assert len(diagnostic_sink.events) == 1
    assert (
        diagnostic_sink.events[0].payload.delivery_state
        is OutboxDeliveryDiagnosticState.RETRY_SCHEDULED
    )
    assert diagnostic_sink.events[0].payload.retry_delay == timedelta(seconds=5)


@pytest.mark.asyncio
async def test_terminal_failure_is_quarantined_with_safe_code() -> None:
    adapter = await create_in_memory_adapter()
    workspace_id = synthetic_uuid(6620)
    event_id = synthetic_uuid(6621)
    context = adapter.workspace_context(workspace_id)
    async with adapter.uow_factory(context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.outbox.append(
            intent(
                adapter,
                workspace_id=workspace_id,
                event_id=event_id,
                trace_id=context.trace_id,
                producer_deduplication_id=None,
            )
        )
        await uow.commit()
    consumers = OutboxConsumerRegistry()
    consumers.register("workspace_projection", TerminalConsumer())
    diagnostics = DiagnosticEventRegistry.with_default_families()
    dispatcher = OutboxDispatcher(
        repository=adapter.delivery_repository,
        consumers=consumers,
        audit_events=adapter.audit_events,
        diagnostic_events=diagnostics,
        diagnostic_sink=RecordingDiagnosticSink(diagnostics),
        service_principal_id=synthetic_uuid(6622),
        clock=iter(
            (
                datetime(2026, 10, 12, 11, tzinfo=timezone.utc),
                datetime(2026, 10, 12, 11, 0, 1, tzinfo=timezone.utc),
            )
        ).__next__,
        audit_event_id_source=lambda: synthetic_uuid(6623),
        retry_delay=lambda attempt_number: timedelta(seconds=attempt_number * 5),
        maximum_retry_delay=timedelta(minutes=5),
        lease_duration=timedelta(seconds=30),
        handler_timeout=timedelta(seconds=5),
        maximum_attempts=3,
    )

    report = await dispatcher.dispatch_batch(limit=1, worker_identity="worker_alpha")

    assert report.quarantined == 1
    assert report.outcome_codes == (DispatchOutcomeCode.QUARANTINED,)
    delivery = await adapter.delivery_repository.resolve(event_id)
    assert delivery is not None
    assert delivery.status is OutboxDeliveryStatus.QUARANTINED
    assert delivery.last_error_code == "consumer.rejected"


@pytest.mark.asyncio
async def test_retryable_failure_at_attempt_limit_is_quarantined() -> None:
    adapter = await create_in_memory_adapter()
    workspace_id = synthetic_uuid(6630)
    event_id = synthetic_uuid(6631)
    context = adapter.workspace_context(workspace_id)
    async with adapter.uow_factory(context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.outbox.append(
            intent(
                adapter,
                workspace_id=workspace_id,
                event_id=event_id,
                trace_id=context.trace_id,
                producer_deduplication_id=None,
            )
        )
        await uow.commit()
    consumers = OutboxConsumerRegistry()
    consumers.register("workspace_projection", RetryableConsumer())
    diagnostics = DiagnosticEventRegistry.with_default_families()
    base = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
    clock = iter(
        (
            base,
            base + timedelta(seconds=1),
            base + timedelta(seconds=6),
            base + timedelta(seconds=7),
        )
    ).__next__
    dispatcher = OutboxDispatcher(
        repository=adapter.delivery_repository,
        consumers=consumers,
        audit_events=adapter.audit_events,
        diagnostic_events=diagnostics,
        diagnostic_sink=RecordingDiagnosticSink(diagnostics),
        service_principal_id=synthetic_uuid(6632),
        clock=clock,
        audit_event_id_source=iter(
            (synthetic_uuid(6633), synthetic_uuid(6634))
        ).__next__,
        retry_delay=lambda attempt_number: timedelta(seconds=5),
        maximum_retry_delay=timedelta(minutes=5),
        lease_duration=timedelta(seconds=30),
        handler_timeout=timedelta(seconds=5),
        maximum_attempts=2,
    )

    first = await dispatcher.dispatch_batch(limit=1, worker_identity="worker_alpha")
    second = await dispatcher.dispatch_batch(limit=1, worker_identity="worker_alpha")

    assert first.retry_scheduled == 1
    assert second.quarantined == 1
    delivery = await adapter.delivery_repository.resolve(event_id)
    assert delivery is not None
    assert delivery.status is OutboxDeliveryStatus.QUARANTINED
    assert delivery.last_error_code == "delivery.attempts_exhausted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("consumer", "expected_status", "expected_code"),
    (
        (
            TimingOutConsumer(),
            OutboxDeliveryStatus.RETRY_SCHEDULED,
            "delivery.handler_timeout",
        ),
        (
            MismatchedContextConsumer(),
            OutboxDeliveryStatus.QUARANTINED,
            "delivery.context_mismatch",
        ),
    ),
)
async def test_timeout_or_context_mismatch_never_acknowledges_success(
    consumer: object,
    expected_status: OutboxDeliveryStatus,
    expected_code: str,
) -> None:
    adapter = await create_in_memory_adapter()
    workspace_id = synthetic_uuid(6640)
    event_id = synthetic_uuid(6641)
    context = adapter.workspace_context(workspace_id)
    async with adapter.uow_factory(context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.outbox.append(
            intent(
                adapter,
                workspace_id=workspace_id,
                event_id=event_id,
                trace_id=context.trace_id,
                producer_deduplication_id=None,
            )
        )
        await uow.commit()
    consumers = OutboxConsumerRegistry()
    consumers.register("workspace_projection", consumer)  # type: ignore[arg-type]
    diagnostics = DiagnosticEventRegistry.with_default_families()
    base = datetime(2026, 10, 12, 14, tzinfo=timezone.utc)
    dispatcher = OutboxDispatcher(
        repository=adapter.delivery_repository,
        consumers=consumers,
        audit_events=adapter.audit_events,
        diagnostic_events=diagnostics,
        diagnostic_sink=RecordingDiagnosticSink(diagnostics),
        service_principal_id=synthetic_uuid(6642),
        clock=iter((base, base + timedelta(seconds=1))).__next__,
        audit_event_id_source=lambda: synthetic_uuid(6643),
        retry_delay=lambda attempt_number: timedelta(seconds=5),
        maximum_retry_delay=timedelta(minutes=5),
        lease_duration=timedelta(seconds=30),
        handler_timeout=timedelta(milliseconds=1),
        maximum_attempts=3,
    )

    report = await dispatcher.dispatch_batch(limit=1, worker_identity="worker_alpha")

    assert report.delivered == 0
    delivery = await adapter.delivery_repository.resolve(event_id)
    assert delivery is not None
    assert delivery.status is expected_status
    assert delivery.last_error_code == expected_code


@pytest.mark.asyncio
async def test_unknown_route_is_quarantined_without_consumer_execution() -> None:
    adapter = await create_in_memory_adapter()
    adapter.outbox_events.register(
        event_type="workspace.unrouted",
        schema_version=1,
        payload_type=WorkspaceCreatedPayload,
    )
    workspace_id = synthetic_uuid(6650)
    event_id = synthetic_uuid(6651)
    context = adapter.workspace_context(workspace_id)
    known = intent(
        adapter,
        workspace_id=workspace_id,
        event_id=event_id,
        trace_id=context.trace_id,
        producer_deduplication_id=None,
    )
    unrouted = adapter.outbox_events.build_intent(
        event_id=event_id,
        workspace_id=workspace_id,
        event_type="workspace.unrouted",
        schema_version=1,
        payload=known.payload,
        trace_id=context.trace_id,
    )
    async with adapter.uow_factory(context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.outbox.append(unrouted)
        await uow.commit()
    diagnostics = DiagnosticEventRegistry.with_default_families()
    base = datetime(2026, 10, 12, 15, tzinfo=timezone.utc)
    dispatcher = OutboxDispatcher(
        repository=adapter.delivery_repository,
        consumers=OutboxConsumerRegistry(),
        audit_events=adapter.audit_events,
        diagnostic_events=diagnostics,
        diagnostic_sink=RecordingDiagnosticSink(diagnostics),
        service_principal_id=synthetic_uuid(6652),
        clock=iter((base, base + timedelta(seconds=1))).__next__,
        audit_event_id_source=lambda: synthetic_uuid(6653),
        retry_delay=lambda attempt_number: timedelta(seconds=5),
        maximum_retry_delay=timedelta(minutes=5),
        lease_duration=timedelta(seconds=30),
        handler_timeout=timedelta(seconds=5),
        maximum_attempts=3,
    )

    report = await dispatcher.dispatch_batch(limit=1, worker_identity="worker_alpha")

    assert report.quarantined == 1
    delivery = await adapter.delivery_repository.resolve(event_id)
    assert delivery is not None
    assert delivery.last_error_code == "delivery.unknown_route"


class FailBeforeFirstFinalization:
    def __init__(self, repository: object) -> None:
        self._repository = repository
        self._lost = False

    async def claim_due(self, **kwargs: object):
        return await self._repository.claim_due(**kwargs)  # type: ignore[attr-defined]

    async def finalize(self, claim: object, **kwargs: object):
        if not self._lost:
            self._lost = True
            raise ConnectionError("provider response containing protected detail")
        return await self._repository.finalize(  # type: ignore[attr-defined]
            claim,
            **kwargs,
        )


@pytest.mark.asyncio
async def test_acknowledgement_loss_redelivers_one_logical_consumer_effect() -> None:
    adapter = await create_in_memory_adapter()
    workspace_id = synthetic_uuid(6660)
    event_id = synthetic_uuid(6661)
    context = adapter.workspace_context(workspace_id)
    async with adapter.uow_factory(context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.outbox.append(
            intent(
                adapter,
                workspace_id=workspace_id,
                event_id=event_id,
                trace_id=context.trace_id,
                producer_deduplication_id=None,
            )
        )
        await uow.commit()
    consumer = IdempotentRecordingConsumer()
    consumers = OutboxConsumerRegistry()
    consumers.register("workspace_projection", consumer)
    diagnostics = DiagnosticEventRegistry.with_default_families()
    repository = FailBeforeFirstFinalization(adapter.delivery_repository)
    base = datetime(2026, 10, 12, 16, tzinfo=timezone.utc)
    dispatcher = OutboxDispatcher(
        repository=repository,  # type: ignore[arg-type]
        consumers=consumers,
        audit_events=adapter.audit_events,
        diagnostic_events=diagnostics,
        diagnostic_sink=RecordingDiagnosticSink(diagnostics),
        service_principal_id=synthetic_uuid(6662),
        clock=iter(
            (
                base,
                base + timedelta(seconds=1),
                base + timedelta(seconds=30),
                base + timedelta(seconds=31),
            )
        ).__next__,
        audit_event_id_source=iter(
            (synthetic_uuid(6663), synthetic_uuid(6664))
        ).__next__,
        retry_delay=lambda attempt_number: timedelta(seconds=5),
        maximum_retry_delay=timedelta(minutes=5),
        lease_duration=timedelta(seconds=30),
        handler_timeout=timedelta(seconds=5),
        maximum_attempts=3,
    )

    with pytest.raises(
        UnexpectedPersistenceError,
        match="Outbox dispatch persistence failed",
    ) as captured:
        await dispatcher.dispatch_batch(limit=1, worker_identity="worker_alpha")
    assert captured.value.__cause__ is None
    assert "protected detail" not in str(captured.value)
    report = await dispatcher.dispatch_batch(limit=1, worker_identity="worker_beta")

    assert report.delivered == 1
    assert consumer.effects == {event_id}
    assert len(consumer.contexts) == 2

    delivery = await adapter.delivery_repository.resolve(event_id)
    assert delivery is not None
    assert delivery.attempt_count == 2
    assert delivery.status is OutboxDeliveryStatus.DELIVERED


@pytest.mark.asyncio
async def test_projection_request_redelivery_has_one_logical_consumer_effect() -> None:
    adapter = await create_in_memory_adapter()
    workspace_id = synthetic_uuid(6720)
    environment_id = synthetic_uuid(6721)
    event_id = synthetic_uuid(6722)
    await persist_workspace(adapter, workspace_id)
    await persist_environment(adapter, workspace_id, environment_id)
    context = adapter.environment_context(workspace_id, environment_id)
    projection_request = adapter.outbox_events.build_intent(
        event_id=event_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        event_type=PROJECTION_REQUEST_EVENT_TYPE,
        schema_version=PROJECTION_REQUEST_SCHEMA_VERSION,
        payload=ProjectionRequestPayload(
            ingestion_run_id=synthetic_uuid(6723),
            ingestion_run_source_id=synthetic_uuid(6724),
            source_revision_id=synthetic_uuid(6725),
            processing_receipt_id=synthetic_uuid(6726),
            processor_kind="document-ingestion",
            parser_version="text-markdown:v1",
            parser_config_reference="text-markdown-default:v1",
            locator_contract_version="physical-line-codepoint:v1",
        ),
        producer_deduplication_id=f"projection-request:{synthetic_uuid(6724)}",
        trace_id=context.trace_id,
        correlation_id=synthetic_uuid(6723),
        causation_id=synthetic_uuid(6727),
    )
    async with adapter.uow_factory(context) as uow:
        await uow.outbox.append(projection_request)
        await uow.commit()

    consumer = IdempotentRecordingConsumer()
    consumers = OutboxConsumerRegistry()
    consumers.register("projection_request", consumer)
    repository = FailBeforeFirstFinalization(adapter.delivery_repository)
    operational_registry = OperationalObservationRegistry()
    operational_sink = RecordingOperationalObservationSink(operational_registry)
    operational_observer = OperationalObservationEmitter(
        registry=operational_registry,
        sink=operational_sink,
        policy=OperationalObservationPolicy(),
        clock=lambda: base + timedelta(seconds=31),
        observation_id_source=lambda: UUID(int=6729),
    )
    base = datetime(2026, 10, 12, 17, tzinfo=timezone.utc)
    dispatcher, _, _ = _build_dispatcher(
        adapter,
        consumers=consumers,
        repository=repository,  # type: ignore[arg-type]
        clock=iter(
            (
                base,
                base + timedelta(seconds=1),
                base + timedelta(seconds=30),
                base + timedelta(seconds=31),
            )
        ).__next__,
        audit_id_start=6728,
        operational_observer=operational_observer,
    )

    with pytest.raises(UnexpectedPersistenceError):
        await dispatcher.dispatch_batch(limit=1, worker_identity="worker_alpha")
    report = await dispatcher.dispatch_batch(limit=1, worker_identity="worker_beta")

    assert report.delivered == 1
    assert consumer.effects == {event_id}
    assert len(consumer.contexts) == 2
    assert [item.component for item in operational_sink.observations] == [
        OperationalComponent.OUTBOX,
        OperationalComponent.WORKER,
        OperationalComponent.OUTBOX,
        OperationalComponent.WORKER,
    ]
    assert {item.trace_id for item in operational_sink.observations} == {
        context.trace_id
    }


@pytest.mark.asyncio
async def test_concurrent_projection_request_dispatch_has_one_logical_consumer_effect() -> None:
    adapter = await create_in_memory_adapter()
    workspace_id = synthetic_uuid(6730)
    environment_id = synthetic_uuid(6731)
    event_id = synthetic_uuid(6732)
    await persist_workspace(adapter, workspace_id)
    await persist_environment(adapter, workspace_id, environment_id)
    context = adapter.environment_context(workspace_id, environment_id)
    projection_request = adapter.outbox_events.build_intent(
        event_id=event_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        event_type=PROJECTION_REQUEST_EVENT_TYPE,
        schema_version=PROJECTION_REQUEST_SCHEMA_VERSION,
        payload=ProjectionRequestPayload(
            ingestion_run_id=synthetic_uuid(6733),
            ingestion_run_source_id=synthetic_uuid(6734),
            source_revision_id=synthetic_uuid(6735),
            processing_receipt_id=synthetic_uuid(6736),
            processor_kind="document-ingestion",
            parser_version="text-markdown:v1",
            parser_config_reference="text-markdown-default:v1",
            locator_contract_version="physical-line-codepoint:v1",
        ),
        producer_deduplication_id=f"projection-request:{synthetic_uuid(6734)}",
        trace_id=context.trace_id,
        correlation_id=synthetic_uuid(6733),
        causation_id=synthetic_uuid(6737),
    )
    async with adapter.uow_factory(context) as uow:
        await uow.outbox.append(projection_request)
        await uow.commit()

    consumer = IdempotentRecordingConsumer()
    consumers = OutboxConsumerRegistry()
    consumers.register("projection_request", consumer)
    dispatcher, _, _ = _build_dispatcher(
        adapter,
        consumers=consumers,
        clock=lambda: datetime(2026, 10, 12, 18, tzinfo=timezone.utc),
        audit_id_start=6738,
    )

    reports = await asyncio.gather(
        dispatcher.dispatch_batch(limit=1, worker_identity="worker_concurrent_a"),
        dispatcher.dispatch_batch(limit=1, worker_identity="worker_concurrent_b"),
    )

    assert sum(value.claimed for value in reports) == 1
    assert sum(value.delivered for value in reports) == 1
    assert consumer.effects == {event_id}


class BarrierConsumer(IdempotentRecordingConsumer):
    def __init__(self, expected: int) -> None:
        super().__init__()
        self._expected = expected
        self._started = 0
        self._all_started = asyncio.Event()

    async def consume(
        self,
        intent: OutboxIntent,
        diagnostic_context: DiagnosticContext,
    ) -> OutboxConsumerSuccess:
        self._started += 1
        if self._started == self._expected:
            self._all_started.set()
        await self._all_started.wait()
        return await super().consume(intent, diagnostic_context)


@pytest.mark.asyncio
async def test_bounded_batch_starts_handlers_within_the_same_lease_window() -> None:
    adapter = await create_in_memory_adapter()
    workspace_id = synthetic_uuid(6670)
    event_ids = (synthetic_uuid(6671), synthetic_uuid(6672))
    await _append_events(adapter, workspace_id=workspace_id, event_ids=event_ids)
    consumer = BarrierConsumer(expected=2)
    consumers = OutboxConsumerRegistry()
    consumers.register("workspace_projection", consumer)
    base = datetime(2026, 10, 12, 17, tzinfo=timezone.utc)
    dispatcher, _, _ = _build_dispatcher(
        adapter,
        consumers=consumers,
        clock=lambda: base,
        audit_id_start=6674,
        handler_timeout=timedelta(milliseconds=100),
    )

    report = await dispatcher.dispatch_batch(limit=2, worker_identity="worker_alpha")

    assert report.claimed == 2
    assert report.delivered == 2
    assert consumer.effects == set(event_ids)


@pytest.mark.asyncio
async def test_unknown_consumer_is_quarantined_without_handler_execution() -> None:
    adapter = await create_in_memory_adapter()
    workspace_id = synthetic_uuid(6680)
    event_id = synthetic_uuid(6681)
    await _append_events(adapter, workspace_id=workspace_id, event_ids=(event_id,))
    base = datetime(2026, 10, 12, 18, tzinfo=timezone.utc)
    dispatcher, _, _ = _build_dispatcher(
        adapter,
        consumers=OutboxConsumerRegistry(),
        clock=lambda: base,
        audit_id_start=6683,
    )

    report = await dispatcher.dispatch_batch(limit=1, worker_identity="worker_alpha")

    assert report.outcome_codes == (DispatchOutcomeCode.QUARANTINED,)
    delivery = await adapter.delivery_repository.resolve(event_id)
    assert delivery is not None
    assert delivery.last_error_code == "delivery.unknown_consumer"


class SimulatedWorkerCrash(BaseException):
    pass


class CrashBeforeFirstHandlerRegistry(OutboxConsumerRegistry):
    def __init__(self) -> None:
        super().__init__()
        self._crashed = False

    def resolve_optional(self, consumer_name: str):
        if not self._crashed:
            self._crashed = True
            raise SimulatedWorkerCrash()
        return super().resolve_optional(consumer_name)


@pytest.mark.asyncio
async def test_crash_before_handler_recovers_after_lease_expiry() -> None:
    adapter = await create_in_memory_adapter()
    workspace_id = synthetic_uuid(6690)
    event_id = synthetic_uuid(6691)
    await _append_events(adapter, workspace_id=workspace_id, event_ids=(event_id,))
    consumer = IdempotentRecordingConsumer()
    consumers = CrashBeforeFirstHandlerRegistry()
    consumers.register("workspace_projection", consumer)
    base = datetime(2026, 10, 12, 19, tzinfo=timezone.utc)
    dispatcher, _, _ = _build_dispatcher(
        adapter,
        consumers=consumers,
        clock=iter(
            (base, base + timedelta(seconds=30), base + timedelta(seconds=31))
        ).__next__,
        audit_id_start=6693,
    )

    with pytest.raises(SimulatedWorkerCrash):
        await dispatcher.dispatch_batch(limit=1, worker_identity="worker_alpha")
    assert consumer.effects == set()

    report = await dispatcher.dispatch_batch(limit=1, worker_identity="worker_beta")

    assert report.delivered == 1
    assert consumer.effects == {event_id}
    delivery = await adapter.delivery_repository.resolve(event_id)
    assert delivery is not None
    assert delivery.attempt_count == 2


class LoseFirstFinalizationResponse:
    def __init__(self, repository: object) -> None:
        self._repository = repository
        self._lost = False

    async def claim_due(self, **kwargs: object):
        return await self._repository.claim_due(**kwargs)  # type: ignore[attr-defined]

    async def finalize(self, claim: object, **kwargs: object):
        result = await self._repository.finalize(  # type: ignore[attr-defined]
            claim,
            **kwargs,
        )
        if not self._lost:
            self._lost = True
            raise ConnectionError("provider response containing protected detail")
        return result


@pytest.mark.asyncio
async def test_response_loss_after_commit_does_not_redeliver() -> None:
    adapter = await create_in_memory_adapter()
    workspace_id = synthetic_uuid(6700)
    event_id = synthetic_uuid(6701)
    await _append_events(adapter, workspace_id=workspace_id, event_ids=(event_id,))
    consumer = IdempotentRecordingConsumer()
    consumers = OutboxConsumerRegistry()
    consumers.register("workspace_projection", consumer)
    base = datetime(2026, 10, 12, 20, tzinfo=timezone.utc)
    repository = LoseFirstFinalizationResponse(adapter.delivery_repository)
    dispatcher, diagnostic_sink, _ = _build_dispatcher(
        adapter,
        consumers=consumers,
        repository=repository,  # type: ignore[arg-type]
        clock=iter((base, base + timedelta(seconds=1), base + timedelta(seconds=30))).__next__,
        audit_id_start=6703,
    )

    with pytest.raises(UnexpectedPersistenceError) as captured:
        await dispatcher.dispatch_batch(limit=1, worker_identity="worker_alpha")
    assert captured.value.__cause__ is None
    assert "protected detail" not in str(captured.value)

    report = await dispatcher.dispatch_batch(limit=1, worker_identity="worker_beta")

    assert report.claimed == 0
    assert consumer.effects == {event_id}
    assert diagnostic_sink.events == ()
    delivery = await adapter.delivery_repository.resolve(event_id)
    assert delivery is not None
    assert delivery.status is OutboxDeliveryStatus.DELIVERED
    assert delivery.attempt_count == 1


class LeakingConsumer:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def consume(
        self,
        intent: OutboxIntent,
        diagnostic_context: DiagnosticContext,
    ) -> OutboxConsumerSuccess:
        del intent, diagnostic_context
        raise self._error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protected_failure",
    (
        RuntimeError("customer@example.test token=secret-1"),
        ValueError("provider payload: confidential-contract"),
    ),
)
async def test_unexpected_handler_failure_never_leaks_raw_detail(
    protected_failure: Exception,
) -> None:
    adapter = await create_in_memory_adapter()
    workspace_id = synthetic_uuid(6710)
    event_id = synthetic_uuid(6711)
    await _append_events(adapter, workspace_id=workspace_id, event_ids=(event_id,))
    consumers = OutboxConsumerRegistry()
    consumers.register("workspace_projection", LeakingConsumer(protected_failure))
    base = datetime(2026, 10, 12, 21, tzinfo=timezone.utc)
    dispatcher, diagnostic_sink, audit_ids = _build_dispatcher(
        adapter,
        consumers=consumers,
        clock=lambda: base,
        audit_id_start=6713,
    )

    report = await dispatcher.dispatch_batch(limit=1, worker_identity="worker_alpha")

    delivery = await adapter.delivery_repository.resolve(event_id)
    assert delivery is not None
    audit = await adapter.read_audit_event(
        adapter.worker_workspace_context(workspace_id),
        audit_ids[0],
    )
    assert audit is not None
    visible = " ".join(
        (
            report.model_dump_json(),
            delivery.model_dump_json(),
            audit.model_dump_json(),
            diagnostic_sink.events[0].model_dump_json(),
        )
    )
    assert str(protected_failure) not in visible
    assert delivery.last_error_code == "delivery.handler_unexpected"
