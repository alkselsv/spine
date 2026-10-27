from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import insert, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from spine.application.connections.google import (
    GoogleConnectionCommand,
    GoogleConnectionConflictError,
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
    GoogleConnectionApproval,
    GoogleConnectionStatus,
    OpaqueGoogleReference,
)
from spine.infrastructure.connections.postgresql_google import (
    PostgreSQLGoogleConnectionRepository,
    google_connection_approvals,
)
from spine.infrastructure.persistence.postgresql_google_sheets_sync import (
    PostgreSQLGoogleSheetsSyncRepository,
)
from spine.application.persistence.google_sheets_sync import (
    GoogleSheetsIdentityDisposition,
    GoogleSheetsIdentityFailureCode,
    GoogleSheetsSyncRunCommand,
    GoogleSheetsSyncRunState,
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
    OCCURRED_AT,
    GoogleConnectionRepositoryScenario,
    assert_google_connection_generation_fencing_contract,
    assert_google_connection_idempotency_conflict_contract,
    assert_google_connection_registration_replay_contract,
    assert_google_connection_successor_contract,
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
    migration_url = _role_url(
        provision=postgresql_provision, role=MIGRATION_ROLE, password=MIGRATION_PASSWORD
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
        async with migration_engine.begin() as connection:
            # The session PostgreSQL target is shared across modules. Google
            # connection history is intentionally immutable, so reset this
            # disposable schema instead of deleting protected audit records.
            await connection.execute(text("DROP SCHEMA IF EXISTS spine CASCADE"))
        await migration_engine.dispose()


def _id(value: int) -> UUID:
    return UUID(f"85000000-0000-0000-0000-{value:012d}")


def _connection(
    *,
    case: int,
    version: int,
    predecessor: UUID | None = None,
) -> GoogleConnection:
    return GoogleConnection(
        connection_version_id=_id(case * 100 + version),
        connection_id=_id(case * 100),
        approval_id=_id(case * 100 + 20 + version),
        version=version,
        predecessor_version_id=predecessor,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        service_principal_id=SERVICE_PRINCIPAL_ID,
        credential_reference=OpaqueGoogleReference(
            value=_id(case * 100 + 40 + version)
        ),
        grant_reference=OpaqueGoogleReference(value=_id(case * 100 + 60 + version)),
        approved_resource_scope_references=(
            OpaqueGoogleReference(value=_id(case * 100 + 80)),
        ),
        authorization_generation=version,
        configuration_generation=version,
        status=GoogleConnectionStatus.ACTIVE,
    )


def _approval(connection: GoogleConnection) -> GoogleConnectionApproval:
    return GoogleConnectionApproval(
        approval_id=connection.approval_id,
        workspace_id=connection.workspace_id,
        environment_id=connection.environment_id,
        service_principal_id=connection.service_principal_id,
        credential_reference=connection.credential_reference,
        grant_reference=connection.grant_reference,
        approved_resource_scope_references=connection.approved_resource_scope_references,
        authorization_generation=connection.authorization_generation,
        configuration_generation=connection.configuration_generation,
        status=connection.status,
    )


async def _seed_approvals(
    database: GoogleDatabase, approvals: tuple[GoogleConnectionApproval, ...]
) -> None:
    async with database.migration_engine.begin() as connection:
        for approval in approvals:
            await connection.execute(
                insert(google_connection_approvals).values(
                    approval_id=approval.approval_id,
                    workspace_id=approval.workspace_id,
                    environment_id=approval.environment_id,
                    service_principal_id=approval.service_principal_id,
                    credential_reference=approval.credential_reference.value,
                    grant_reference=approval.grant_reference.value,
                    approved_resource_scope_references=[
                        item.value
                        for item in approval.approved_resource_scope_references
                    ],
                    authorization_generation=approval.authorization_generation,
                    configuration_generation=approval.configuration_generation,
                    status=approval.status.value,
                    recorded_at=datetime(2026, 10, 26, tzinfo=timezone.utc),
                )
            )


def _boundary() -> TrustedContextBoundary:
    return TrustedContextBoundary.for_testing(
        issuer_id=UUID("86000000-0000-0000-0000-000000000001"),
        secret=b"issue-599-postgresql-contract-secret",
    )


def _context(
    boundary: TrustedContextBoundary,
    *,
    workspace_id: UUID = WORKSPACE_ID,
    environment_id: UUID = ENVIRONMENT_ID,
    operation: str = "google_connection_register",
):
    return boundary.worker(
        scope=EnvironmentScope(
            workspace_id=workspace_id, environment_id=environment_id
        ),
        service_principal_id=SERVICE_PRINCIPAL_ID,
        purpose=PersistencePurpose("google_connector"),
        operation=PersistenceOperation(operation),
        trace_id=UUID("87000000-0000-0000-0000-000000000001"),
    )


async def _scenario(
    database: GoogleDatabase, *, case: int
) -> GoogleConnectionRepositoryScenario:
    boundary = _boundary()
    initial = _connection(case=case, version=1)
    successor = _connection(
        case=case, version=2, predecessor=initial.connection_version_id
    )
    await _seed_approvals(database, (_approval(initial), _approval(successor)))
    return GoogleConnectionRepositoryScenario(
        repository=PostgreSQLGoogleConnectionRepository(
            session_factory=async_sessionmaker(
                database.runtime_engine, expire_on_commit=False
            ),
            context_verifier=boundary,
        ),
        context=_context(boundary),
        initial=initial,
        successor=successor,
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_registration_replay_is_stable(
    google_database: GoogleDatabase,
) -> None:
    async def scenario() -> GoogleConnectionRepositoryScenario:
        return await _scenario(google_database, case=1)

    await assert_google_connection_registration_replay_contract(scenario)


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_idempotency_key_reuse_with_other_payload_conflicts(
    google_database: GoogleDatabase,
) -> None:
    async def scenario() -> GoogleConnectionRepositoryScenario:
        return await _scenario(google_database, case=2)

    await assert_google_connection_idempotency_conflict_contract(scenario)


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_unfenced_successor_conflicts(
    google_database: GoogleDatabase,
) -> None:
    async def scenario() -> GoogleConnectionRepositoryScenario:
        return await _scenario(google_database, case=3)

    await assert_google_connection_generation_fencing_contract(scenario)


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_fenced_successor_becomes_current(
    google_database: GoogleDatabase,
) -> None:
    async def scenario() -> GoogleConnectionRepositoryScenario:
        return await _scenario(google_database, case=4)

    await assert_google_connection_successor_contract(scenario)


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_unapproved_connection_conflicts(
    google_database: GoogleDatabase,
) -> None:
    boundary = _boundary()
    connection = _connection(case=6, version=1)
    repository = PostgreSQLGoogleConnectionRepository(
        session_factory=async_sessionmaker(
            google_database.runtime_engine, expire_on_commit=False
        ),
        context_verifier=boundary,
    )

    with pytest.raises(GoogleConnectionConflictError):
        await repository.register(
            GoogleConnectionCommand(
                connection=connection,
                idempotency_key="unapproved-postgresql-google-connection",
                audit_event_id=_id(5990),
                occurred_at=OCCURRED_AT,
            ),
            _context(boundary),
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_runtime_cannot_mint_connection_approval(
    google_database: GoogleDatabase,
) -> None:
    approval = _approval(_connection(case=7, version=1))

    with pytest.raises(ProgrammingError):
        async with google_database.runtime_engine.begin() as database_connection:
            await database_connection.execute(
                text(
                    "SELECT set_config('spine.workspace_id', :workspace_id, true), "
                    "set_config('spine.environment_id', :environment_id, true)"
                ),
                {
                    "workspace_id": str(WORKSPACE_ID),
                    "environment_id": str(ENVIRONMENT_ID),
                },
            )
            await database_connection.execute(
                insert(google_connection_approvals).values(
                    approval_id=approval.approval_id,
                    workspace_id=approval.workspace_id,
                    environment_id=approval.environment_id,
                    service_principal_id=approval.service_principal_id,
                    credential_reference=approval.credential_reference.value,
                    grant_reference=approval.grant_reference.value,
                    approved_resource_scope_references=[
                        item.value
                        for item in approval.approved_resource_scope_references
                    ],
                    authorization_generation=approval.authorization_generation,
                    configuration_generation=approval.configuration_generation,
                    status=approval.status.value,
                    recorded_at=OCCURRED_AT,
                )
            )


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_google_sheets_sync_ledger_survives_repository_recreation(
    google_database: GoogleDatabase,
) -> None:
    from sqlalchemy import text

    now = datetime(2026, 11, 27, tzinfo=timezone.utc)
    run_id = _id(60801)
    revision_id = _id(60802)
    boundary = _boundary()

    class UnitOfWork:
        def __init__(self, connection):
            self.connection = connection

        async def _execute(self, statement):
            return await self.connection.execute(statement)

        async def commit(self):
            await self.connection.commit()

        async def rollback(self):
            await self.connection.rollback()

    @asynccontextmanager
    async def uow_factory(context):
        async with google_database.runtime_engine.connect() as connection:
            await connection.begin()
            await connection.execute(
                text(
                    "SELECT set_config('spine.workspace_id', :workspace_id, true), "
                    "set_config('spine.environment_id', :environment_id, true)"
                ),
                {
                    "workspace_id": str(WORKSPACE_ID),
                    "environment_id": str(ENVIRONMENT_ID),
                },
            )
            yield UnitOfWork(connection)

    repository = PostgreSQLGoogleSheetsSyncRepository(
        uow_factory=uow_factory,
        context_factory=lambda operation, identity: _context(
            boundary, operation=operation.value
        ),
    )
    command = GoogleSheetsSyncRunCommand(
        sync_run_id=run_id,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        connection_id=_id(60803),
        resource_scope_reference=_id(60804),
        service_principal_id=SERVICE_PRINCIPAL_ID,
        access_policy_version_id=_id(60805),
        authorization_generation=1,
        configuration_generation=1,
        scope_set_digest="a" * 64,
        command_identity="google-sheets:drive-change:608",
        idempotency_key="google-sheets:drive-change:608",
        prior_committed_cursor="cursor-old",
        proposed_cursor="cursor-new",
        requested_at=now,
    )
    await repository.start_run(command)
    await repository.record_identity_disposition(
        sync_run_id=run_id,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        connection_id=command.connection_id,
        scope_identity="google-sheets-scope:v1:test",
        disposition=GoogleSheetsIdentityDisposition.QUARANTINED,
        failure_code=GoogleSheetsIdentityFailureCode.MISSING_BUSINESS_ID,
        now=now,
    )
    page = await repository.open_page(
        sync_run_id=run_id,
        scope_identity="google-sheets-scope:v1:test",
        page_token=None,
        next_page_token="page-2",
        provider_version="version-1",
        identities=("estimate:608",),
        now=now,
    )
    await repository.mark_item_succeeded(
        page.items[0].item_id, revision_id=revision_id, now=now
    )
    await repository.complete_page(page.page_id, now=now)
    await repository.mark_run(
        run_id, state=GoogleSheetsSyncRunState.SUCCEEDED, failure_code=None, now=now
    )

    reopened = PostgreSQLGoogleSheetsSyncRepository(
        uow_factory=uow_factory,
        context_factory=lambda operation, identity: _context(
            boundary, operation=operation.value
        ),
    )
    persisted = await reopened.resolve_run(run_id)
    persisted_pages = await reopened.list_pages(run_id)
    dispositions = await reopened.list_identity_dispositions(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        connection_id=command.connection_id,
        scope_identities=("google-sheets-scope:v1:test",),
    )

    assert persisted is not None
    assert persisted.state is GoogleSheetsSyncRunState.SUCCEEDED
    assert persisted.proposed_cursor == "cursor-new"
    assert len(persisted_pages) == 1
    assert persisted_pages[0].items[0].revision_id == revision_id
    assert len(dispositions) == 1
    assert dispositions[0].disposition is GoogleSheetsIdentityDisposition.QUARANTINED
    assert dispositions[0].failure_code is GoogleSheetsIdentityFailureCode.MISSING_BUSINESS_ID


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_rls_hides_other_workspace(
    google_database: GoogleDatabase,
) -> None:
    scenario = await _scenario(google_database, case=5)
    await scenario.repository.register(
        GoogleConnectionCommand(
            connection=scenario.initial,
            idempotency_key="rls-google-connection-v1",
            audit_event_id=_id(5991),
            occurred_at=OCCURRED_AT,
        ),
        scenario.context,
    )

    with pytest.raises(GoogleConnectionDeniedError):
        async with scenario.repository.admit_read(
            GoogleReadCommand(
                connection_id=scenario.initial.connection_id,
                resource_scope_reference=scenario.initial.approved_resource_scope_references[
                    0
                ],
                authorization_generation=1,
                configuration_generation=1,
            ),
            _context(
                _boundary(),
                workspace_id=OTHER_WORKSPACE_ID,
                environment_id=OTHER_ENVIRONMENT_ID,
                operation="google_connection_read",
            ),
        ):
            pytest.fail("RLS-hidden connection was admitted")

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
        approval_count = await connection.scalar(
            text("SELECT count(*) FROM spine.google_connection_approvals")
        )
    assert count == 0
    assert approval_count == 0
