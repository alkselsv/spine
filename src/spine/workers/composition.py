"""Trusted worker composition over the bounded outbox dispatcher."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from spine.application.diagnostics import (
    AuditEventRegistry,
    DiagnosticEventRegistry,
    DiagnosticSink,
)
from spine.application.outbox import (
    DispatchReport,
    OutboxConsumer,
    OutboxConsumerRegistry,
    OutboxDispatchRepository,
    OutboxDispatcher,
)


@dataclass(frozen=True, slots=True)
class OutboxConsumerBinding:
    """One explicit composition-root-owned consumer registration."""

    consumer_name: str
    consumer: OutboxConsumer


class OutboxWorker:
    """Process entry seam exposing only bounded dispatcher execution."""

    def __init__(self, dispatcher: OutboxDispatcher) -> None:
        self._dispatcher = dispatcher

    async def dispatch_batch(
        self,
        *,
        limit: int,
        worker_identity: str,
    ) -> DispatchReport:
        return await self._dispatcher.dispatch_batch(
            limit=limit,
            worker_identity=worker_identity,
        )


def create_outbox_worker(
    *,
    repository: OutboxDispatchRepository,
    consumer_bindings: Iterable[OutboxConsumerBinding],
    audit_events: AuditEventRegistry,
    diagnostic_events: DiagnosticEventRegistry,
    diagnostic_sink: DiagnosticSink,
    service_principal_id: UUID,
    clock: Callable[[], datetime],
    audit_event_id_source: Callable[[], UUID],
    retry_delay: Callable[[int], timedelta],
    maximum_retry_delay: timedelta,
    lease_duration: timedelta,
    handler_timeout: timedelta,
    maximum_attempts: int,
) -> OutboxWorker:
    """Build one worker from explicit consumers and injected runtime services."""

    consumers = OutboxConsumerRegistry()
    for binding in consumer_bindings:
        if type(binding) is not OutboxConsumerBinding:
            raise TypeError("consumer bindings must be canonical values")
        consumers.register(binding.consumer_name, binding.consumer)
    dispatcher = OutboxDispatcher(
        repository=repository,
        consumers=consumers,
        audit_events=audit_events,
        diagnostic_events=diagnostic_events,
        diagnostic_sink=diagnostic_sink,
        service_principal_id=service_principal_id,
        clock=clock,
        audit_event_id_source=audit_event_id_source,
        retry_delay=retry_delay,
        maximum_retry_delay=maximum_retry_delay,
        lease_duration=lease_duration,
        handler_timeout=handler_timeout,
        maximum_attempts=maximum_attempts,
    )
    return OutboxWorker(dispatcher)


__all__ = ["OutboxConsumerBinding", "OutboxWorker", "create_outbox_worker"]
