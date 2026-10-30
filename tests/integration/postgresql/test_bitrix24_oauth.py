from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from uuid import UUID

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import insert, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from spine.application.connections.bitrix24_oauth import (
    Bitrix24ActivationRequest,
    Bitrix24BootstrapAttempt,
    Bitrix24BootstrapCoordinator,
    Bitrix24CallbackCommand,
    Bitrix24CallbackDispatcher,
    Bitrix24ConnectionActivation,
    Bitrix24RefreshCommand,
    CallbackAccepted,
    CallbackDuplicate,
    CallbackVerification,
    CallbackVerificationStatus,
    PairActivated,
    RefreshBusy,
    RefreshClaim,
    SecretTokenPair,
    StaleRefreshFenced,
)
from spine.application.persistence import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderConnection,
    ProviderConnectionStatus,
    ProviderKind,
)
from spine.infrastructure.connections.postgresql_bitrix24_oauth import (
    PostgreSQLBitrix24OAuthRepository,
)
from spine.infrastructure.connections.bitrix24_oauth import InMemoryTokenPairStore
from spine.infrastructure.connections.postgresql_provider_management import (
    provider_connection_approvals,
)
from spine.infrastructure.db.migrations import upgrade_database
from spine.infrastructure.db.operator import bootstrap_database_roles
from spine.infrastructure.db.settings import (
    POSTGRESQL_SEARCH_PATH_OPTIONS,
    MigrationDatabaseSettings,
    OperatorDatabaseSettings,
)
from spine.infrastructure.db.test_harness import TestDatabaseProvision
from spine.infrastructure.persistence.contexts import TrustedContextBoundary

pytestmark = [pytest.mark.postgresql, pytest.mark.asyncio(loop_scope="session")]
MIGRATION_ROLE = "spine_migration"
RUNTIME_ROLE = "spine_runtime"
DISPATCHER_ROLE = "spine_dispatcher"
MIGRATION_PASSWORD = "migration-test-secret"
RUNTIME_PASSWORD = "runtime-test-secret"
DISPATCHER_PASSWORD = "dispatcher-test-secret"
WORKSPACE_ID = UUID("91000000-0000-0000-0000-000000000001")
ENVIRONMENT_ID = UUID("92000000-0000-0000-0000-000000000001")
SERVICE_PRINCIPAL_ID = UUID("93000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def _id(value: int) -> UUID:
    return UUID(f"94000000-0000-0000-0000-{value:012d}")


@dataclass(frozen=True, slots=True)
class OAuthDatabase:
    migration_engine: AsyncEngine
    runtime_engine: AsyncEngine


def _role_url(provision: TestDatabaseProvision, role: str, password: str) -> SecretStr:
    value = make_url(provision.url.get_secret_value()).set(
        username=role, password=password
    )
    return SecretStr(value.render_as_string(hide_password=False))


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def oauth_database(
    postgresql_provision: TestDatabaseProvision,
) -> AsyncIterator[OAuthDatabase]:
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
    migration_url = _role_url(postgresql_provision, MIGRATION_ROLE, MIGRATION_PASSWORD)
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
        migration_url.get_secret_value(), hide_parameters=True
    )
    runtime_engine = create_async_engine(
        _role_url(
            postgresql_provision, RUNTIME_ROLE, RUNTIME_PASSWORD
        ).get_secret_value(),
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    async with migration_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO spine.workspaces (id, slug, display_name) "
                "VALUES (:workspace_id, 'bitrix24-oauth', 'Bitrix24 OAuth')"
            ),
            {"workspace_id": WORKSPACE_ID},
        )
        await connection.execute(
            text(
                "INSERT INTO spine.environments (id, workspace_id, kind, display_name) "
                "VALUES (:environment_id, :workspace_id, 'staging', 'Bitrix24 OAuth')"
            ),
            {"workspace_id": WORKSPACE_ID, "environment_id": ENVIRONMENT_ID},
        )
    try:
        yield OAuthDatabase(migration_engine, runtime_engine)
    finally:
        await runtime_engine.dispose()
        async with migration_engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA IF EXISTS spine CASCADE"))
        await migration_engine.dispose()


def _connection() -> ProviderConnection:
    return ProviderConnection(
        connection_version_id=_id(5),
        connection_id=_id(4),
        approval_id=_id(3),
        version=1,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        service_principal_id=SERVICE_PRINCIPAL_ID,
        provider_kind=ProviderKind.BITRIX24,
        credential_reference=OpaqueProviderReference(value=_id(7)),
        grant_reference=OpaqueProviderReference(value=_id(8)),
        approved_resource_scope_references=(OpaqueProviderReference(value=_id(9)),),
        authorization_generation=10,
        configuration_generation=11,
        status=ProviderConnectionStatus.ACTIVE,
    )


async def _seed_approval(
    engine: AsyncEngine,
    connection: ProviderConnection,
) -> None:
    async with engine.begin() as session:
        await session.execute(
            insert(provider_connection_approvals).values(
                approval_id=connection.approval_id,
                workspace_id=connection.workspace_id,
                environment_id=connection.environment_id,
                service_principal_id=connection.service_principal_id,
                provider_kind=connection.provider_kind.value,
                credential_reference=connection.credential_reference.value,
                grant_reference=connection.grant_reference.value,
                approved_resource_scope_references=[_id(9)],
                authorization_generation=connection.authorization_generation,
                configuration_generation=connection.configuration_generation,
                status=connection.status.value,
                recorded_at=NOW,
            )
        )


async def test_postgresql_refresh_claim_and_pair_pointer_switch_are_cas_fenced(
    oauth_database: OAuthDatabase,
) -> None:
    connection = _connection()
    await _seed_approval(oauth_database.migration_engine, connection)
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=_id(15), secret=b"bitrix24-oauth-postgresql-test-secret"
    )
    context = boundary.worker(
        scope=EnvironmentScope(
            workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID
        ),
        service_principal_id=SERVICE_PRINCIPAL_ID,
        purpose=PersistencePurpose("provider_connection_access"),
        operation=PersistenceOperation("bitrix24_oauth_refresh"),
        trace_id=_id(16),
    )
    bootstrap_context = boundary.worker(
        scope=EnvironmentScope(
            workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID
        ),
        service_principal_id=SERVICE_PRINCIPAL_ID,
        purpose=PersistencePurpose("provider_connection_access"),
        operation=PersistenceOperation("bitrix24_oauth_bootstrap"),
        trace_id=_id(17),
    )
    callback_context = boundary.worker(
        scope=EnvironmentScope(
            workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID
        ),
        service_principal_id=SERVICE_PRINCIPAL_ID,
        purpose=PersistencePurpose("provider_connection_access"),
        operation=PersistenceOperation("bitrix24_oauth_callback"),
        trace_id=_id(18),
    )
    bootstrap_repository = PostgreSQLBitrix24OAuthRepository(
        session_factory=async_sessionmaker(
            oauth_database.runtime_engine, expire_on_commit=False
        ),
        context_verifier=boundary,
        context=bootstrap_context,
    )
    callback_repository = PostgreSQLBitrix24OAuthRepository(
        session_factory=async_sessionmaker(
            oauth_database.runtime_engine, expire_on_commit=False
        ),
        context_verifier=boundary,
        context=callback_context,
    )
    attempt = Bitrix24BootstrapAttempt(
        attempt_id=_id(19),
        workspace_id=connection.workspace_id,
        environment_id=connection.environment_id,
        approval_id=connection.approval_id,
        connection_id=connection.connection_id,
        connection_version_id=connection.connection_version_id,
        grant_reference=connection.grant_reference,
        portal_member_id="member-1",
        application_id="app-1",
        installing_user_id="42",
        approved_scopes=("disk",),
        authorization_generation=connection.authorization_generation,
        configuration_generation=connection.configuration_generation,
        expires_at=datetime(2026, 10, 30, 13, tzinfo=timezone.utc),
    )
    await Bitrix24BootstrapCoordinator(repository=bootstrap_repository).start(attempt)
    accepted = await Bitrix24CallbackDispatcher(
        repository=callback_repository,
        verifier=_Verifier(
            OpaqueProviderReference(value=_id(13)),
            portal_member_id="member-1",
            application_id="app-1",
            acting_user_id="42",
        ),
    ).dispatch(
        Bitrix24CallbackCommand(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            application_id="app-1",
            portal_member_id_hint="member-1",
            event_kind="ONAPPINSTALL",
            callback_reference=OpaqueProviderReference(value=_id(24)),
            payload_fingerprint="b" * 64,
            received_at=NOW,
        )
    )
    assert isinstance(accepted, CallbackAccepted)
    activation_context = boundary.worker(
        scope=EnvironmentScope(
            workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID
        ),
        service_principal_id=SERVICE_PRINCIPAL_ID,
        purpose=PersistencePurpose("provider_connection_access"),
        operation=PersistenceOperation("bitrix24_oauth_activate"),
        trace_id=_id(25),
    )
    activation_repository = PostgreSQLBitrix24OAuthRepository(
        session_factory=async_sessionmaker(
            oauth_database.runtime_engine, expire_on_commit=False
        ),
        context_verifier=boundary,
        context=activation_context,
    )
    activated = await Bitrix24ConnectionActivation(
        repository=activation_repository,
        authority=_AllowActivation(),
        pair_store=InMemoryTokenPairStore(
            initial_pairs={
                OpaqueProviderReference(value=_id(13)): SecretTokenPair(
                    "initial-access", "initial-refresh"
                )
            }
        ),
    ).activate(
        Bitrix24ActivationRequest(
            connection=connection,
            receipt_id=accepted.receipt_id,
            command_id=_id(26),
            occurred_at=NOW,
        )
    )
    assert activated.binding_id == accepted.binding_id
    repository = PostgreSQLBitrix24OAuthRepository(
        session_factory=async_sessionmaker(
            oauth_database.runtime_engine, expire_on_commit=False
        ),
        context_verifier=boundary,
        context=context,
    )

    claim = await repository.claim_refresh(
        Bitrix24RefreshCommand(
            connection=connection, expected_token_epoch=1, attempt_id=_id(20)
        )
    )
    loser = await repository.claim_refresh(
        Bitrix24RefreshCommand(
            connection=connection, expected_token_epoch=1, attempt_id=_id(21)
        )
    )
    assert isinstance(claim, RefreshClaim)
    assert loser == RefreshBusy(binding_id=accepted.binding_id, token_epoch=1)

    activated = await repository.activate_candidate(
        claim, OpaqueProviderReference(value=_id(22))
    )
    stale = await repository.activate_candidate(
        claim, OpaqueProviderReference(value=_id(23))
    )

    assert activated == PairActivated(
        binding_id=accepted.binding_id,
        pair_reference=OpaqueProviderReference(value=_id(22)),
        token_epoch=2,
    )
    assert stale == StaleRefreshFenced(binding_id=accepted.binding_id)
    current = await repository.current_authority(connection)
    assert current is not None
    assert current.active_pair_reference == OpaqueProviderReference(value=_id(22))
    assert current.token_epoch == 2
    async with oauth_database.migration_engine.connect() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT reason, payload::text AS payload FROM spine.audit_events "
                        "WHERE target_id = :binding_id ORDER BY occurred_at, reason"
                    ),
                    {"binding_id": accepted.binding_id},
                )
            )
            .mappings()
            .all()
        )
    assert {row.reason for row in rows} == {"connection_activated", "pair_activated"}
    rendered_audit = " ".join(row.payload for row in rows)
    assert "access_token" not in rendered_audit
    assert "refresh_token" not in rendered_audit
    assert "client_secret" not in rendered_audit


class _AllowActivation:
    async def authorize(self, request):
        del request
        return True


class _Verifier:
    def __init__(
        self,
        pair_candidate_reference: OpaqueProviderReference | None = None,
        *,
        portal_member_id: str = "member-callback",
        application_id: str = "app-callback",
        acting_user_id: str = "77",
    ) -> None:
        self.pair_candidate_reference = (
            pair_candidate_reference or OpaqueProviderReference(value=_id(70))
        )
        self.portal_member_id = portal_member_id
        self.application_id = application_id
        self.acting_user_id = acting_user_id

    async def verify(self, attempt, callback_reference):
        del attempt, callback_reference
        return CallbackVerification(
            status=CallbackVerificationStatus.VERIFIED,
            portal_member_id=self.portal_member_id,
            application_id=self.application_id,
            acting_user_id=self.acting_user_id,
            granted_scopes=("disk",),
            pair_candidate_reference=self.pair_candidate_reference,
        )


async def test_postgresql_callback_receipt_replays_without_second_activation(
    oauth_database: OAuthDatabase,
) -> None:
    connection = _connection()
    await _seed_approval(oauth_database.migration_engine, connection)
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=_id(60), secret=b"bitrix24-callback-postgresql-secret"
    )
    context = boundary.worker(
        scope=EnvironmentScope(
            workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID
        ),
        service_principal_id=SERVICE_PRINCIPAL_ID,
        purpose=PersistencePurpose("provider_connection_access"),
        operation=PersistenceOperation("bitrix24_oauth_callback"),
        trace_id=_id(61),
    )
    bootstrap_context = boundary.worker(
        scope=EnvironmentScope(
            workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID
        ),
        service_principal_id=SERVICE_PRINCIPAL_ID,
        purpose=PersistencePurpose("provider_connection_access"),
        operation=PersistenceOperation("bitrix24_oauth_bootstrap"),
        trace_id=_id(68),
    )
    bootstrap_repository = PostgreSQLBitrix24OAuthRepository(
        session_factory=async_sessionmaker(
            oauth_database.runtime_engine, expire_on_commit=False
        ),
        context_verifier=boundary,
        context=bootstrap_context,
    )
    repository = PostgreSQLBitrix24OAuthRepository(
        session_factory=async_sessionmaker(
            oauth_database.runtime_engine, expire_on_commit=False
        ),
        context_verifier=boundary,
        context=context,
    )
    attempt = Bitrix24BootstrapAttempt(
        attempt_id=_id(62),
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        approval_id=connection.approval_id,
        connection_id=connection.connection_id,
        connection_version_id=connection.connection_version_id,
        grant_reference=connection.grant_reference,
        portal_member_id="member-callback",
        application_id="app-callback",
        installing_user_id="77",
        approved_scopes=("disk",),
        authorization_generation=connection.authorization_generation,
        configuration_generation=connection.configuration_generation,
        expires_at=datetime(2026, 10, 30, 13, tzinfo=timezone.utc),
    )
    await Bitrix24BootstrapCoordinator(repository=bootstrap_repository).start(attempt)
    command = Bitrix24CallbackCommand(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        application_id="app-callback",
        portal_member_id_hint="member-callback",
        event_kind="ONAPPINSTALL",
        callback_reference=OpaqueProviderReference(value=_id(67)),
        payload_fingerprint="c" * 64,
        received_at=NOW,
    )
    dispatcher = Bitrix24CallbackDispatcher(repository=repository, verifier=_Verifier())

    accepted = await dispatcher.dispatch(command)
    duplicate = await dispatcher.dispatch(
        replace(
            command,
            callback_reference=OpaqueProviderReference(value=_id(71)),
        )
    )

    assert isinstance(accepted, CallbackAccepted)
    assert duplicate == CallbackDuplicate(
        receipt_id=accepted.receipt_id, binding_id=accepted.binding_id
    )
    read_context = boundary.worker(
        scope=EnvironmentScope(
            workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID
        ),
        service_principal_id=SERVICE_PRINCIPAL_ID,
        purpose=PersistencePurpose("provider_connection_access"),
        operation=PersistenceOperation("bitrix24_oauth_read"),
        trace_id=_id(72),
    )
    read_repository = PostgreSQLBitrix24OAuthRepository(
        session_factory=async_sessionmaker(
            oauth_database.runtime_engine, expire_on_commit=False
        ),
        context_verifier=boundary,
        context=read_context,
    )
    assert await read_repository.current_authority(connection) is None
    activation_context = boundary.worker(
        scope=EnvironmentScope(
            workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID
        ),
        service_principal_id=SERVICE_PRINCIPAL_ID,
        purpose=PersistencePurpose("provider_connection_access"),
        operation=PersistenceOperation("bitrix24_oauth_activate"),
        trace_id=_id(73),
    )
    activated = await Bitrix24ConnectionActivation(
        repository=PostgreSQLBitrix24OAuthRepository(
            session_factory=async_sessionmaker(
                oauth_database.runtime_engine, expire_on_commit=False
            ),
            context_verifier=boundary,
            context=activation_context,
        ),
        authority=_AllowActivation(),
        pair_store=InMemoryTokenPairStore(
            initial_pairs={
                OpaqueProviderReference(value=_id(70)): SecretTokenPair(
                    "callback-access", "callback-refresh"
                )
            }
        ),
    ).activate(
        Bitrix24ActivationRequest(
            connection=connection,
            receipt_id=accepted.receipt_id,
            command_id=_id(74),
            occurred_at=NOW,
        )
    )
    assert activated.binding_id == accepted.binding_id
    authority = await read_repository.current_authority(connection)
    assert authority is not None
    assert authority.active_pair_reference == OpaqueProviderReference(value=_id(70))
