from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from spine.application.connections.google import (
    GoogleConnectionDeniedError,
    GoogleReadCommand,
)
from spine.application.persistence import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.domain.connections.google import (
    GoogleConnection,
    GoogleConnectionStatus,
    OpaqueGoogleReference,
)
from spine.infrastructure.connections.postgresql_google import (
    PostgreSQLGoogleConnectionRepository,
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
from tests.contracts.connections.google_connection_repository_contract import (
    GoogleConnectionRepositoryScenario,
    assert_google_connection_repository_contract,
)


pytestmark = [pytest.mark.postgresql, pytest.mark.asyncio(loop_scope="session")]
MIGRATION_ROLE = "spine_migration"
RUNTIME_ROLE = "spine_runtime"
DISPATCHER_ROLE = "spine_dispatcher"
MIGRATION_PASSWORD = "migration-test-secret"
RUNTIME_PASSWORD = "runtime-test-secret"
DISPATCHER_PASSWORD = "dispatcher-test-secret"
WORKSPACE_ID = UUID("81000000-0000-0000-0000-000000000001")
OTHER_WORKSPACE_ID = UUID("81000000-0000-0000-0000-000000000002")
ENVIRONMENT_ID = UUID("82000000-0000-0000-0000-000000000001")
OTHER_ENVIRONMENT_ID = UUID("82000000-0000-0000-0000-000000000002")
SERVICE_PRINCIPAL_ID = UUID("83000000-0000-0000-0000-000000000001")


@dataclass(frozen=True, slots=True)
class GoogleDatabase:
    migration_engine: AsyncEngine
    runtime_engine: AsyncEngine


def _role_url(provision: TestDatabaseProvision, role: str, password: str) -> SecretStr:
    value = make_url(provision.url.get_secret_value()).set(
        username=role, password=password
    )
    return SecretStr(value.render_as_string(hide_password=False))


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def google_database(
    postgresql_provision: TestDatabaseProvision,
) -> AsyncIterator[GoogleDatabase]:
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
        _role_url(postgresql_provision, RUNTIME_ROLE, RUNTIME_PASSWORD).get_secret_value(),
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    async with migration_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO spine.workspaces (id, slug, display_name) VALUES "
                "(:workspace_id, 'google-connections', 'Google Connections'), "
                "(:other_workspace_id, 'google-connections-other', 'Other Google Connections')"
            ),
            {
                "workspace_id": WORKSPACE_ID,
                "other_workspace_id": OTHER_WORKSPACE_ID,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO spine.environments (id, workspace_id, kind, display_name) VALUES "
                "(:environment_id, :workspace_id, 'staging', 'Google'), "
                "(:other_environment_id, :other_workspace_id, 'staging', 'Other Google')"
            ),
            {
                "workspace_id": WORKSPACE_ID,
                "environment_id": ENVIRONMENT_ID,
                "other_workspace_id": OTHER_WORKSPACE_ID,
                "other_environment_id": OTHER_ENVIRONMENT_ID,
            },
        )
    try:
        yield GoogleDatabase(migration_engine, runtime_engine)
    finally:
        await runtime_engine.dispose()
        await migration_engine.dispose()


def _connection(
    *,
    version: int,
    predecessor: UUID | None = None,
    connection_id: UUID = UUID("84000000-0000-0000-0000-000000000001"),
) -> GoogleConnection:
    return GoogleConnection(
        connection_version_id=UUID(
            f"85000000-0000-0000-0000-{version:012d}"
        ),
        connection_id=connection_id,
        version=version,
        predecessor_version_id=predecessor,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        service_principal_id=SERVICE_PRINCIPAL_ID,
        credential_reference=OpaqueGoogleReference(value=f"vault:google-v{version}"),
        grant_reference=OpaqueGoogleReference(value=f"grant:google-v{version}"),
        approved_resource_scope_references=(
            OpaqueGoogleReference(value="scope:estimate-register-v1"),
        ),
        authorization_generation=version,
        configuration_generation=version,
        status=GoogleConnectionStatus.ACTIVE,
    )


def _boundary() -> TrustedContextBoundary:
    return TrustedContextBoundary.for_testing(
        issuer_id=UUID("86000000-0000-0000-0000-000000000001"),
        secret=b"issue-599-postgresql-contract-secret",
    )


def _context(boundary: TrustedContextBoundary, *, workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID):
    return boundary.worker(
        scope=EnvironmentScope(
            workspace_id=workspace_id, environment_id=environment_id
        ),
        service_principal_id=SERVICE_PRINCIPAL_ID,
        purpose=PersistencePurpose("google_connector"),
        operation=PersistenceOperation("google_connection_register"),
        trace_id=UUID("87000000-0000-0000-0000-000000000001"),
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_google_connection_repository_contract(
    google_database: GoogleDatabase,
) -> None:
    async def scenario() -> GoogleConnectionRepositoryScenario:
        boundary = _boundary()
        initial = _connection(version=1)
        return GoogleConnectionRepositoryScenario(
            repository=PostgreSQLGoogleConnectionRepository(
                session_factory=async_sessionmaker(
                    google_database.runtime_engine, expire_on_commit=False
                ),
                context_verifier=boundary,
            ),
            context=_context(boundary),
            initial=initial,
            successor=_connection(
                version=2, predecessor=initial.connection_version_id
            ),
        )

    await assert_google_connection_repository_contract(scenario)


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_google_connection_rls_hides_other_workspace(
    google_database: GoogleDatabase,
) -> None:
    boundary = _boundary()
    repository = PostgreSQLGoogleConnectionRepository(
        session_factory=async_sessionmaker(
            google_database.runtime_engine, expire_on_commit=False
        ),
        context_verifier=boundary,
    )

    with pytest.raises(GoogleConnectionDeniedError):
        await repository.resolve_for_read(
            GoogleReadCommand(
                connection_id=UUID("84000000-0000-0000-0000-000000000001"),
                resource_scope_reference=OpaqueGoogleReference(
                    value="scope:estimate-register-v1"
                ),
                authorization_generation=2,
                configuration_generation=2,
            ),
            _context(
                boundary,
                workspace_id=OTHER_WORKSPACE_ID,
                environment_id=OTHER_ENVIRONMENT_ID,
            ),
        )

    async with google_database.runtime_engine.begin() as connection:
        await connection.execute(
            text(
                "SELECT set_config('spine.workspace_id', :workspace_id, true), "
                "set_config('spine.environment_id', :environment_id, true)"
            ),
            {
                "workspace_id": str(OTHER_WORKSPACE_ID),
                "environment_id": str(OTHER_ENVIRONMENT_ID),
            },
        )
        count = await connection.scalar(
            text("SELECT count(*) FROM spine.google_connection_versions")
        )
    assert count == 0
