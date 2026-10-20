from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from uuid import UUID

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from spine.application.diagnostics.audit import AuditEventRegistry
from spine.application.diagnostics import (
    DiagnosticContext,
    DiagnosticEvent,
    DiagnosticEventRegistry,
)
from spine.application.outbox import (
    DispatchOutcomeCode,
    OutboxConsumerRegistry,
    OutboxConsumerSuccess,
    OutboxDispatcher,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedPersistenceContext,
    WorkspaceScope,
)
from spine.application.persistence.delivery import (
    OutboxDelivery,
    OutboxDeliveryRouteRegistry,
    StaleDeliveryLeaseError,
    UnresolvedOutboxIntent,
)
from spine.application.persistence.outbox import OutboxIntent
from spine.application.persistence.errors import AuditConflictError
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.projection import ProjectionRecoveryAction
from spine.application.projection_control import ProjectionRecoveryRequestService
from spine.domain.common import EnvironmentKind
from spine.domain.workspaces import Environment, Workspace
from spine.infrastructure.db.migrations import upgrade_database
from spine.infrastructure.db.operator import bootstrap_database_roles
from spine.infrastructure.db.settings import (
    POSTGRESQL_SEARCH_PATH_OPTIONS,
    MigrationDatabaseSettings,
    OperatorDatabaseSettings,
)
from spine.infrastructure.db.test_harness import TestDatabaseProvision
from spine.infrastructure.outbox import (
    PostgreSQLOutboxDeliveryInspection,
    PostgreSQLOutboxDeliveryRepository,
)
from spine.infrastructure.diagnostics import (
    JsonLoggingDiagnosticSink,
    RecordingDiagnosticSink,
)
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.persistence.postgresql import PostgreSQLPersistence
from spine.infrastructure.persistence.projection_composition import (
    PROJECTION_REQUEST_CONSUMER_NAME,
    register_projection_delivery_routes,
)
from tests.contracts.persistence.adapter import DeliveryPersistenceAdapter
from tests.contracts.persistence.ids import synthetic_uuid
from tests.contracts.persistence.outbox_events import create_outbox_event_registry
from tests.contracts.persistence.test_outbox_delivery_repository_contract import (
    test_acknowledge_requires_the_active_lease_and_is_terminal as contract_acknowledge,
    test_claim_commits_a_bounded_lease_before_delivery as contract_claim,
    test_concurrent_workers_cannot_claim_the_same_delivery as contract_concurrent_claim,
    test_claim_preserves_workspace_environment_and_trace_scope as contract_scope,
    test_expired_lease_is_reclaimed_and_stale_worker_cannot_finalize as contract_reclaim,
    test_materialize_creates_bounded_pending_control_state as contract_materialize,
    test_renew_rotates_lease_authority_before_expiry as contract_renew,
    test_retry_is_claimable_only_at_its_due_boundary as contract_retry,
    test_unknown_route_can_be_claimed_and_safely_quarantined as contract_quarantine,
)
from tests.contracts.persistence.test_outbox_writer_contract import intent, workspace
from tests.contracts.diagnostics.fixtures import (
    DIAGNOSTIC_LEAK_CORPUS,
    assert_no_diagnostic_leaks,
)


pytestmark = [
    pytest.mark.postgresql,
    pytest.mark.asyncio(loop_scope="session"),
]

MIGRATION_ROLE = "spine_migration"
RUNTIME_ROLE = "spine_runtime"
DISPATCHER_ROLE = "spine_dispatcher"
MIGRATION_PASSWORD = "migration-delivery-secret"
RUNTIME_PASSWORD = "runtime-delivery-secret"
DISPATCHER_PASSWORD = "dispatcher-delivery-secret"


@dataclass(frozen=True, slots=True)
class DeliveryDatabase:
    migration_engine: AsyncEngine
    runtime_engine: AsyncEngine
    dispatcher_engine: AsyncEngine


def _role_url(provision: TestDatabaseProvision, role: str, password: str) -> SecretStr:
    value = make_url(provision.url.get_secret_value()).set(
        username=role,
        password=password,
    )
    return SecretStr(value.render_as_string(hide_password=False))


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def delivery_database(
    postgresql_provision: TestDatabaseProvision,
) -> AsyncIterator[DeliveryDatabase]:
    operator = OperatorDatabaseSettings(
        url=postgresql_provision.url,
        database_name=postgresql_provision.target.database_name,
        migration_role=MIGRATION_ROLE,
        migration_password=MIGRATION_PASSWORD,
        runtime_role=RUNTIME_ROLE,
        runtime_password=RUNTIME_PASSWORD,
        dispatcher_role=DISPATCHER_ROLE,
        dispatcher_password=DISPATCHER_PASSWORD,
    )
    await bootstrap_database_roles(operator)
    migration_url = _role_url(
        postgresql_provision,
        MIGRATION_ROLE,
        MIGRATION_PASSWORD,
    )
    await asyncio.to_thread(
        upgrade_database,
        MigrationDatabaseSettings(
            url=migration_url,
            migration_role=MIGRATION_ROLE,
            runtime_role=RUNTIME_ROLE,
            dispatcher_role=DISPATCHER_ROLE,
        ),
    )
    migration_engine = create_async_engine(
        migration_url.get_secret_value(),
        hide_parameters=True,
    )
    common = {
        "pool_size": 5,
        "max_overflow": 0,
        "pool_reset_on_return": "rollback",
        "hide_parameters": True,
        "connect_args": {"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    }
    runtime_engine = create_async_engine(
        _role_url(
            postgresql_provision,
            RUNTIME_ROLE,
            RUNTIME_PASSWORD,
        ).get_secret_value(),
        **common,
    )
    dispatcher_engine = create_async_engine(
        _role_url(
            postgresql_provision,
            DISPATCHER_ROLE,
            DISPATCHER_PASSWORD,
        ).get_secret_value(),
        **common,
    )
    try:
        yield DeliveryDatabase(
            migration_engine=migration_engine,
            runtime_engine=runtime_engine,
            dispatcher_engine=dispatcher_engine,
        )
    finally:
        await runtime_engine.dispose()
        await dispatcher_engine.dispose()
        async with migration_engine.begin() as connection:
            await connection.execute(text("TRUNCATE TABLE spine.workspaces CASCADE"))
        await migration_engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def postgresql_delivery_adapter(
    delivery_database: DeliveryDatabase,
) -> DeliveryPersistenceAdapter:
    async with delivery_database.migration_engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE spine.workspaces CASCADE"))
    runtime_sessions = async_sessionmaker(
        delivery_database.runtime_engine,
        expire_on_commit=False,
        autoflush=False,
    )
    dispatcher_sessions = async_sessionmaker(
        delivery_database.dispatcher_engine,
        expire_on_commit=False,
        autoflush=False,
    )
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(6540),
        secret=b"issue-65-postgresql-delivery-contract",
    )
    outbox_events = create_outbox_event_registry()
    audit_events = AuditEventRegistry.with_default_families()
    routes = OutboxDeliveryRouteRegistry(version="integration.v1")
    routes.register(
        event_type="workspace.created",
        schema_version=1,
        consumer_name="workspace_projection",
    )
    register_projection_delivery_routes(routes)
    routes.seal()
    persistence = PostgreSQLPersistence(
        session_factory=runtime_sessions,
        context_verifier=boundary,
        outbox_events=outbox_events,
        audit_events=audit_events,
    )
    delivery_repository = PostgreSQLOutboxDeliveryRepository(
        session_factory=dispatcher_sessions,
        outbox_events=outbox_events,
        delivery_routes=routes,
        audit_events=audit_events,
        lease_token_factory=(
            synthetic_uuid(value) for value in range(6700, 6900)
        ).__next__,
    )

    def workspace_context(workspace_id: UUID) -> TrustedPersistenceContext:
        return boundary.interactive(
            scope=WorkspaceScope(workspace_id=workspace_id),
            acting_subject_id=synthetic_uuid(6541),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation("workspace_repository"),
            trace_id=synthetic_uuid(6542),
        )

    def worker_workspace_context(workspace_id: UUID) -> TrustedPersistenceContext:
        return boundary.worker(
            scope=WorkspaceScope(workspace_id=workspace_id),
            service_principal_id=synthetic_uuid(6543),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation("workspace_repository"),
            trace_id=synthetic_uuid(6544),
        )

    def environment_context(
        workspace_id: UUID,
        environment_id: UUID,
    ) -> TrustedPersistenceContext:
        return boundary.worker(
            scope=EnvironmentScope(
                workspace_id=workspace_id,
                environment_id=environment_id,
            ),
            service_principal_id=synthetic_uuid(6545),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation("environment_repository"),
            trace_id=synthetic_uuid(6546),
        )

    def ingestion_context(
        workspace_id: UUID,
        environment_id: UUID,
        operation: str,
    ) -> TrustedPersistenceContext:
        return boundary.worker(
            scope=EnvironmentScope(
                workspace_id=workspace_id,
                environment_id=environment_id,
            ),
            service_principal_id=synthetic_uuid(6547),
            purpose=PersistencePurpose("projection_recovery_test"),
            operation=PersistenceOperation(operation),
            trace_id=synthetic_uuid(6548),
        )

    @asynccontextmanager
    async def hold_transactions() -> AsyncIterator[None]:
        yield

    return DeliveryPersistenceAdapter(
        uow_factory=persistence.uow_factory,
        workspace_context=workspace_context,
        worker_workspace_context=worker_workspace_context,
        environment_context=environment_context,
        ingestion_context=ingestion_context,
        hold_transactions=hold_transactions,
        outbox_events=outbox_events,
        audit_events=audit_events,
        read_audit_event=persistence.audit_reader.resolve,
        fail_next_audit_append=lambda: None,
        delivery_repository=delivery_repository,
        delivery_routes=routes,
    )


async def test_postgresql_claim_commits_a_bounded_lease_before_delivery(
    postgresql_delivery_adapter: DeliveryPersistenceAdapter,
) -> None:
    await contract_claim(postgresql_delivery_adapter)


async def test_runtime_inspection_is_rls_scoped_safe_and_read_only(
    postgresql_delivery_adapter: DeliveryPersistenceAdapter,
    delivery_database: DeliveryDatabase,
) -> None:
    adapter = postgresql_delivery_adapter
    runtime_sessions = async_sessionmaker(
        delivery_database.runtime_engine,
        expire_on_commit=False,
        autoflush=False,
    )
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(6810),
        secret=b"issue-365-delivery-inspection-boundary",
    )
    persistence = PostgreSQLPersistence(
        session_factory=runtime_sessions,
        context_verifier=boundary,
        outbox_events=adapter.outbox_events,
        audit_events=adapter.audit_events,
    )
    tenant_scopes = (
        (synthetic_uuid(6811), synthetic_uuid(6812), synthetic_uuid(6813)),
        (synthetic_uuid(6814), synthetic_uuid(6815), synthetic_uuid(6816)),
    )
    created: list[tuple[UUID, UUID, UUID]] = []
    for workspace_id, environment_id, event_id in tenant_scopes:
        workspace_context = boundary.interactive(
            scope=WorkspaceScope(workspace_id=workspace_id),
            acting_subject_id=synthetic_uuid(6817),
            purpose=PersistencePurpose("outbox_inspection_test"),
            operation=PersistenceOperation("create_test_scope"),
            trace_id=synthetic_uuid(6818),
        )
        async with persistence.uow_factory(workspace_context) as uow:
            await uow.workspaces.add(
                Workspace(
                    id=workspace_id,
                    slug=f"outbox-inspection-{workspace_id.hex[-8:]}",
                    display_name="Inspection test workspace",
                )
            )
            await uow.environments.add(
                Environment(
                    id=environment_id,
                    workspace_id=workspace_id,
                    kind=EnvironmentKind.PRODUCTION,
                    display_name="Production",
                )
            )
            await uow.commit()
        context = boundary.interactive(
            scope=EnvironmentScope(
                workspace_id=workspace_id,
                environment_id=environment_id,
            ),
            acting_subject_id=synthetic_uuid(6819),
            purpose=PersistencePurpose("outbox_inspection_test"),
            operation=PersistenceOperation("append_test_intent"),
            trace_id=synthetic_uuid(6820),
        )
        async with persistence.uow_factory(context) as uow:
            await uow.outbox.append(
                intent(
                    adapter,
                    event_id=event_id,
                    workspace_id=workspace_id,
                    environment_id=environment_id,
                    producer_deduplication_id=None,
                    correlation_id=synthetic_uuid(6825 + len(created)),
                    trace_id=context.trace_id,
                )
            )
            await uow.commit()
        created.append((workspace_id, environment_id, event_id))

    claim_time = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
    claims = await adapter.delivery_repository.claim_due(
        limit=2,
        worker_identity="inspection_test_worker",
        claimed_at=claim_time,
        lease_duration=timedelta(seconds=30),
    )
    assert len(claims) == 2
    for claim in claims:
        await adapter.delivery_repository.quarantine(
            claim,
            quarantined_at=claim_time + timedelta(seconds=1),
            error_code="consumer.unavailable",
        )

    inspector = PostgreSQLOutboxDeliveryInspection(
        session_factory=runtime_sessions,
        context_verifier=boundary,
    )

    def inspection_context(workspace_id: UUID, environment_id: UUID):
        return boundary.interactive(
            scope=EnvironmentScope(
                workspace_id=workspace_id,
                environment_id=environment_id,
            ),
            acting_subject_id=synthetic_uuid(6821),
            purpose=PersistencePurpose("outbox_inspection"),
            operation=PersistenceOperation("inspect_quarantined_deliveries"),
            trace_id=synthetic_uuid(6822),
        )

    workspace_id, environment_id, event_id = created[0]
    async with delivery_database.migration_engine.connect() as connection:
        before = (
            await connection.execute(
                text(
                    "SELECT row_to_json(delivery)::text "
                    "FROM spine.outbox_deliveries AS delivery "
                    "WHERE event_id = :event_id"
                ),
                {"event_id": event_id},
            )
        ).scalar_one()
    page = await inspector.list_quarantined(
        context=inspection_context(workspace_id, environment_id),
        limit=10,
        after=None,
    )
    repeated = await inspector.list_quarantined(
        context=inspection_context(workspace_id, environment_id),
        limit=10,
        after=None,
    )
    async with delivery_database.migration_engine.connect() as connection:
        after = (
            await connection.execute(
                text(
                    "SELECT row_to_json(delivery)::text "
                    "FROM spine.outbox_deliveries AS delivery "
                    "WHERE event_id = :event_id"
                ),
                {"event_id": event_id},
            )
        ).scalar_one()

    assert page.state == "complete"
    assert len(page.items) == 1
    assert page.items[0].event_id == event_id
    assert page.items[0].workspace_id == workspace_id
    assert page.items[0].environment_id == environment_id
    assert page.items[0].correlation_id == synthetic_uuid(6825)
    assert repeated == page
    assert before == after
    runtime_session = runtime_sessions()
    try:
        with pytest.raises(ProgrammingError):
            async with runtime_session.begin():
                await runtime_session.execute(
                    text(
                        "SELECT payload FROM spine.outbox_intents "
                        "WHERE event_id = :event_id"
                    ),
                    {"event_id": event_id},
                )
    finally:
        await runtime_session.close()
    with pytest.raises(PermissionError):
        await inspector.list_quarantined(
            context=boundary.worker(
                scope=EnvironmentScope(
                    workspace_id=workspace_id,
                    environment_id=environment_id,
                ),
                service_principal_id=synthetic_uuid(6823),
                purpose=PersistencePurpose("outbox_inspection"),
                operation=PersistenceOperation("inspect_quarantined_deliveries"),
                trace_id=synthetic_uuid(6824),
            ),
            limit=10,
            after=None,
        )


async def test_postgresql_materialize_creates_bounded_pending_state(
    postgresql_delivery_adapter: DeliveryPersistenceAdapter,
) -> None:
    await contract_materialize(postgresql_delivery_adapter)


async def test_postgresql_acknowledge_requires_active_lease(
    postgresql_delivery_adapter: DeliveryPersistenceAdapter,
) -> None:
    await contract_acknowledge(postgresql_delivery_adapter)


async def test_postgresql_retry_obeys_due_boundary(
    postgresql_delivery_adapter: DeliveryPersistenceAdapter,
) -> None:
    await contract_retry(postgresql_delivery_adapter)


async def test_postgresql_expired_lease_rejects_stale_worker(
    postgresql_delivery_adapter: DeliveryPersistenceAdapter,
) -> None:
    await contract_reclaim(postgresql_delivery_adapter)


async def test_postgresql_unknown_route_is_quarantined(
    postgresql_delivery_adapter: DeliveryPersistenceAdapter,
) -> None:
    await contract_quarantine(postgresql_delivery_adapter)


async def test_postgresql_renew_rotates_lease(
    postgresql_delivery_adapter: DeliveryPersistenceAdapter,
) -> None:
    await contract_renew(postgresql_delivery_adapter)


async def test_postgresql_concurrent_workers_do_not_overlap_claims(
    postgresql_delivery_adapter: DeliveryPersistenceAdapter,
) -> None:
    await contract_concurrent_claim(postgresql_delivery_adapter)


async def test_postgresql_concurrent_transitions_accept_only_one_lease_result(
    postgresql_delivery_adapter: DeliveryPersistenceAdapter,
) -> None:
    adapter = postgresql_delivery_adapter
    workspace_id = synthetic_uuid(6554)
    event_id = synthetic_uuid(6555)
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
    claimed_at = datetime(2026, 10, 11, 19, 30, tzinfo=timezone.utc)
    claim = (
        await adapter.delivery_repository.claim_due(
            limit=1,
            worker_identity="worker_alpha",
            claimed_at=claimed_at,
            lease_duration=timedelta(seconds=30),
        )
    )[0]
    transition_at = claimed_at + timedelta(seconds=1)

    outcomes = await asyncio.gather(
        adapter.delivery_repository.acknowledge(
            claim,
            completed_at=transition_at,
        ),
        adapter.delivery_repository.reschedule(
            claim,
            failed_at=transition_at,
            next_attempt_at=transition_at + timedelta(seconds=10),
            error_code="consumer.unavailable",
        ),
        return_exceptions=True,
    )

    accepted = [
        outcome for outcome in outcomes if isinstance(outcome, OutboxDelivery)
    ]
    rejected = [
        outcome for outcome in outcomes if isinstance(outcome, StaleDeliveryLeaseError)
    ]
    assert len(accepted) == 1
    assert len(rejected) == 1
    assert await adapter.delivery_repository.resolve(event_id) == accepted[0]


async def test_postgresql_claim_preserves_tenant_and_trace_scope(
    postgresql_delivery_adapter: DeliveryPersistenceAdapter,
) -> None:
    await contract_scope(postgresql_delivery_adapter)


class _SuccessfulConsumer:
    async def consume(
        self,
        intent: OutboxIntent,
        diagnostic_context: DiagnosticContext,
    ) -> OutboxConsumerSuccess:
        del intent
        return OutboxConsumerSuccess(diagnostic_context=diagnostic_context)


class _RecordingConsumer(_SuccessfulConsumer):
    def __init__(self) -> None:
        self.event_ids: list[UUID] = []

    async def consume(
        self,
        intent: OutboxIntent,
        diagnostic_context: DiagnosticContext,
    ) -> OutboxConsumerSuccess:
        assert intent.event_id is not None
        self.event_ids.append(intent.event_id)
        return await super().consume(intent, diagnostic_context)


async def test_postgresql_recovery_request_replay_dispatches_one_tenant_scoped_intent(
    postgresql_delivery_adapter: DeliveryPersistenceAdapter,
) -> None:
    adapter = postgresql_delivery_adapter
    workspace_id = synthetic_uuid(6950)
    environment_id = synthetic_uuid(6951)
    async with adapter.uow_factory(adapter.workspace_context(workspace_id)) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.environments.add(
            Environment(
                id=environment_id,
                workspace_id=workspace_id,
                kind=EnvironmentKind.DEVELOPMENT,
                display_name="Projection recovery",
            )
        )
        await uow.commit()
    assert adapter.ingestion_context is not None
    context = adapter.ingestion_context(
        workspace_id,
        environment_id,
        "request_projection_recovery",
    )
    service = ProjectionRecoveryRequestService(
        uow_factory=adapter.uow_factory,
        outbox_events=adapter.outbox_events,
    )
    first = await service.request(
        context=context,
        projection_kind="context",
        action=ProjectionRecoveryAction.REBUILD,
        idempotency_key=IdempotencyKey("postgresql-projection-recovery-364"),
    )
    replay = await service.request(
        context=context,
        projection_kind="context",
        action=ProjectionRecoveryAction.REBUILD,
        idempotency_key=IdempotencyKey("postgresql-projection-recovery-364"),
    )
    consumer = _RecordingConsumer()
    consumers = OutboxConsumerRegistry()
    consumers.register(PROJECTION_REQUEST_CONSUMER_NAME, consumer)
    diagnostics = DiagnosticEventRegistry.with_default_families()
    dispatcher = OutboxDispatcher(
        repository=adapter.delivery_repository,
        consumers=consumers,
        audit_events=adapter.audit_events,
        diagnostic_events=diagnostics,
        diagnostic_sink=RecordingDiagnosticSink(diagnostics),
        service_principal_id=synthetic_uuid(6952),
        clock=iter(
            (
                datetime(2026, 10, 16, 13, tzinfo=timezone.utc),
                datetime(2026, 10, 16, 13, 0, 1, tzinfo=timezone.utc),
            )
        ).__next__,
        audit_event_id_source=lambda: synthetic_uuid(6953),
        retry_delay=lambda attempt: timedelta(seconds=attempt),
        maximum_retry_delay=timedelta(minutes=5),
        lease_duration=timedelta(seconds=30),
        handler_timeout=timedelta(seconds=5),
        maximum_attempts=3,
    )

    report = await dispatcher.dispatch_batch(
        limit=1,
        worker_identity="projection_recovery_worker",
    )

    assert replay.command_id == first.command_id
    assert replay.replay is True
    assert report.delivered == 1
    assert len(consumer.event_ids) == 1


class _LeakingConsumer:
    def __init__(self, failure: Exception) -> None:
        self._failure = failure

    async def consume(
        self,
        intent: OutboxIntent,
        diagnostic_context: DiagnosticContext,
    ) -> OutboxConsumerSuccess:
        del intent, diagnostic_context
        raise self._failure


class _RecordingAndLoggingSink:
    def __init__(self, registry: DiagnosticEventRegistry, logger: logging.Logger) -> None:
        self.recording = RecordingDiagnosticSink(registry)
        self.logging = JsonLoggingDiagnosticSink(registry, logger=logger)

    @property
    def events(self) -> tuple[DiagnosticEvent, ...]:
        return self.recording.events

    async def emit(self, event: DiagnosticEvent) -> None:
        await self.recording.emit(event)
        await self.logging.emit(event)


async def test_postgresql_dispatch_commits_delivery_and_audit_atomically(
    postgresql_delivery_adapter: DeliveryPersistenceAdapter,
) -> None:
    adapter = postgresql_delivery_adapter
    workspace_id = synthetic_uuid(6560)
    event_id = synthetic_uuid(6561)
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
    consumers.register("workspace_projection", _SuccessfulConsumer())
    diagnostic_events = DiagnosticEventRegistry.with_default_families()
    diagnostic_sink = RecordingDiagnosticSink(diagnostic_events)
    audit_event_id = synthetic_uuid(6562)
    dispatcher = OutboxDispatcher(
        repository=adapter.delivery_repository,
        consumers=consumers,
        audit_events=adapter.audit_events,
        diagnostic_events=diagnostic_events,
        diagnostic_sink=diagnostic_sink,
        service_principal_id=synthetic_uuid(6563),
        clock=iter(
            (
                datetime(2026, 10, 12, 13, tzinfo=timezone.utc),
                datetime(2026, 10, 12, 13, 0, 1, tzinfo=timezone.utc),
            )
        ).__next__,
        audit_event_id_source=lambda: audit_event_id,
        retry_delay=lambda attempt_number: timedelta(seconds=attempt_number * 5),
        maximum_retry_delay=timedelta(minutes=5),
        lease_duration=timedelta(seconds=30),
        handler_timeout=timedelta(seconds=5),
        maximum_attempts=3,
    )

    report = await dispatcher.dispatch_batch(limit=1, worker_identity="worker_alpha")

    assert report.outcome_codes == (DispatchOutcomeCode.DELIVERED,)
    delivery = await adapter.delivery_repository.resolve(event_id)
    assert delivery is not None
    assert delivery.status.value == "delivered"
    audit = await adapter.read_audit_event(
        adapter.worker_workspace_context(workspace_id),
        audit_event_id,
    )
    assert audit is not None
    assert audit.causation_id == event_id
    assert len(diagnostic_sink.events) == 1


async def test_postgresql_temporal_production_ask_composition_reaches_terminal_status(
    delivery_database: DeliveryDatabase,
) -> None:
    """Exercise migration, admission, outbox, Temporal worker and terminalization together."""

    from uuid import uuid4

    from temporalio.common import WorkflowIDReusePolicy
    from temporalio.contrib.pydantic import pydantic_data_converter
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from spine.api.composition import compose_durable_ask
    from spine.application.answer_disclosure import (
        AnswerDisclosureGate,
        InMemoryAcceptedAnswerOutputPort,
    )
    from spine.application.answer_question_attempts import (
        PersistedAnswerQuestionAttemptService,
    )
    from spine.application.answer_question_context import AnswerQuestionContextService
    from spine.application.answer_question_execution import AnswerQuestionExecutionBoundary
    from spine.application.authorized_context_broker import (
        AuthorizedContextBrokerApplicationService,
    )
    from spine.application.diagnostics import DiagnosticContext
    from spine.application.persistence.ask_admission import (
        AskAdmissionCommand,
        QA_ASK_ADMISSION_OPERATION,
        register_ask_admission_outbox_event,
    )
    from spine.application.ask_admission import PersistedAskAdmissionService
    from spine.application.persistence.context import (
        PersistenceOperation,
        PersistencePurpose,
    )
    from spine.application.production_qa_capability import (
        AnswerQuestionContextReferences,
        ExistingAnswerQuestionCandidateValidator,
        PersistedAnswerQuestionCandidateRunner,
        ProductionAnswerQuestionCapability,
    )
    from spine.application.qa_sessions import (
        PersistedQASessionService,
        QASessionRequester,
    )
    from spine.application.workflows.ask_admission_dispatch import (
        ASK_ADMISSION_WORKFLOW_TYPE,
        AskAdmissionCommandData,
    )
    from spine.application.outbox import OutboxConsumerRegistry
    from spine.application.diagnostics.audit import AuditEventRegistry
    from spine.application.persistence.delivery import OutboxDeliveryRouteRegistry
    from spine.domain.capabilities.answer_question_execution import (
        AnswerQuestionExecutionPin as DomainExecutionPin,
    )
    from spine.domain.common import ActorKind, ActorRef
    from spine.infrastructure.auth.trusted_context import TrustedRequestContextBoundary
    from spine.infrastructure.workflows.ask_admission import (
        PersistenceAskAdmissionRunStarter,
    )
    from spine.infrastructure.qa_observation_capture.http import HttpCaptureChannel
    from spine.workflows.temporal.client import TemporalSdkWorkflowClient
    from spine.memory.contracts import ContextOutcome, ContextReasonCode, ContextResponse
    from tests.application.test_answer_question_execution import (
        capability as answer_question_capability,
        configuration,
    )
    from tests.contracts.persistence.outbox_events import create_outbox_event_registry

    workspace_id = synthetic_uuid(50601)
    environment_id = synthetic_uuid(50602)
    subject_id = synthetic_uuid(50603)
    trace_id = synthetic_uuid(50604)
    base_boundary = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(50605),
        secret=b"issue-506-postgresql-temporal-composition-boundary",
    )
    request_boundary = TrustedRequestContextBoundary.for_testing(
        persistence_boundary=base_boundary,
        issuer_id=synthetic_uuid(50606),
        secret=b"issue-506-postgresql-temporal-request-boundary",
    )
    outbox_events = create_outbox_event_registry()
    register_ask_admission_outbox_event(outbox_events)
    audit_events = AuditEventRegistry.with_default_families()
    runtime_sessions = async_sessionmaker(
        delivery_database.runtime_engine,
        expire_on_commit=False,
        autoflush=False,
    )
    dispatcher_sessions = async_sessionmaker(
        delivery_database.dispatcher_engine,
        expire_on_commit=False,
        autoflush=False,
    )
    persistence = PostgreSQLPersistence(
        session_factory=runtime_sessions,
        context_verifier=request_boundary,
        outbox_events=outbox_events,
        audit_events=audit_events,
    )

    def scoped_context(operation: str) -> TrustedPersistenceContext:
        return base_boundary.worker(
            scope=EnvironmentScope(
                workspace_id=workspace_id,
                environment_id=environment_id,
            ),
            service_principal_id=synthetic_uuid(50607),
            purpose=PersistencePurpose("issue_506_integration"),
            operation=PersistenceOperation(operation),
            trace_id=trace_id,
        )

    class ScopedPostgreSQLRepository:
        """Adapt a tenant UoW repository to the service's repository port."""

        def __init__(self, name: str) -> None:
            self._name = name

        def __getattr__(self, method_name: str):
            async def invoke(*args, **kwargs):
                async with persistence.uow_factory(
                    scoped_context("qa_sessions")
                ) as uow:
                    repository = getattr(uow, self._name)
                    result = await getattr(repository, method_name)(*args, **kwargs)
                    await uow.commit()
                return result

            return invoke

    class EligibleForSafeMessageRead:
        async def is_eligible(self, *, message, requester) -> bool:
            del message, requester
            return True

    sessions = PersistedQASessionService(
        repository=ScopedPostgreSQLRepository("qa_sessions"),  # type: ignore[arg-type]
        disclosure_eligibility=EligibleForSafeMessageRead(),  # type: ignore[arg-type]
        answer_question_runs=ScopedPostgreSQLRepository("answer_question_runs"),  # type: ignore[arg-type]
        answer_disclosures=ScopedPostgreSQLRepository("answer_disclosures"),  # type: ignore[arg-type]
    )
    seed_context = scoped_context("qa_sessions")
    pin = DomainExecutionPin.create(
        workspace_id=workspace_id,
        environment_id=environment_id,
        environment=EnvironmentKind.PRODUCTION,
        configuration=configuration(
            workspace_id=workspace_id,
            environment_id=environment_id,
        ),
    )
    execution = AnswerQuestionExecutionBoundary(
        capability=answer_question_capability(),
        binding_resolver=object(),  # type: ignore[arg-type]
        configuration_catalog=object(),  # type: ignore[arg-type]
        runtime_provider=object(),  # type: ignore[arg-type]
    )
    execution.resolve = lambda _request: (None, pin)  # type: ignore[method-assign]
    disclosure_repository = ScopedPostgreSQLRepository("answer_disclosures")
    disclosure_gate = AnswerDisclosureGate(
        repository=disclosure_repository,  # type: ignore[arg-type]
        accepted_output=InMemoryAcceptedAnswerOutputPort(),
    )
    broker = AuthorizedContextBrokerApplicationService(
        trusted_context=object(),
        profile_resolver=object(),
        publication=object(),
        scope_resolver=object(),
        read_model=object(),
        plan_builder=object(),
        retrieval=object(),
        evidence_verifier=object(),
        bundles=object(),
    )

    async def no_usable_evidence(_request):
        return ContextResponse(
            outcome=ContextOutcome.INSUFFICIENT,
            reason_code=ContextReasonCode.INSUFFICIENT_EVIDENCE,
        )

    broker.retrieve = no_usable_evidence  # type: ignore[method-assign]
    context_service = AnswerQuestionContextService(
        broker=broker,
        bundles=object(),  # type: ignore[arg-type]
        revalidator=object(),  # type: ignore[arg-type]
    )
    capability = ProductionAnswerQuestionCapability(
        context=context_service,
        execution=execution,
        context_references=type(
            "ContextReferences",
            (),
            {"for_command": lambda _self, _command: AnswerQuestionContextReferences(
                trusted_request_context_ref=synthetic_uuid(50608),
                authorization_context_ref=synthetic_uuid(50609),
            )},
        )(),
        disclosure_gate=disclosure_gate,
        candidate_runner=PersistedAnswerQuestionCandidateRunner(
            input_artifacts=object(),  # type: ignore[arg-type]
            artifacts=object(),  # type: ignore[arg-type]
            canceller=object(),  # type: ignore[arg-type]
        ),
        validator=ExistingAnswerQuestionCandidateValidator(
            citations=object(),  # type: ignore[arg-type]
            semantic_support=object(),  # type: ignore[arg-type]
            plans=object(),  # type: ignore[arg-type]
        ),
    )
    admission = PersistedAskAdmissionService(persistence.uow_factory)
    attempts = PersistedAnswerQuestionAttemptService(
        uow_factory=persistence.uow_factory,
        execution=execution,
    )
    routes = OutboxDeliveryRouteRegistry(version="issue506.integration.v1")

    class AdmissionCommandLoader:
        async def load(self, workflow_input):
            return AskAdmissionCommandData(
                command_id=workflow_input.command_id,
                workspace_id=workspace_id,
                environment_id=environment_id,
                session_id=session.value.session_id,
                message_id=workflow_input.message_id,
                run_id=workflow_input.run_id,
                question="Where is the approved policy?",
                subject=ActorRef(kind=ActorKind.SERVICE, id=subject_id),
                environment=EnvironmentKind.PRODUCTION,
                idempotency_key="issue-506-admission",
            )

    def worker_context(workflow_input):
        return base_boundary.worker(
            scope=EnvironmentScope(
                workspace_id=workflow_input.workspace_id,
                environment_id=workflow_input.environment_id,
            ),
            service_principal_id=synthetic_uuid(50610),
            purpose=PersistencePurpose("issue_506_worker"),
            operation=PersistenceOperation("answer_question_run"),
            trace_id=workflow_input.trace_id,
        )

    task_queue = "issue-506-production-qualification"
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as temporal:
        temporal_client = TemporalSdkWorkflowClient(
            temporal.client,
            task_queue=task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
        capture_channel = HttpCaptureChannel(
            base_url="https://capture.invalid",
            headers=lambda: {"X-Workload-Identity": "integration-test"},
        )
        try:
            composition = compose_durable_ask(
                capability=capability,
                sessions=sessions,
                admission=admission,
                attempts=attempts,
                command_loader=AdmissionCommandLoader(),
                persistence_context_factory=worker_context,
                temporal_client=temporal_client,
                delivery_routes=routes,
                monotonic_ns=iter(range(100, 1000)).__next__,
                worker_execution_elapsed_port=object(),  # type: ignore[arg-type]
                run_starter=PersistenceAskAdmissionRunStarter(
                    uow_factory=persistence.uow_factory,
                    context_factory=worker_context,
                ),
                capture_channel=capture_channel,
            )
            assert composition.real_qualification_evidence is not None
            assert composition.workflow_executor._service is composition.api_ask_service
            assert composition.observer_admission is not None
            assert composition.observer_admission._admission is composition.admission

            workspace_context = base_boundary.worker(
                scope=WorkspaceScope(workspace_id=workspace_id),
                service_principal_id=synthetic_uuid(50611),
                purpose=PersistencePurpose("issue_506_integration"),
                operation=PersistenceOperation("workspace_repository"),
                trace_id=trace_id,
            )
            async with persistence.uow_factory(workspace_context) as uow:
                await uow.workspaces.add(
                    Workspace(
                        id=workspace_id,
                        slug=f"issue-506-{workspace_id.hex[-8:]}",
                        display_name="Issue 506 qualification",
                    )
                )
                await uow.environments.add(
                    Environment(
                        id=environment_id,
                        workspace_id=workspace_id,
                        kind=EnvironmentKind.PRODUCTION,
                        display_name="Production",
                    )
                )
                await uow.commit()
            session = await sessions.create_session(
                context=seed_context,
                requester=QASessionRequester(subject_id=subject_id),
                idempotency_key=IdempotencyKey("issue-506-session"),
            )
            admitted = await admission.admit(
                context=base_boundary.worker(
                    scope=EnvironmentScope(
                        workspace_id=workspace_id,
                        environment_id=environment_id,
                    ),
                    service_principal_id=subject_id,
                    purpose=PersistencePurpose("issue_506_integration"),
                    operation=QA_ASK_ADMISSION_OPERATION,
                    trace_id=trace_id,
                ),
                command=AskAdmissionCommand.create(
                    workspace_id=workspace_id,
                    environment_id=environment_id,
                    session_id=session.value.session_id,
                    subject=ActorRef(kind=ActorKind.SERVICE, id=subject_id),
                    environment=EnvironmentKind.PRODUCTION,
                    question="Where is the approved policy?",
                    idempotency_key=IdempotencyKey("issue-506-admission"),
                ),
            )

            consumers = OutboxConsumerRegistry()
            for binding in composition.delivery_bindings:
                consumers.register(binding.consumer_name, binding.consumer)
            delivery_repository = PostgreSQLOutboxDeliveryRepository(
                session_factory=dispatcher_sessions,
                outbox_events=outbox_events,
                delivery_routes=routes,
                audit_events=audit_events,
            )
            diagnostics = DiagnosticEventRegistry.with_default_families()
            now = datetime(2026, 10, 20, tzinfo=timezone.utc)
            dispatcher = OutboxDispatcher(
                repository=delivery_repository,
                consumers=consumers,
                audit_events=audit_events,
                diagnostic_events=diagnostics,
                diagnostic_sink=RecordingDiagnosticSink(diagnostics),
                service_principal_id=synthetic_uuid(50613),
                clock=lambda: now,
                audit_event_id_source=uuid4,
                retry_delay=lambda _attempt: timedelta(seconds=1),
                maximum_retry_delay=timedelta(minutes=1),
                lease_duration=timedelta(seconds=30),
                handler_timeout=timedelta(seconds=10),
                maximum_attempts=3,
            )
            registered_activities = tuple(composition.activity_bindings)
            assert len(registered_activities) == 2
            async with Worker(
                temporal.client,
                task_queue=task_queue,
                workflows=[composition.workflow_definition],
                activities=list(registered_activities),
            ):
                report = await dispatcher.dispatch_batch(
                    limit=1,
                    worker_identity="issue-506-outbox-worker",
                )
                workflow_result = await asyncio.wait_for(
                    temporal.client.get_workflow_handle(str(admitted.run_id)).result(),
                    timeout=15,
                )

            final_status = await admission.status(
                context=base_boundary.worker(
                    scope=EnvironmentScope(
                        workspace_id=workspace_id,
                        environment_id=environment_id,
                    ),
                    service_principal_id=subject_id,
                    purpose=PersistencePurpose("issue_506_integration"),
                    operation=QA_ASK_ADMISSION_OPERATION,
                    trace_id=trace_id,
                ),
                result=admitted,
            )
            async with delivery_database.migration_engine.connect() as connection:
                postgresql_major = await connection.scalar(
                    text("SELECT current_setting('server_version_num')::integer / 10000")
                )
                migration_revision = await connection.scalar(
                    text("SELECT version_num FROM spine.alembic_version")
                )
            assert postgresql_major == 17
            assert migration_revision
            assert report.delivered == 1
            assert workflow_result["outcome"] == "abstained"
            assert final_status.status.value == "terminal"
            assert final_status.outcome.value == "abstained"
            assert ASK_ADMISSION_WORKFLOW_TYPE == "spine.qa.ask-admission:v1"
        finally:
            await capture_channel.close()


@pytest.mark.parametrize(
    "protected_failure",
    DIAGNOSTIC_LEAK_CORPUS,
    ids=lambda value: f"corpus_{DIAGNOSTIC_LEAK_CORPUS.index(value)}",
)
async def test_postgresql_issue68_disclosure_corpus_covers_persisted_rows(
    postgresql_delivery_adapter: DeliveryPersistenceAdapter,
    delivery_database: DeliveryDatabase,
    protected_failure: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Apply each raw-failure corpus value to rows returned from PostgreSQL."""

    adapter = postgresql_delivery_adapter
    workspace_id = synthetic_uuid(6792)
    event_id = synthetic_uuid(6793)
    audit_event_id = synthetic_uuid(6794)
    context = adapter.workspace_context(workspace_id)
    persisted_intent = intent(
        adapter,
        workspace_id=workspace_id,
        event_id=event_id,
        trace_id=context.trace_id,
        producer_deduplication_id=None,
    )
    async with adapter.uow_factory(context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.outbox.append(persisted_intent)
        await uow.commit()

    diagnostics = DiagnosticEventRegistry.with_default_families()
    logger = logging.getLogger("spine.tests.issue68.postgresql")
    diagnostic_sink = _RecordingAndLoggingSink(diagnostics, logger)
    consumer = _LeakingConsumer(RuntimeError(protected_failure))
    consumers = OutboxConsumerRegistry()
    consumers.register("workspace_projection", consumer)
    dispatcher = OutboxDispatcher(
        repository=adapter.delivery_repository,
        consumers=consumers,
        audit_events=adapter.audit_events,
        diagnostic_events=diagnostics,
        diagnostic_sink=diagnostic_sink,
        service_principal_id=synthetic_uuid(6795),
        clock=lambda: datetime(2026, 10, 12, 14, tzinfo=timezone.utc),
        audit_event_id_source=lambda: audit_event_id,
        retry_delay=lambda attempt_number: timedelta(seconds=attempt_number),
        maximum_retry_delay=timedelta(minutes=5),
        lease_duration=timedelta(seconds=30),
        handler_timeout=timedelta(seconds=5),
        maximum_attempts=3,
    )
    with caplog.at_level(logging.INFO, logger=logger.name):
        report = await dispatcher.dispatch_batch(
            limit=1,
            worker_identity="worker_alpha",
        )
    delivery = await adapter.delivery_repository.resolve(event_id)
    audit = await adapter.read_audit_event(
        adapter.worker_workspace_context(workspace_id),
        audit_event_id,
    )
    assert delivery is not None
    assert audit is not None

    row_captures: list[str] = []
    async with delivery_database.migration_engine.connect() as connection:
        for query, value in (
            (
                "SELECT row_to_json(a)::text FROM spine.audit_events AS a "
                "WHERE a.audit_event_id = :value",
                audit_event_id,
            ),
            (
                "SELECT row_to_json(o)::text FROM spine.outbox_intents AS o "
                "WHERE o.event_id = :value",
                event_id,
            ),
            (
                "SELECT row_to_json(d)::text FROM spine.outbox_deliveries AS d "
                "WHERE d.event_id = :value",
                event_id,
            ),
        ):
            row = await connection.execute(text(query), {"value": value})
            row_captures.append(row.scalar_one())

    assert report.outcome_codes == (DispatchOutcomeCode.QUARANTINED,)
    assert delivery.status.value == "quarantined"
    assert delivery.last_error_code == "delivery.handler_unexpected"
    assert audit.reason == "delivery.handler_unexpected"
    assert_no_diagnostic_leaks(
        (
            *row_captures,
            persisted_intent.model_dump_json(),
            audit.model_dump_json(),
            delivery.model_dump_json(),
            report.model_dump_json(),
            *(event.model_dump_json() for event in diagnostic_sink.events),
            caplog.text,
        ),
        leak_corpus=DIAGNOSTIC_LEAK_CORPUS,
    )


async def test_concurrent_postgresql_dispatchers_do_not_overlap_active_claim(
    postgresql_delivery_adapter: DeliveryPersistenceAdapter,
) -> None:
    adapter = postgresql_delivery_adapter
    workspace_id = synthetic_uuid(6564)
    event_id = synthetic_uuid(6565)
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
    consumer = _RecordingConsumer()
    consumers = OutboxConsumerRegistry()
    consumers.register("workspace_projection", consumer)
    diagnostics = DiagnosticEventRegistry.with_default_families()
    claimed_at = datetime(2026, 10, 12, 13, 15, tzinfo=timezone.utc)
    audit_event_ids = (synthetic_uuid(6566), synthetic_uuid(6567))

    def build_dispatcher(index: int) -> OutboxDispatcher:
        return OutboxDispatcher(
            repository=adapter.delivery_repository,
            consumers=consumers,
            audit_events=adapter.audit_events,
            diagnostic_events=diagnostics,
            diagnostic_sink=RecordingDiagnosticSink(diagnostics),
            service_principal_id=synthetic_uuid(6568 + index),
            clock=iter((claimed_at, claimed_at + timedelta(seconds=1))).__next__,
            audit_event_id_source=lambda: audit_event_ids[index],
            retry_delay=lambda attempt_number: timedelta(seconds=5),
            maximum_retry_delay=timedelta(minutes=5),
            lease_duration=timedelta(seconds=30),
            handler_timeout=timedelta(seconds=5),
            maximum_attempts=3,
        )

    reports = await asyncio.gather(
        build_dispatcher(0).dispatch_batch(limit=1, worker_identity="worker_alpha"),
        build_dispatcher(1).dispatch_batch(limit=1, worker_identity="worker_beta"),
    )

    assert sum(report.claimed for report in reports) == 1
    assert sum(report.delivered for report in reports) == 1
    assert consumer.event_ids == [event_id]
    delivery = await adapter.delivery_repository.resolve(event_id)
    assert delivery is not None
    assert delivery.status.value == "delivered"
    audits = await asyncio.gather(
        *(
            adapter.read_audit_event(
                adapter.worker_workspace_context(workspace_id),
                audit_event_id,
            )
            for audit_event_id in audit_event_ids
        )
    )
    assert sum(audit is not None for audit in audits) == 1


async def test_atomic_finalization_rejects_mismatched_workspace_scope(
    postgresql_delivery_adapter: DeliveryPersistenceAdapter,
    delivery_database: DeliveryDatabase,
) -> None:
    adapter = postgresql_delivery_adapter
    workspace_id = synthetic_uuid(6590)
    event_id = synthetic_uuid(6591)
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
    claimed_at = datetime(2026, 10, 12, 13, 30, tzinfo=timezone.utc)
    claim = (
        await adapter.delivery_repository.claim_due(
            limit=1,
            worker_identity="worker_alpha",
            claimed_at=claimed_at,
            lease_duration=timedelta(seconds=30),
        )
    )[0]

    async with delivery_database.dispatcher_engine.begin() as connection:
        result = await connection.execute(
            text(
                "SELECT * FROM spine.finalize_outbox_delivery("
                ":event_id, :workspace_id, NULL, :worker_identity, "
                ":lease_token, :attempt_number, :delivery_version, "
                "'delivered', :occurred_at, NULL, NULL, "
                ":audit_event_id, :service_principal_id)"
            ),
            {
                "event_id": event_id,
                "workspace_id": synthetic_uuid(6592),
                "worker_identity": claim.worker_identity,
                "lease_token": claim.lease_token,
                "attempt_number": claim.attempt_number,
                "delivery_version": claim.delivery_version,
                "occurred_at": claimed_at + timedelta(seconds=1),
                "audit_event_id": synthetic_uuid(6593),
                "service_principal_id": synthetic_uuid(6594),
            },
        )
        assert result.mappings().all() == []

    delivery = await adapter.delivery_repository.resolve(event_id)
    assert delivery is not None
    assert delivery.status.value == "in_flight"


async def test_postgresql_unknown_persisted_event_is_quarantined_safely(
    postgresql_delivery_adapter: DeliveryPersistenceAdapter,
    delivery_database: DeliveryDatabase,
) -> None:
    adapter = postgresql_delivery_adapter
    workspace_id = synthetic_uuid(6570)
    event_id = synthetic_uuid(6571)
    trace_id = synthetic_uuid(6572)
    context = adapter.workspace_context(workspace_id)
    async with adapter.uow_factory(context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.commit()
    async with delivery_database.migration_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO spine.outbox_intents ("
                "event_id, workspace_id, environment_id, event_type, "
                "event_schema_version, payload, trace_id, created_at) VALUES ("
                ":event_id, :workspace_id, NULL, 'future.event', 99, "
                "CAST(:payload AS jsonb), :trace_id, :created_at)"
            ),
            {
                "event_id": event_id,
                "workspace_id": workspace_id,
                "payload": '{"protected_unknown_shape":"never_loaded"}',
                "trace_id": trace_id,
                "created_at": datetime(2026, 10, 12, 14, tzinfo=timezone.utc),
            },
        )
    diagnostics = DiagnosticEventRegistry.with_default_families()
    dispatcher = OutboxDispatcher(
        repository=adapter.delivery_repository,
        consumers=OutboxConsumerRegistry(),
        audit_events=adapter.audit_events,
        diagnostic_events=diagnostics,
        diagnostic_sink=RecordingDiagnosticSink(diagnostics),
        service_principal_id=synthetic_uuid(6573),
        clock=iter(
            (
                datetime(2026, 10, 12, 15, tzinfo=timezone.utc),
                datetime(2026, 10, 12, 15, 0, 1, tzinfo=timezone.utc),
            )
        ).__next__,
        audit_event_id_source=lambda: synthetic_uuid(6574),
        retry_delay=lambda attempt_number: timedelta(seconds=5),
        maximum_retry_delay=timedelta(minutes=5),
        lease_duration=timedelta(seconds=30),
        handler_timeout=timedelta(seconds=5),
        maximum_attempts=3,
    )

    report = await dispatcher.dispatch_batch(limit=1, worker_identity="worker_alpha")

    assert report.outcome_codes == (DispatchOutcomeCode.QUARANTINED,)
    delivery = await adapter.delivery_repository.resolve(event_id)
    assert delivery is not None
    assert delivery.last_error_code == "delivery.unknown_event"


async def test_postgresql_audit_conflict_rolls_back_delivery_transition(
    postgresql_delivery_adapter: DeliveryPersistenceAdapter,
    delivery_database: DeliveryDatabase,
) -> None:
    adapter = postgresql_delivery_adapter
    workspace_id = synthetic_uuid(6580)
    event_id = synthetic_uuid(6581)
    audit_event_id = synthetic_uuid(6582)
    service_principal_id = synthetic_uuid(6583)
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
    occurred_at = datetime(2026, 10, 12, 16, tzinfo=timezone.utc)
    async with delivery_database.migration_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO spine.audit_events ("
                "audit_event_id, workspace_id, event_type, schema_version, origin, "
                "service_principal_id, trace_id, occurred_at, outcome, reason, payload) "
                "VALUES (:audit_event_id, :workspace_id, 'outbox.delivery', 1, "
                "'worker', :service_principal_id, :trace_id, :occurred_at, "
                "'succeeded', 'delivery.succeeded', CAST(:payload AS jsonb))"
            ),
            {
                "audit_event_id": audit_event_id,
                "workspace_id": workspace_id,
                "service_principal_id": service_principal_id,
                "trace_id": context.trace_id,
                "occurred_at": occurred_at,
                "payload": (
                    '{"outbox_event_id":"00000000-0000-0000-0000-000000000001",'
                    '"attempt_id":"00000000-0000-0000-0000-000000000002",'
                    '"attempt_number":1}'
                ),
            },
        )
    consumers = OutboxConsumerRegistry()
    consumers.register("workspace_projection", _SuccessfulConsumer())
    diagnostics = DiagnosticEventRegistry.with_default_families()
    diagnostic_sink = RecordingDiagnosticSink(diagnostics)
    dispatcher = OutboxDispatcher(
        repository=adapter.delivery_repository,
        consumers=consumers,
        audit_events=adapter.audit_events,
        diagnostic_events=diagnostics,
        diagnostic_sink=diagnostic_sink,
        service_principal_id=service_principal_id,
        clock=iter((occurred_at, occurred_at + timedelta(seconds=1))).__next__,
        audit_event_id_source=lambda: audit_event_id,
        retry_delay=lambda attempt_number: timedelta(seconds=5),
        maximum_retry_delay=timedelta(minutes=5),
        lease_duration=timedelta(seconds=30),
        handler_timeout=timedelta(seconds=5),
        maximum_attempts=3,
    )

    with pytest.raises(AuditConflictError):
        await dispatcher.dispatch_batch(limit=1, worker_identity="worker_alpha")

    delivery = await adapter.delivery_repository.resolve(event_id)
    assert delivery is not None
    assert delivery.status.value == "in_flight"
    assert diagnostic_sink.events == ()


async def test_dispatcher_role_is_limited_to_outbox_delivery_tables(
    delivery_database: DeliveryDatabase,
) -> None:
    async with delivery_database.migration_engine.connect() as connection:
        privileges = (
            await connection.execute(
                text(
                    "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
                    "WHERE grantee = :role ORDER BY table_name, privilege_type"
                ),
                {"role": DISPATCHER_ROLE},
            )
        ).all()
        security = (
            await connection.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE oid = 'spine.outbox_deliveries'::regclass"
                )
            )
        ).one()
        routine_privileges = (
            await connection.execute(
                text(
                    "SELECT routine_name, privilege_type "
                    "FROM information_schema.role_routine_grants "
                    "WHERE grantee = :role AND routine_schema = 'spine'"
                ),
                {"role": DISPATCHER_ROLE},
            )
        ).all()
        function_security = (
            await connection.execute(
                text(
                    "SELECT procedure.prosecdef, procedure.proconfig "
                    "FROM pg_proc AS procedure "
                    "JOIN pg_namespace AS namespace "
                    "ON namespace.oid = procedure.pronamespace "
                    "WHERE namespace.nspname = 'spine' "
                    "AND procedure.proname = 'finalize_outbox_delivery'"
                )
            )
        ).one()

    assert set(privileges) == {
        ("outbox_deliveries", "INSERT"),
        ("outbox_deliveries", "SELECT"),
        ("outbox_deliveries", "UPDATE"),
        ("outbox_intents", "SELECT"),
    }
    assert tuple(security) == (True, True)
    assert set(routine_privileges) == {("finalize_outbox_delivery", "EXECUTE")}
    assert function_security.prosecdef is True
    assert function_security.proconfig == ["search_path=pg_catalog, spine"]


async def test_runtime_delivery_reads_are_isolated_between_workspaces(
    postgresql_delivery_adapter: DeliveryPersistenceAdapter,
    delivery_database: DeliveryDatabase,
) -> None:
    adapter = postgresql_delivery_adapter
    first_workspace = synthetic_uuid(6550)
    second_workspace = synthetic_uuid(6551)
    first_event = synthetic_uuid(6552)
    second_event = synthetic_uuid(6553)
    for workspace_id, event_id in (
        (first_workspace, first_event),
        (second_workspace, second_event),
    ):
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
    await adapter.delivery_repository.claim_due(
        limit=2,
        worker_identity="worker_alpha",
        claimed_at=datetime(2026, 10, 11, 20, tzinfo=timezone.utc),
        lease_duration=timedelta(seconds=30),
    )

    async def visible(workspace_id: UUID) -> set[UUID]:
        async with delivery_database.runtime_engine.connect() as connection:
            async with connection.begin():
                await connection.execute(
                    text(
                        "SELECT set_config('spine.workspace_id', :workspace, true), "
                        "set_config('spine.environment_id', '', true)"
                    ),
                    {"workspace": str(workspace_id)},
                )
                rows = await connection.execute(
                    text("SELECT event_id FROM spine.outbox_deliveries")
                )
                return {row.event_id for row in rows}

    assert await visible(first_workspace) == {first_event}
    assert await visible(second_workspace) == {second_event}


async def test_unknown_persisted_event_claim_uses_header_only_envelope(
    postgresql_delivery_adapter: DeliveryPersistenceAdapter,
    delivery_database: DeliveryDatabase,
) -> None:
    adapter = postgresql_delivery_adapter
    workspace_id = synthetic_uuid(6560)
    event_id = synthetic_uuid(6561)
    trace_id = synthetic_uuid(6562)
    context = adapter.workspace_context(workspace_id)
    async with adapter.uow_factory(context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.commit()

    async with delivery_database.migration_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO spine.outbox_intents "
                "(event_id, workspace_id, event_type, event_schema_version, "
                "payload, trace_id) VALUES "
                "(:event_id, :workspace_id, 'future.event', 99, "
                "'{\"protected_unknown_shape\": \"never_loaded\"}'::jsonb, "
                ":trace_id)"
            ),
            {
                "event_id": event_id,
                "workspace_id": workspace_id,
                "trace_id": trace_id,
            },
        )

    statements: list[str] = []

    def record_statement(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del connection, cursor, parameters, context, executemany
        if "outbox_intents" in statement.lower():
            statements.append(statement.lower())

    event.listen(
        delivery_database.dispatcher_engine.sync_engine,
        "before_cursor_execute",
        record_statement,
    )
    try:
        claims = await adapter.delivery_repository.claim_due(
            limit=1,
            worker_identity="worker_alpha",
            claimed_at=datetime(2026, 10, 11, 21, tzinfo=timezone.utc),
            lease_duration=timedelta(seconds=30),
        )
    finally:
        event.remove(
            delivery_database.dispatcher_engine.sync_engine,
            "before_cursor_execute",
            record_statement,
        )

    assert len(claims) == 1
    claim = claims[0]
    assert type(claim.intent) is UnresolvedOutboxIntent
    assert claim.intent.event_id == event_id
    assert claim.consumer_name is None
    assert statements
    assert all("payload" not in statement for statement in statements)

    quarantined = await adapter.delivery_repository.quarantine(
        claim,
        quarantined_at=datetime(2026, 10, 11, 21, 0, 1, tzinfo=timezone.utc),
        error_code="delivery.unknown_route",
    )

    assert quarantined.status.value == "quarantined"
    assert quarantined.last_error_code == "delivery.unknown_route"
