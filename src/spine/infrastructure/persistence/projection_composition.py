"""Production composition for committed projection-request delivery."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from spine.application.diagnostics import (
    AuditEventRegistry,
    DiagnosticEventRegistry,
    DiagnosticSink,
)
from spine.application.outbox import OutboxDispatchRepository
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.parsed_artifacts import ParsedArtifactRepository
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.application.projection_request_execution import (
    AdapterProjectionReceiptBuilder,
    PersistenceCanonicalProjectionRequestResolver,
    ProjectionRequestConsumer,
    ProjectionRequestEnvelope,
    ProjectionResultRecorder,
)
from spine.application.projection_result_persistence import (
    ParsedArtifactCanonicalProjectionLocatorResolver,
    build_projection_result_persistence,
)
from spine.memory.contracts import ContextProjection, ProjectionCommand
from spine.workers import OutboxConsumerBinding, OutboxWorker, create_outbox_worker


def build_production_projection_request_consumer(
    *,
    uow_factory: UnitOfWorkFactory,
    request_context_factory: Callable[
        [ProjectionRequestEnvelope], TrustedPersistenceContext
    ],
    projection_context_factory: Callable[
        [ProjectionCommand], TrustedPersistenceContext
    ],
    parsed_artifacts: ParsedArtifactRepository,
    projection: ContextProjection,
    receipt_builder: AdapterProjectionReceiptBuilder,
    reference_id_source: Callable[[], UUID] = uuid4,
) -> ProjectionRequestConsumer:
    """Compose the canonical resolver, physical adapter and result bridge.

    The returned consumer is intended to be registered as the existing
    ``projection_request`` outbox route.  It does not create a second worker or
    dispatcher.
    """

    canonical_resolver = PersistenceCanonicalProjectionRequestResolver(
        uow_factory=uow_factory,
        context_factory=request_context_factory,
    )
    locator_resolver = ParsedArtifactCanonicalProjectionLocatorResolver(
        uow_factory=uow_factory,
        context_factory=projection_context_factory,
        parsed_artifacts=parsed_artifacts,
    )
    result_bridge: ProjectionResultRecorder = build_projection_result_persistence(
        uow_factory=uow_factory,
        context_factory=projection_context_factory,
        locator_resolver=locator_resolver,
        reference_id_source=reference_id_source,
    )
    return ProjectionRequestConsumer(
        canonical_resolver=canonical_resolver,
        projection=projection,
        receipt_builder=receipt_builder,
        result_bridge=result_bridge,
    )


def build_production_projection_request_binding(
    *,
    uow_factory: UnitOfWorkFactory,
    request_context_factory: Callable[
        [ProjectionRequestEnvelope], TrustedPersistenceContext
    ],
    projection_context_factory: Callable[
        [ProjectionCommand], TrustedPersistenceContext
    ],
    parsed_artifacts: ParsedArtifactRepository,
    projection: ContextProjection,
    receipt_builder: AdapterProjectionReceiptBuilder,
    reference_id_source: Callable[[], UUID] = uuid4,
) -> OutboxConsumerBinding:
    """Register the production consumer on the existing outbox route."""

    consumer = build_production_projection_request_consumer(
        uow_factory=uow_factory,
        request_context_factory=request_context_factory,
        projection_context_factory=projection_context_factory,
        parsed_artifacts=parsed_artifacts,
        projection=projection,
        receipt_builder=receipt_builder,
        reference_id_source=reference_id_source,
    )
    return OutboxConsumerBinding(
        consumer_name="projection_request",
        consumer=consumer,
    )


def create_production_outbox_worker(
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
    uow_factory: UnitOfWorkFactory,
    request_context_factory: Callable[
        [ProjectionRequestEnvelope], TrustedPersistenceContext
    ],
    projection_context_factory: Callable[
        [ProjectionCommand], TrustedPersistenceContext
    ],
    parsed_artifacts: ParsedArtifactRepository,
    projection: ContextProjection,
    receipt_builder: AdapterProjectionReceiptBuilder,
    reference_id_source: Callable[[], UUID] = uuid4,
) -> OutboxWorker:
    """Build the existing outbox worker with one production projection route."""

    projection_binding = build_production_projection_request_binding(
        uow_factory=uow_factory,
        request_context_factory=request_context_factory,
        projection_context_factory=projection_context_factory,
        parsed_artifacts=parsed_artifacts,
        projection=projection,
        receipt_builder=receipt_builder,
        reference_id_source=reference_id_source,
    )
    return create_outbox_worker(
        repository=repository,
        consumer_bindings=(*consumer_bindings, projection_binding),
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


__all__ = [
    "build_production_projection_request_binding",
    "build_production_projection_request_consumer",
    "create_production_outbox_worker",
]
