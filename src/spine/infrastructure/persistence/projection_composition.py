"""Production composition for committed projection-request delivery."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from spine.application.diagnostics import (
    AuditEventRegistry,
    DiagnosticContext,
    DiagnosticEventRegistry,
    DiagnosticSink,
)
from spine.application.outbox import (
    OutboxConsumer,
    OutboxConsumerSuccess,
    OutboxDispatchRepository,
)
from spine.application.persistence.context import (
    TrustedContextVerifier,
    TrustedPersistenceContext,
)
from spine.application.persistence.delivery import OutboxDeliveryRouteRegistry
from spine.application.persistence.outbox import OutboxIntent
from spine.application.persistence.parsed_artifacts import ParsedArtifactRepository
from spine.application.persistence.projection import (
    PROJECTION_OUTBOX_SCHEMA_VERSION,
    PROJECTION_RECOVERY_REQUESTED_OUTBOX_EVENT_TYPE,
    PROJECTION_SUCCESSOR_REQUESTED_OUTBOX_EVENT_TYPE,
)
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.application.projection_request_execution import (
    AdapterProjectionReceiptBuilder,
    PersistenceCanonicalProjectionRequestResolver,
    ProjectionRequestConsumer,
    ProjectionRequestCompletion,
    ProjectionRequestEnvelope,
    ProjectionResultRecorder,
)
from spine.application.projection_result_persistence import (
    ParsedArtifactCanonicalProjectionLocatorResolver,
    build_projection_result_persistence,
)
from spine.application.projection_recovery_execution import (
    MappingProjectionRecoveryResultRecorder,
    PersistenceCanonicalProjectionRecoveryResolver,
    PersistenceProjectionRecoveryLifecycle,
    ProjectionRecoveryRequestConsumer,
    ProjectionRecoveryRequestEnvelope,
    ProjectionRecoveryResultRecorder,
)
from spine.application.projection_publication_faults import (
    ProjectionPublicationQualificationGuard,
)
from spine.application.telemetry import OperationalObservationEmitter
from spine.application.document_ingestion import (
    PROJECTION_REQUEST_EVENT_TYPE,
    PROJECTION_REQUEST_SCHEMA_VERSION,
)
from spine.memory.contracts import ContextProjection, ProjectionCommand
from spine.memory.cognee.retrieval.adapter import (
    CogneePublicStructuredRetrievalSdk,
    CogneeStructuredRetrievalSdk,
)
from spine.infrastructure.persistence.cognee_projection_receipt import (
    CogneeProjectionReceiptBuilder,
)
from spine.infrastructure.persistence.parsed_artifacts import (
    PostgreSQLParsedArtifactRepository,
)
from spine.workers import OutboxConsumerBinding, OutboxWorker, create_outbox_worker


PROJECTION_REQUEST_CONSUMER_NAME = "projection_request"


def build_postgresql_cognee_projection_request_consumer(
    *,
    session_factory: Callable[[], Any],
    parsed_artifact_context_factory: Callable[
        [UUID, UUID], TrustedPersistenceContext
    ],
    context_verifier: TrustedContextVerifier,
    uow_factory: UnitOfWorkFactory,
    request_context_factory: Callable[
        [ProjectionRequestEnvelope], TrustedPersistenceContext
    ],
    projection_context_factory: Callable[
        [ProjectionCommand], TrustedPersistenceContext
    ],
    projection: ContextProjection,
    clock: Callable[[], datetime],
    physical_reader: CogneeStructuredRetrievalSdk | None = None,
    reference_id_source: Callable[[], UUID] = uuid4,
    completion: ProjectionRequestCompletion | None = None,
) -> ProjectionRequestConsumer:
    """Compose PostgreSQL parsed artifacts and an observed Cognee receipt builder."""

    parsed_artifacts = PostgreSQLParsedArtifactRepository(
        session_factory,
        context_factory=parsed_artifact_context_factory,
        context_verifier=context_verifier,
    )
    receipt_builder = CogneeProjectionReceiptBuilder(
        physical_reader=physical_reader or CogneePublicStructuredRetrievalSdk(),
        parsed_artifacts=parsed_artifacts,
        uow_factory=uow_factory,
        context_factory=projection_context_factory,
        clock=clock,
    )
    return build_production_projection_request_consumer(
        uow_factory=uow_factory,
        request_context_factory=request_context_factory,
        projection_context_factory=projection_context_factory,
        parsed_artifacts=parsed_artifacts,
        projection=projection,
        receipt_builder=receipt_builder,
        reference_id_source=reference_id_source,
        completion=completion,
    )


def build_postgresql_cognee_projection_request_binding(
    *,
    session_factory: Callable[[], Any],
    parsed_artifact_context_factory: Callable[
        [UUID, UUID], TrustedPersistenceContext
    ],
    context_verifier: TrustedContextVerifier,
    uow_factory: UnitOfWorkFactory,
    request_context_factory: Callable[
        [ProjectionRequestEnvelope], TrustedPersistenceContext
    ],
    projection_context_factory: Callable[
        [ProjectionCommand], TrustedPersistenceContext
    ],
    projection: ContextProjection,
    clock: Callable[[], datetime],
    physical_reader: CogneeStructuredRetrievalSdk | None = None,
    reference_id_source: Callable[[], UUID] = uuid4,
    completion: ProjectionRequestCompletion | None = None,
) -> OutboxConsumerBinding:
    """Bind the concrete PostgreSQL/Cognee composition to the existing route."""

    consumer = build_postgresql_cognee_projection_request_consumer(
        session_factory=session_factory,
        parsed_artifact_context_factory=parsed_artifact_context_factory,
        context_verifier=context_verifier,
        uow_factory=uow_factory,
        request_context_factory=request_context_factory,
        projection_context_factory=projection_context_factory,
        projection=projection,
        clock=clock,
        physical_reader=physical_reader,
        reference_id_source=reference_id_source,
        completion=completion,
    )
    return OutboxConsumerBinding(
        consumer_name=PROJECTION_REQUEST_CONSUMER_NAME,
        consumer=consumer,
    )


def build_postgresql_cognee_projection_delivery_binding(
    *,
    session_factory: Callable[[], Any],
    parsed_artifact_context_factory: Callable[
        [UUID, UUID], TrustedPersistenceContext
    ],
    context_verifier: TrustedContextVerifier,
    uow_factory: UnitOfWorkFactory,
    request_context_factory: Callable[
        [ProjectionRequestEnvelope], TrustedPersistenceContext
    ],
    projection_context_factory: Callable[
        [ProjectionCommand], TrustedPersistenceContext
    ],
    recovery_context_factory: Callable[
        [ProjectionRecoveryRequestEnvelope], TrustedPersistenceContext
    ],
    projection: ContextProjection,
    clock: Callable[[], datetime],
    recovery_result_recorder: ProjectionRecoveryResultRecorder | None = None,
    physical_reader: CogneeStructuredRetrievalSdk | None = None,
    operational_observer: OperationalObservationEmitter | None = None,
    reference_id_source: Callable[[], UUID] = uuid4,
    completion: ProjectionRequestCompletion | None = None,
    parsed_artifacts: ParsedArtifactRepository | None = None,
    receipt_builder: AdapterProjectionReceiptBuilder | None = None,
    result_bridge: ProjectionResultRecorder | None = None,
    qualification_guard: ProjectionPublicationQualificationGuard | None = None,
) -> OutboxConsumerBinding:
    """Compose ordinary and recovery delivery with the owned PostgreSQL/Cognee adapters."""

    artifacts = parsed_artifacts or PostgreSQLParsedArtifactRepository(
        session_factory,
        context_factory=parsed_artifact_context_factory,
        context_verifier=context_verifier,
    )
    receipts = receipt_builder or CogneeProjectionReceiptBuilder(
        physical_reader=physical_reader or CogneePublicStructuredRetrievalSdk(),
        parsed_artifacts=artifacts,
        uow_factory=uow_factory,
        context_factory=projection_context_factory,
        clock=clock,
    )
    results = result_bridge or build_projection_result_persistence(
        uow_factory=uow_factory,
        context_factory=projection_context_factory,
        locator_resolver=ParsedArtifactCanonicalProjectionLocatorResolver(
            uow_factory=uow_factory,
            context_factory=projection_context_factory,
            parsed_artifacts=artifacts,
        ),
        reference_id_source=reference_id_source,
    )
    request_consumer = build_production_projection_request_consumer(
        uow_factory=uow_factory,
        request_context_factory=request_context_factory,
        projection_context_factory=projection_context_factory,
        parsed_artifacts=artifacts,
        projection=projection,
        receipt_builder=receipts,
        reference_id_source=reference_id_source,
        completion=completion,
        result_bridge=results,
    )
    recorder = recovery_result_recorder or MappingProjectionRecoveryResultRecorder(
        receipt_builder=receipts,
        result_bridge=results,
    )
    recovery_consumer = ProjectionRecoveryRequestConsumer(
        canonical_resolver=PersistenceCanonicalProjectionRecoveryResolver(
            uow_factory=uow_factory,
            context_factory=recovery_context_factory,
            clock=clock,
        ),
        projection=projection,
        result_recorder=recorder,
        completion=completion,
        lifecycle=PersistenceProjectionRecoveryLifecycle(
            uow_factory=uow_factory,
            context_factory=projection_context_factory,
            clock=clock,
        ),
        operational_observer=operational_observer,
        qualification_guard=qualification_guard,
    )
    return build_projection_delivery_route_binding(
        projection_request_consumer=request_consumer,
        recovery_request_consumer=recovery_consumer,
    )


def register_projection_delivery_routes(
    routes: OutboxDeliveryRouteRegistry,
) -> None:
    """Register both ingestion and operator projection handoffs."""

    routes.register(
        event_type=PROJECTION_REQUEST_EVENT_TYPE,
        schema_version=PROJECTION_REQUEST_SCHEMA_VERSION,
        consumer_name=PROJECTION_REQUEST_CONSUMER_NAME,
    )
    routes.register(
        event_type=PROJECTION_RECOVERY_REQUESTED_OUTBOX_EVENT_TYPE,
        schema_version=PROJECTION_OUTBOX_SCHEMA_VERSION,
        consumer_name=PROJECTION_REQUEST_CONSUMER_NAME,
    )
    routes.register(
        event_type=PROJECTION_SUCCESSOR_REQUESTED_OUTBOX_EVENT_TYPE,
        schema_version=PROJECTION_OUTBOX_SCHEMA_VERSION,
        consumer_name=PROJECTION_REQUEST_CONSUMER_NAME,
    )


class _ProjectionRequestRouteConsumer:
    """Dispatch both typed projection intents through one worker binding."""

    def __init__(
        self,
        *,
        projection_request_consumer: ProjectionRequestConsumer,
        recovery_request_consumer: ProjectionRecoveryRequestConsumer,
    ) -> None:
        self._projection_request_consumer = projection_request_consumer
        self._recovery_request_consumer = recovery_request_consumer

    async def consume(
        self,
        intent: OutboxIntent,
        diagnostic_context: DiagnosticContext,
    ) -> OutboxConsumerSuccess:
        if (
            intent.event_type
            in {
                PROJECTION_RECOVERY_REQUESTED_OUTBOX_EVENT_TYPE,
                PROJECTION_SUCCESSOR_REQUESTED_OUTBOX_EVENT_TYPE,
            }
            and intent.schema_version == PROJECTION_OUTBOX_SCHEMA_VERSION
        ):
            return await self._recovery_request_consumer.consume(
                intent, diagnostic_context
            )
        return await self._projection_request_consumer.consume(
            intent, diagnostic_context
        )


class _PreparingProjectionRouteConsumer:
    """Prepare only ordinary projection requests before canonical resolution."""

    def __init__(
        self,
        *,
        consumer: OutboxConsumer,
        preparer: Callable[[OutboxIntent], Awaitable[None]],
    ) -> None:
        self._consumer = consumer
        self._preparer = preparer

    async def consume(
        self,
        intent: OutboxIntent,
        diagnostic_context: DiagnosticContext,
    ) -> OutboxConsumerSuccess:
        if (
            intent.event_type == PROJECTION_REQUEST_EVENT_TYPE
            and intent.schema_version == PROJECTION_REQUEST_SCHEMA_VERSION
        ):
            await self._preparer(intent)
        return await self._consumer.consume(intent, diagnostic_context)


def build_projection_delivery_route_binding(
    *,
    projection_request_consumer: OutboxConsumer,
    recovery_request_consumer: OutboxConsumer,
) -> OutboxConsumerBinding:
    """Bind ordinary and recovery consumers to the canonical projection route."""

    return OutboxConsumerBinding(
        consumer_name=PROJECTION_REQUEST_CONSUMER_NAME,
        consumer=_ProjectionRequestRouteConsumer(
            projection_request_consumer=projection_request_consumer,
            recovery_request_consumer=recovery_request_consumer,
        ),
    )


def prepare_projection_delivery_binding(
    binding: OutboxConsumerBinding,
    preparer: Callable[[OutboxIntent], Awaitable[None]],
) -> OutboxConsumerBinding:
    """Prepare ordinary requests once while leaving recovery intents untouched."""

    return OutboxConsumerBinding(
        consumer_name=binding.consumer_name,
        consumer=_PreparingProjectionRouteConsumer(
            consumer=binding.consumer,
            preparer=preparer,
        ),
    )


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
    completion: ProjectionRequestCompletion | None = None,
    result_bridge: ProjectionResultRecorder | None = None,
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
    results = result_bridge or build_projection_result_persistence(
        uow_factory=uow_factory,
        context_factory=projection_context_factory,
        locator_resolver=locator_resolver,
        reference_id_source=reference_id_source,
    )
    return ProjectionRequestConsumer(
        canonical_resolver=canonical_resolver,
        projection=projection,
        receipt_builder=receipt_builder,
        result_bridge=results,
        completion=completion,
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
        consumer_name=PROJECTION_REQUEST_CONSUMER_NAME,
        consumer=consumer,
    )


def build_production_projection_delivery_binding(
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
    recovery_context_factory: Callable[
        [ProjectionRecoveryRequestEnvelope], TrustedPersistenceContext
    ],
    recovery_result_recorder: ProjectionRecoveryResultRecorder,
    clock: Callable[[], datetime],
    operational_observer: OperationalObservationEmitter | None = None,
    reference_id_source: Callable[[], UUID] = uuid4,
) -> OutboxConsumerBinding:
    """Compose ordinary and recovery intents on ``projection_request``."""

    request_consumer = build_production_projection_request_consumer(
        uow_factory=uow_factory,
        request_context_factory=request_context_factory,
        projection_context_factory=projection_context_factory,
        parsed_artifacts=parsed_artifacts,
        projection=projection,
        receipt_builder=receipt_builder,
        reference_id_source=reference_id_source,
    )
    recovery_consumer = ProjectionRecoveryRequestConsumer(
        canonical_resolver=PersistenceCanonicalProjectionRecoveryResolver(
            uow_factory=uow_factory,
            context_factory=recovery_context_factory,
            clock=clock,
        ),
        projection=projection,
        result_recorder=recovery_result_recorder,
        lifecycle=PersistenceProjectionRecoveryLifecycle(
            uow_factory=uow_factory,
            context_factory=projection_context_factory,
            clock=clock,
        ),
        operational_observer=operational_observer,
    )
    return build_projection_delivery_route_binding(
        projection_request_consumer=request_consumer,
        recovery_request_consumer=recovery_consumer,
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
    session_factory: Callable[[], Any],
    uow_factory: UnitOfWorkFactory,
    request_context_factory: Callable[
        [ProjectionRequestEnvelope], TrustedPersistenceContext
    ],
    projection_context_factory: Callable[
        [ProjectionCommand], TrustedPersistenceContext
    ],
    parsed_artifact_context_factory: Callable[
        [UUID, UUID], TrustedPersistenceContext
    ],
    context_verifier: TrustedContextVerifier,
    projection: ContextProjection,
    recovery_context_factory: Callable[
        [ProjectionRecoveryRequestEnvelope], TrustedPersistenceContext
    ],
    physical_reader: CogneeStructuredRetrievalSdk | None = None,
    operational_observer: OperationalObservationEmitter | None = None,
    reference_id_source: Callable[[], UUID] = uuid4,
    recovery_result_recorder: ProjectionRecoveryResultRecorder | None = None,
    projection_request_completion: ProjectionRequestCompletion | None = None,
    projection_request_preparer: Callable[
        [OutboxIntent], Awaitable[None]
    ] | None = None,
    projection_qualification_guard: ProjectionPublicationQualificationGuard
    | None = None,
) -> OutboxWorker:
    """Build the existing outbox worker with one production projection route."""

    projection_binding = build_postgresql_cognee_projection_delivery_binding(
        session_factory=session_factory,
        parsed_artifact_context_factory=parsed_artifact_context_factory,
        context_verifier=context_verifier,
        uow_factory=uow_factory,
        request_context_factory=request_context_factory,
        projection_context_factory=projection_context_factory,
        projection=projection,
        recovery_context_factory=recovery_context_factory,
        clock=clock,
        physical_reader=physical_reader,
        operational_observer=operational_observer,
        reference_id_source=reference_id_source,
        recovery_result_recorder=recovery_result_recorder,
        completion=projection_request_completion,
        qualification_guard=projection_qualification_guard,
    )
    if projection_request_preparer is not None:
        projection_binding = prepare_projection_delivery_binding(
            projection_binding,
            projection_request_preparer,
        )
    return create_outbox_worker(
        repository=repository,
        consumer_bindings=(
            *consumer_bindings,
            projection_binding,
        ),
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
        operational_observer=operational_observer,
    )


__all__ = [
    "PROJECTION_REQUEST_CONSUMER_NAME",
    "build_postgresql_cognee_projection_delivery_binding",
    "build_postgresql_cognee_projection_request_binding",
    "build_postgresql_cognee_projection_request_consumer",
    "build_projection_delivery_route_binding",
    "build_production_projection_delivery_binding",
    "build_production_projection_request_binding",
    "build_production_projection_request_consumer",
    "create_production_outbox_worker",
    "prepare_projection_delivery_binding",
    "register_projection_delivery_routes",
]
