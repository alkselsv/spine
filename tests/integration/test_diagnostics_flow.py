from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
from fastapi import Depends
from pydantic import BaseModel, ConfigDict

from spine.api.main import create_app, get_diagnostic_context
from spine.application.diagnostics import DiagnosticContext, DiagnosticEventRegistry
from spine.application.outbox import OutboxConsumerSuccess
from spine.application.persistence.outbox import OutboxIntent
from spine.infrastructure.diagnostics import RecordingDiagnosticSink
from spine.workers import OutboxConsumerBinding, create_outbox_worker
from tests.contracts.persistence.conftest import create_in_memory_adapter
from tests.contracts.persistence.ids import synthetic_uuid
from tests.contracts.persistence.test_outbox_writer_contract import intent, workspace
from tests.contracts.diagnostics.fixtures import (
    assert_downstream_diagnostic_context,
    assert_no_diagnostic_leaks,
)


NOW = datetime(2026, 10, 11, 14, tzinfo=timezone.utc)


class SyntheticOutboxCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_id: UUID | None = None
    workspace_id: UUID | None = None
    environment_id: UUID | None = None
    actor_id: UUID | None = None
    protected_value: str | None = None


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


async def test_synthetic_asgi_to_worker_flow_preserves_trace_without_leaks() -> None:
    adapter = await create_in_memory_adapter()
    workspace_id = synthetic_uuid(6751)
    event_id = synthetic_uuid(6752)
    trusted_context = adapter.workspace_context(workspace_id)
    trace_id = trusted_context.trace_id
    untrusted_trace_id = synthetic_uuid(6753)
    untrusted_workspace_id = synthetic_uuid(6756)
    untrusted_environment_id = synthetic_uuid(6757)
    untrusted_actor_id = synthetic_uuid(6758)
    protected_value = "Bearer production-token protected customer document excerpt"
    async with adapter.uow_factory(trusted_context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.commit()

    diagnostic_events = DiagnosticEventRegistry.with_default_families()
    diagnostic_sink = RecordingDiagnosticSink(diagnostic_events)
    application = create_app(
        trace_id_source=lambda: trace_id,
        diagnostic_registry=diagnostic_events,
        diagnostic_sink=diagnostic_sink,
        clock=lambda: NOW,
    )

    @application.post("/api/v1/synthetic-outbox-command", status_code=202)
    async def submit_command(
        command: SyntheticOutboxCommand,
        diagnostic_context: DiagnosticContext = Depends(get_diagnostic_context),
    ) -> dict[str, str]:
        del command
        context = adapter.workspace_context(workspace_id)
        assert context.trace_id == diagnostic_context.trace_id
        async with adapter.uow_factory(context) as uow:
            await uow.outbox.append(
                intent(
                    adapter,
                    workspace_id=workspace_id,
                    event_id=event_id,
                    trace_id=diagnostic_context.trace_id,
                    producer_deduplication_id=None,
                )
            )
            await uow.commit()
        return {"event_id": str(event_id)}

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/synthetic-outbox-command?trace_id=" + str(untrusted_trace_id),
            headers={"x-trace-id": str(untrusted_trace_id)},
            json={
                "trace_id": str(untrusted_trace_id),
                "workspace_id": str(untrusted_workspace_id),
                "environment_id": str(untrusted_environment_id),
                "actor_id": str(untrusted_actor_id),
                "protected_value": protected_value,
            },
        )

    consumer = RecordingConsumer()
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
        service_principal_id=synthetic_uuid(6754),
        clock=iter((NOW + timedelta(seconds=1), NOW + timedelta(seconds=2))).__next__,
        audit_event_id_source=lambda: synthetic_uuid(6755),
        retry_delay=lambda attempt: timedelta(seconds=attempt),
        maximum_retry_delay=timedelta(minutes=5),
        lease_duration=timedelta(seconds=30),
        handler_timeout=timedelta(seconds=5),
        maximum_attempts=3,
    )
    report = await worker.dispatch_batch(limit=1, worker_identity="worker_alpha")

    assert response.status_code == 202
    assert response.headers["x-trace-id"] == str(trace_id)
    assert response.json() == {"event_id": str(event_id)}
    assert report.delivered == 1
    assert len(consumer.contexts) == 1
    assert_downstream_diagnostic_context(
        consumer.contexts[0],
        trace_id=trace_id,
        causation_id=event_id,
    )
    assert consumer.intents[0].workspace_id == workspace_id
    assert {event.trace_id for event in diagnostic_sink.events} == {trace_id}
    payload_snapshot = consumer.intents[0].payload_snapshot
    assert payload_snapshot is not None
    assert_no_diagnostic_leaks(
        (
            response.text,
            *(event.model_dump_json() for event in diagnostic_sink.events),
        ),
        leak_corpus=(
            protected_value,
            str(untrusted_trace_id),
            str(untrusted_workspace_id),
            str(untrusted_environment_id),
            str(untrusted_actor_id),
            payload_snapshot.decode("utf-8"),
        ),
    )
