from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from spine.application.diagnostics import DiagnosticContext, DiagnosticEventRegistry
from spine.application.outbox import OutboxConsumerSuccess
from spine.application.persistence.delivery import OutboxDeliveryStatus
from spine.application.persistence.outbox import OutboxIntent
from spine.infrastructure.diagnostics import RecordingDiagnosticSink
from spine.workers import OutboxConsumerBinding, create_outbox_worker
from tests.contracts.persistence.conftest import create_in_memory_adapter
from tests.contracts.persistence.ids import synthetic_uuid
from tests.contracts.persistence.test_environment_repository_contract import environment
from tests.contracts.persistence.test_outbox_writer_contract import intent, workspace


class RecordingConsumer:
    def __init__(self) -> None:
        self.intents: list[OutboxIntent] = []
        self.contexts: list[DiagnosticContext] = []

    async def consume(
        self,
        intent: OutboxIntent,
        diagnostic_context: DiagnosticContext,
    ) -> OutboxConsumerSuccess:
        self.intents.append(intent)
        self.contexts.append(diagnostic_context)
        return OutboxConsumerSuccess(diagnostic_context=diagnostic_context)


class MismatchedContextConsumer:
    async def consume(
        self,
        intent: OutboxIntent,
        diagnostic_context: DiagnosticContext,
    ) -> OutboxConsumerSuccess:
        del intent
        return OutboxConsumerSuccess(
            diagnostic_context=diagnostic_context.nested(synthetic_uuid(6799))
        )


async def test_worker_factory_reconstructs_trusted_intent_context() -> None:
    adapter = await create_in_memory_adapter()
    workspace_id = synthetic_uuid(6701)
    environment_id = synthetic_uuid(6702)
    event_id = synthetic_uuid(6703)
    correlation_id = synthetic_uuid(6705)
    source_causation_id = synthetic_uuid(6706)
    workspace_context = adapter.workspace_context(workspace_id)
    async with adapter.uow_factory(workspace_context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.environments.add(environment(workspace_id, environment_id))
        await uow.commit()

    context = adapter.environment_context(workspace_id, environment_id)
    trace_id = context.trace_id
    outbox_intent = intent(
        adapter,
        workspace_id=workspace_id,
        event_id=event_id,
        trace_id=trace_id,
        producer_deduplication_id=None,
    ).model_copy(
        update={
            "environment_id": environment_id,
            "correlation_id": correlation_id,
            "causation_id": source_causation_id,
        }
    )
    async with adapter.uow_factory(context) as uow:
        await uow.outbox.append(outbox_intent)
        await uow.commit()

    consumer = RecordingConsumer()
    diagnostic_events = DiagnosticEventRegistry.with_default_families()
    diagnostic_sink = RecordingDiagnosticSink(diagnostic_events)
    claimed_at = datetime(2026, 10, 11, 13, tzinfo=timezone.utc)
    completed_at = claimed_at + timedelta(seconds=1)
    worker = create_outbox_worker(
        repository=adapter.delivery_repository,
        consumer_bindings=(
            OutboxConsumerBinding(
                consumer_name="workspace_projection",
                consumer=consumer,
            ),
        ),
        audit_events=adapter.audit_events,
        diagnostic_events=diagnostic_events,
        diagnostic_sink=diagnostic_sink,
        service_principal_id=synthetic_uuid(6707),
        clock=iter((claimed_at, claimed_at, completed_at, completed_at)).__next__,
        audit_event_id_source=lambda: synthetic_uuid(6708),
        retry_delay=lambda attempt: timedelta(seconds=attempt),
        maximum_retry_delay=timedelta(minutes=5),
        lease_duration=timedelta(seconds=30),
        handler_timeout=timedelta(seconds=5),
        maximum_attempts=3,
    )

    report = await worker.dispatch_batch(limit=1, worker_identity="worker_alpha")

    assert report.delivered == 1
    assert len(consumer.intents) == 1
    assert consumer.intents[0].workspace_id == workspace_id
    assert consumer.intents[0].environment_id == environment_id
    assert consumer.contexts == [
        DiagnosticContext(
            trace_id=trace_id,
            correlation_id=correlation_id,
            causation_id=event_id,
        )
    ]


async def test_worker_quarantines_consumer_context_substitution() -> None:
    adapter = await create_in_memory_adapter()
    workspace_id = synthetic_uuid(6711)
    event_id = synthetic_uuid(6712)
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

    diagnostics = DiagnosticEventRegistry.with_default_families()
    worker = create_outbox_worker(
        repository=adapter.delivery_repository,
        consumer_bindings=(
            OutboxConsumerBinding(
                consumer_name="workspace_projection",
                consumer=MismatchedContextConsumer(),
            ),
        ),
        audit_events=adapter.audit_events,
        diagnostic_events=diagnostics,
        diagnostic_sink=RecordingDiagnosticSink(diagnostics),
        service_principal_id=synthetic_uuid(6713),
        clock=iter(
            (
                datetime(2026, 10, 11, 13, 10, tzinfo=timezone.utc),
                datetime(2026, 10, 11, 13, 10, 1, tzinfo=timezone.utc),
            )
        ).__next__,
        audit_event_id_source=lambda: synthetic_uuid(6714),
        retry_delay=lambda attempt: timedelta(seconds=attempt),
        maximum_retry_delay=timedelta(minutes=5),
        lease_duration=timedelta(seconds=30),
        handler_timeout=timedelta(seconds=5),
        maximum_attempts=3,
    )

    report = await worker.dispatch_batch(limit=1, worker_identity="worker_alpha")

    assert report.quarantined == 1
    delivery = await adapter.delivery_repository.resolve(event_id)
    assert delivery is not None
    assert delivery.status is OutboxDeliveryStatus.QUARANTINED
    assert delivery.last_error_code == "delivery.context_mismatch"
