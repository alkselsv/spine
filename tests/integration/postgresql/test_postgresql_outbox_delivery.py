from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from spine.application.diagnostics.audit import AuditEventRegistry
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
from spine.infrastructure.db.migrations import upgrade_database
from spine.infrastructure.db.operator import bootstrap_database_roles
from spine.infrastructure.db.settings import (
    POSTGRESQL_SEARCH_PATH_OPTIONS,
    MigrationDatabaseSettings,
    OperatorDatabaseSettings,
)
from spine.infrastructure.db.test_harness import TestDatabaseProvision
from spine.infrastructure.outbox import PostgreSQLOutboxDeliveryRepository
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.persistence.postgresql import PostgreSQLPersistence
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

    @asynccontextmanager
    async def hold_transactions() -> AsyncIterator[None]:
        yield

    return DeliveryPersistenceAdapter(
        uow_factory=persistence.uow_factory,
        workspace_context=workspace_context,
        worker_workspace_context=worker_workspace_context,
        environment_context=environment_context,
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

    assert set(privileges) == {
        ("outbox_deliveries", "INSERT"),
        ("outbox_deliveries", "SELECT"),
        ("outbox_deliveries", "UPDATE"),
        ("outbox_intents", "SELECT"),
    }
    assert tuple(security) == (True, True)


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

    claims = await adapter.delivery_repository.claim_due(
        limit=1,
        worker_identity="worker_alpha",
        claimed_at=datetime(2026, 10, 11, 21, tzinfo=timezone.utc),
        lease_duration=timedelta(seconds=30),
    )

    assert len(claims) == 1
    claim = claims[0]
    assert type(claim.intent) is UnresolvedOutboxIntent
    assert claim.intent.event_id == event_id
    assert claim.consumer_name is None

    quarantined = await adapter.delivery_repository.quarantine(
        claim,
        quarantined_at=datetime(2026, 10, 11, 21, 0, 1, tzinfo=timezone.utc),
        error_code="delivery.unknown_route",
    )

    assert quarantined.status.value == "quarantined"
    assert quarantined.last_error_code == "delivery.unknown_route"
