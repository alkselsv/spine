from __future__ import annotations

from datetime import datetime, timedelta, timezone
import asyncio

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
    OutboxDispatcher,
    RetryableOutboxConsumerFailure,
    TerminalOutboxConsumerFailure,
)
from spine.application.persistence.delivery import OutboxDeliveryStatus
from spine.application.persistence.outbox import OutboxIntent
from spine.domain.audit import AuditOutcome
from spine.infrastructure.diagnostics import RecordingDiagnosticSink
from tests.contracts.persistence.conftest import create_in_memory_adapter
from tests.contracts.persistence.ids import synthetic_uuid
from tests.contracts.persistence.test_outbox_writer_contract import intent, workspace
from tests.contracts.persistence.outbox_events import WorkspaceCreatedPayload


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
async def test_retryable_failure_schedules_injected_backoff_without_raw_error() -> None:
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
        retry_delay=lambda attempt_number: timedelta(seconds=attempt_number * 5),
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
        lease_duration=timedelta(seconds=30),
        handler_timeout=timedelta(seconds=5),
        maximum_attempts=3,
    )

    report = await dispatcher.dispatch_batch(limit=1, worker_identity="worker_alpha")

    assert report.quarantined == 1
    delivery = await adapter.delivery_repository.resolve(event_id)
    assert delivery is not None
    assert delivery.last_error_code == "delivery.unknown_route"


class LoseFirstFinalization:
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
    repository = LoseFirstFinalization(adapter.delivery_repository)
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
        lease_duration=timedelta(seconds=30),
        handler_timeout=timedelta(seconds=5),
        maximum_attempts=3,
    )

    with pytest.raises(ConnectionError):
        await dispatcher.dispatch_batch(limit=1, worker_identity="worker_alpha")
    report = await dispatcher.dispatch_batch(limit=1, worker_identity="worker_beta")

    assert report.delivered == 1
    assert consumer.effects == {event_id}
    assert len(consumer.contexts) == 2
    delivery = await adapter.delivery_repository.resolve(event_id)
    assert delivery is not None
    assert delivery.attempt_count == 2
    assert delivery.status is OutboxDeliveryStatus.DELIVERED
