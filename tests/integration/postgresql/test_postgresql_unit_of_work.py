from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedPersistenceContext,
    WorkspaceScope,
)
from spine.application.persistence.errors import ConstraintConflictError
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
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.persistence.postgresql import PostgreSQLPersistence
from tests.contracts.persistence.adapter import PersistenceAdapter
from tests.contracts.persistence.ids import synthetic_uuid
from tests.contracts.persistence.test_environment_repository_contract import (
    test_duplicate_environment_identity_raises_typed_conflict as contract_duplicate_environment,
    test_environment_cannot_be_added_for_another_workspace as contract_environment_scope,
    test_environment_commit_makes_it_visible_within_owning_workspace as contract_environment_commit,
    test_foreign_environment_is_not_disclosed as contract_foreign_environment,
    test_mismatched_environment_scope_fails_before_repository_access as contract_mismatched_environment,
)
from tests.contracts.persistence.test_unit_of_work_contract import (
    test_concurrent_duplicate_commit_has_one_logical_effect as contract_concurrent_duplicate,
    test_duplicate_workspace_identity_raises_typed_conflict as contract_duplicate_workspace,
    test_explicit_commit_makes_workspace_visible as contract_explicit_commit,
    test_explicit_rollback_is_idempotent_before_close as contract_explicit_rollback,
    test_exit_without_successful_commit_discards_workspace as contract_implicit_rollback,
    test_independent_uows_do_not_observe_uncommitted_mutations as contract_independent_uows,
    test_missing_workspace_is_normal_result_and_uow_remains_usable as contract_missing_workspace,
    test_repository_failure_is_terminal_and_discards_pending_mutations as contract_repository_failure,
    test_cancelled_commit_is_terminal_and_discards_pending_mutation as contract_cancelled_commit,
    test_cancelled_entry_is_terminal as contract_cancelled_entry,
    test_uow_concurrent_entry_fails_deterministically as contract_concurrent_entry,
    test_uow_concurrent_use_fails_deterministically as contract_concurrent_use,
    test_uow_post_close_access_fails_deterministically as contract_post_close,
    test_uow_reentry_fails_deterministically as contract_reentry,
    test_worker_and_interactive_contexts_have_same_workspace_isolation as contract_worker_parity,
)


pytestmark = [
    pytest.mark.postgresql,
    pytest.mark.asyncio(loop_scope="session"),
]

MIGRATION_ROLE = "spine_migration"
RUNTIME_ROLE = "spine_runtime"
MIGRATION_PASSWORD = "migration-test-secret"
RUNTIME_PASSWORD = "runtime-test-secret"


@dataclass(frozen=True, slots=True)
class PostgreSQLContractDatabase:
    migration_url: SecretStr
    runtime_url: SecretStr


@dataclass(frozen=True, slots=True)
class OneConnectionHarness:
    adapter: PersistenceAdapter
    engine: AsyncEngine
    migration_url: SecretStr
    workspace_statement_started: asyncio.Event


class TransactionGate:
    def __init__(self) -> None:
        self._blocking = False
        self._release = asyncio.Event()
        self._release.set()

    async def wait(self) -> None:
        if self._blocking:
            await self._release.wait()

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        self._blocking = True
        self._release.clear()
        try:
            yield
        finally:
            self._blocking = False
            self._release.set()


class TransactionControlledSession:
    """Delegate real SQL while allowing contract tests to pause lifecycle awaits."""

    def __init__(self, session: AsyncSession, gate: TransactionGate) -> None:
        self._session = session
        self._gate = gate

    async def begin(self) -> object:
        await self._gate.wait()
        return await self._session.begin()

    async def execute(self, statement: object, parameters: object | None = None) -> object:
        return await self._session.execute(statement, parameters)

    async def commit(self) -> None:
        await self._gate.wait()
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()

    async def close(self) -> None:
        await self._session.close()


def _role_url(provision: TestDatabaseProvision, role: str, password: str) -> SecretStr:
    value = make_url(provision.url.get_secret_value()).set(
        username=role,
        password=password,
    )
    return SecretStr(value.render_as_string(hide_password=False))


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def postgresql_contract_database(
    postgresql_provision: TestDatabaseProvision,
) -> AsyncIterator[PostgreSQLContractDatabase]:
    operator = OperatorDatabaseSettings(
        url=postgresql_provision.url,
        database_name=postgresql_provision.target.database_name,
        migration_role=MIGRATION_ROLE,
        migration_password=MIGRATION_PASSWORD,
        runtime_role=RUNTIME_ROLE,
        runtime_password=RUNTIME_PASSWORD,
    )
    await bootstrap_database_roles(operator)
    migration_url = _role_url(
        postgresql_provision,
        MIGRATION_ROLE,
        MIGRATION_PASSWORD,
    )
    migration = MigrationDatabaseSettings(
        url=migration_url,
        migration_role=MIGRATION_ROLE,
        runtime_role=RUNTIME_ROLE,
    )
    await asyncio.to_thread(upgrade_database, migration)
    database = PostgreSQLContractDatabase(
        migration_url=migration_url,
        runtime_url=_role_url(
            postgresql_provision,
            RUNTIME_ROLE,
            RUNTIME_PASSWORD,
        ),
    )
    yield database

    engine = create_async_engine(migration_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE TABLE spine.initial_workspace_bootstrap, "
                    "spine.environments, spine.workspaces CASCADE"
                )
            )
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def postgresql_adapter(
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> AsyncIterator[PersistenceAdapter]:
    engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        pool_size=5,
        max_overflow=0,
        pool_pre_ping=True,
        pool_reset_on_return="rollback",
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    session_factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
        autoflush=False,
    )
    transaction_gate = TransactionGate()
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(950),
        secret=b"issue-45-postgresql-contract-secret",
    )
    persistence = PostgreSQLPersistence(
        session_factory=lambda: TransactionControlledSession(
            session_factory(), transaction_gate
        ),  # type: ignore[arg-type]
        context_verifier=boundary,
    )

    def workspace_context(workspace_id: UUID) -> TrustedPersistenceContext:
        return boundary.interactive(
            scope=WorkspaceScope(workspace_id=workspace_id),
            acting_subject_id=synthetic_uuid(951),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation("workspace_repository"),
            trace_id=synthetic_uuid(952),
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
            service_principal_id=synthetic_uuid(953),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation("environment_repository"),
            trace_id=synthetic_uuid(954),
        )

    def worker_workspace_context(workspace_id: UUID) -> TrustedPersistenceContext:
        return boundary.worker(
            scope=WorkspaceScope(workspace_id=workspace_id),
            service_principal_id=synthetic_uuid(955),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation("workspace_repository"),
            trace_id=synthetic_uuid(956),
        )

    @asynccontextmanager
    async def hold_transactions() -> AsyncIterator[None]:
        async with transaction_gate.hold():
            yield

    try:
        yield PersistenceAdapter(
            uow_factory=persistence.tenant_uow_factory,
            workspace_context=workspace_context,
            worker_workspace_context=worker_workspace_context,
            environment_context=environment_context,
            hold_transactions=hold_transactions,
        )
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def one_connection_harness(
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> AsyncIterator[OneConnectionHarness]:
    engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        pool_size=1,
        max_overflow=0,
        pool_timeout=1,
        pool_pre_ping=True,
        pool_reset_on_return="rollback",
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(960),
        secret=b"issue-45-one-connection-secret-001",
    )
    persistence = PostgreSQLPersistence(
        session_factory=async_sessionmaker(
            engine,
            expire_on_commit=False,
            autoflush=False,
        ),
        context_verifier=boundary,
    )
    workspace_statement_started = asyncio.Event()

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def observe_workspace_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if "FROM spine.workspaces" in statement:
            workspace_statement_started.set()

    def workspace_context(workspace_id: UUID) -> TrustedPersistenceContext:
        return boundary.interactive(
            scope=WorkspaceScope(workspace_id=workspace_id),
            acting_subject_id=synthetic_uuid(961),
            purpose=PersistencePurpose("pool_leakage_test"),
            operation=PersistenceOperation("workspace_repository"),
            trace_id=synthetic_uuid(962),
        )

    def worker_workspace_context(workspace_id: UUID) -> TrustedPersistenceContext:
        return boundary.worker(
            scope=WorkspaceScope(workspace_id=workspace_id),
            service_principal_id=synthetic_uuid(965),
            purpose=PersistencePurpose("pool_leakage_test"),
            operation=PersistenceOperation("workspace_repository"),
            trace_id=synthetic_uuid(966),
        )

    @asynccontextmanager
    async def hold_transactions() -> AsyncIterator[None]:
        yield

    adapter = PersistenceAdapter(
        uow_factory=persistence.tenant_uow_factory,
        workspace_context=workspace_context,
        worker_workspace_context=worker_workspace_context,
        environment_context=lambda workspace_id, environment_id: boundary.worker(
            scope=EnvironmentScope(
                workspace_id=workspace_id,
                environment_id=environment_id,
            ),
            service_principal_id=synthetic_uuid(963),
            purpose=PersistencePurpose("pool_leakage_test"),
            operation=PersistenceOperation("environment_repository"),
            trace_id=synthetic_uuid(964),
        ),
        hold_transactions=hold_transactions,
    )
    try:
        yield OneConnectionHarness(
            adapter=adapter,
            engine=engine,
            migration_url=postgresql_contract_database.migration_url,
            workspace_statement_started=workspace_statement_started,
        )
    finally:
        await engine.dispose()


ContractTest = Callable[[PersistenceAdapter], Awaitable[None]]


@pytest.mark.parametrize(
    "contract",
    (
        contract_explicit_commit,
        contract_missing_workspace,
        contract_worker_parity,
        contract_independent_uows,
        contract_concurrent_duplicate,
        contract_explicit_rollback,
        contract_repository_failure,
        contract_duplicate_workspace,
        contract_reentry,
        contract_concurrent_entry,
        contract_concurrent_use,
        contract_post_close,
        contract_cancelled_entry,
        contract_cancelled_commit,
        contract_environment_commit,
        contract_environment_scope,
        contract_foreign_environment,
        contract_duplicate_environment,
        contract_mismatched_environment,
    ),
    ids=lambda contract: contract.__name__.removeprefix("test_"),
)
async def test_postgresql_adapter_satisfies_shared_contract(
    postgresql_adapter: PersistenceAdapter,
    contract: ContractTest,
) -> None:
    await contract(postgresql_adapter)


@pytest.mark.parametrize("raise_error", (False, True), ids=("normal", "exception"))
async def test_postgresql_adapter_implicitly_rolls_back(
    postgresql_adapter: PersistenceAdapter,
    raise_error: bool,
) -> None:
    await contract_implicit_rollback(postgresql_adapter, raise_error)


async def test_named_check_constraint_maps_to_stable_conflict(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(720)
    invalid = Workspace(id=workspace_id, slug="", display_name="Invalid")

    with pytest.raises(
        ConstraintConflictError,
        match="Workspace data violates persistence constraints",
    ):
        async with postgresql_adapter.uow_factory(
            postgresql_adapter.workspace_context(workspace_id)
        ) as uow:
            await uow.workspaces.add(invalid)


async def test_named_foreign_key_maps_to_stable_conflict(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(721)
    invalid = Environment(
        id=synthetic_uuid(722),
        workspace_id=workspace_id,
        kind=EnvironmentKind.DEVELOPMENT,
        display_name="Missing owner",
    )

    with pytest.raises(
        ConstraintConflictError,
        match="Owning Workspace does not exist",
    ):
        async with postgresql_adapter.uow_factory(
            postgresql_adapter.workspace_context(workspace_id)
        ) as uow:
            await uow.environments.add(invalid)


async def test_named_unique_constraint_maps_to_stable_conflict(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    first_id = synthetic_uuid(723)
    second_id = synthetic_uuid(724)
    first = Workspace(id=first_id, slug="shared-slug", display_name="First")
    second = Workspace(id=second_id, slug="shared-slug", display_name="Second")

    async with postgresql_adapter.uow_factory(
        postgresql_adapter.workspace_context(first_id)
    ) as uow:
        await uow.workspaces.add(first)
        await uow.commit()

    with pytest.raises(
        ConstraintConflictError,
        match="Workspace identity already exists",
    ):
        async with postgresql_adapter.uow_factory(
            postgresql_adapter.workspace_context(second_id)
        ) as uow:
            await uow.workspaces.add(second)


async def _assert_followup_scope_is_isolated(
    harness: OneConnectionHarness,
    protected_workspace_id: UUID,
) -> None:
    async with harness.engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT current_setting('spine.workspace_id', true), "
                    "current_setting('spine.environment_id', true)"
                )
            )
        ).one()
        visible_without_context = (
            await connection.execute(text("SELECT id FROM spine.workspaces"))
        ).scalars().all()
    assert tuple(row) in {(None, None), ("", ""), (None, ""), ("", None)}
    assert visible_without_context == []

    other_workspace_id = synthetic_uuid(protected_workspace_id.int % 1000 + 800)
    other_context = harness.adapter.workspace_context(other_workspace_id)
    async with harness.adapter.uow_factory(other_context) as uow:
        assert await uow.workspaces.resolve(protected_workspace_id) is None


async def test_single_connection_clears_tenant_settings_after_every_terminal_path(
    one_connection_harness: OneConnectionHarness,
) -> None:
    adapter = one_connection_harness.adapter
    committed_id = synthetic_uuid(701)
    rolled_back_id = synthetic_uuid(702)
    failed_id = synthetic_uuid(703)

    async with adapter.uow_factory(adapter.workspace_context(committed_id)) as uow:
        await uow.workspaces.add(
            Workspace(
                id=committed_id,
                slug="pool-committed",
                display_name="Pool Committed",
            )
        )
        await uow.commit()
    await _assert_followup_scope_is_isolated(
        one_connection_harness,
        committed_id,
    )

    async with adapter.uow_factory(adapter.workspace_context(rolled_back_id)) as uow:
        await uow.workspaces.add(
            Workspace(
                id=rolled_back_id,
                slug="pool-rolled-back",
                display_name="Pool Rolled Back",
            )
        )
        await uow.rollback()
    await _assert_followup_scope_is_isolated(
        one_connection_harness,
        rolled_back_id,
    )

    async with adapter.uow_factory(adapter.workspace_context(failed_id)) as uow:
        candidate = Workspace(
            id=failed_id,
            slug="pool-failed",
            display_name="Pool Failed",
        )
        await uow.workspaces.add(candidate)
        with pytest.raises(ConstraintConflictError):
            await uow.workspaces.add(candidate.model_copy(deep=True))
    await _assert_followup_scope_is_isolated(
        one_connection_harness,
        failed_id,
    )


async def test_cancelled_statement_releases_single_connection_without_scope_leakage(
    one_connection_harness: OneConnectionHarness,
) -> None:
    adapter = one_connection_harness.adapter
    workspace_id = synthetic_uuid(704)
    entered = asyncio.Event()
    execute_query = asyncio.Event()

    async def blocked_reader() -> None:
        async with adapter.uow_factory(adapter.workspace_context(workspace_id)) as uow:
            entered.set()
            await execute_query.wait()
            await uow.workspaces.resolve(workspace_id)

    task = asyncio.create_task(blocked_reader())
    await entered.wait()
    migration_engine = create_async_engine(
        one_connection_harness.migration_url.get_secret_value(),
        hide_parameters=True,
    )
    try:
        async with migration_engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(
                text("LOCK TABLE spine.workspaces IN ACCESS EXCLUSIVE MODE")
            )
            one_connection_harness.workspace_statement_started.clear()
            execute_query.set()
            await asyncio.wait_for(
                one_connection_harness.workspace_statement_started.wait(),
                timeout=2,
            )
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await transaction.rollback()
    finally:
        await migration_engine.dispose()

    await _assert_followup_scope_is_isolated(
        one_connection_harness,
        workspace_id,
    )
    async with adapter.uow_factory(adapter.workspace_context(workspace_id)) as uow:
        assert await uow.workspaces.resolve(workspace_id) is None
