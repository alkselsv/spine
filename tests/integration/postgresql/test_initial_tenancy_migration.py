from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from spine.application.persistence.errors import ConstraintConflictError
from spine.domain.workspaces import Workspace
from spine.infrastructure.db.initial_workspace import (
    PostgreSQLInitialWorkspaceBootstrap,
)
from spine.infrastructure.db.migrations import (
    MigrationPreflightError,
    upgrade_database,
)
from spine.infrastructure.db.operator import bootstrap_database_roles
from spine.infrastructure.db.settings import (
    MigrationDatabaseSettings,
    OperatorDatabaseSettings,
)
from spine.infrastructure.db.test_harness import TestDatabaseProvision
from spine.infrastructure.persistence.contexts import (
    create_initial_workspace_bootstrap_authority,
)


pytestmark = pytest.mark.postgresql
MIGRATION_ROLE = "spine_migration"
RUNTIME_ROLE = "spine_runtime"
MIGRATION_PASSWORD = "migration-test-secret"
RUNTIME_PASSWORD = "runtime-test-secret"
INITIAL_REVISION = "20261009_01"


@dataclass(frozen=True, slots=True)
class MigratedDatabase:
    operator: OperatorDatabaseSettings
    migration: MigrationDatabaseSettings
    runtime_url: SecretStr


def _role_url(provision: TestDatabaseProvision, role: str, password: str) -> SecretStr:
    value = make_url(provision.url.get_secret_value()).set(
        username=role,
        password=password,
    )
    return SecretStr(value.render_as_string(hide_password=False))


@asynccontextmanager
async def _connection(url: SecretStr) -> AsyncIterator[AsyncConnection]:
    engine = create_async_engine(url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.connect() as connection:
            yield connection
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def migrated_database(
    postgresql_provision: TestDatabaseProvision,
) -> MigratedDatabase:
    operator = OperatorDatabaseSettings(
        url=postgresql_provision.url,
        database_name=postgresql_provision.target.database_name,
        migration_role=MIGRATION_ROLE,
        migration_password=MIGRATION_PASSWORD,
        runtime_role=RUNTIME_ROLE,
        runtime_password=RUNTIME_PASSWORD,
    )
    await bootstrap_database_roles(operator)
    migration = MigrationDatabaseSettings(
        url=_role_url(postgresql_provision, MIGRATION_ROLE, MIGRATION_PASSWORD),
        migration_role=MIGRATION_ROLE,
        runtime_role=RUNTIME_ROLE,
    )
    await asyncio.to_thread(upgrade_database, migration)
    return MigratedDatabase(
        operator=operator,
        migration=migration,
        runtime_url=_role_url(
            postgresql_provision,
            RUNTIME_ROLE,
            RUNTIME_PASSWORD,
        ),
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_empty_database_migrates_to_one_initial_head_and_owned_schema(
    migrated_database: MigratedDatabase,
) -> None:
    async with _connection(migrated_database.migration.url) as connection:
        heads = (
            await connection.execute(
                text("SELECT version_num FROM spine.alembic_version")
            )
        ).scalars().all()
        owner = await connection.scalar(
            text(
                "SELECT pg_get_userbyid(nspowner) FROM pg_namespace "
                "WHERE nspname = 'spine'"
            )
        )
    assert heads == [INITIAL_REVISION]
    assert owner == MIGRATION_ROLE


@pytest.mark.asyncio(loop_scope="session")
async def test_initial_schema_has_named_tenancy_constraints_and_postgresql_types(
    migrated_database: MigratedDatabase,
) -> None:
    async with _connection(migrated_database.migration.url) as connection:
        constraints = set(
            (
                await connection.execute(
                    text(
                        "SELECT conname FROM pg_constraint AS constraint_record "
                        "JOIN pg_namespace AS namespace "
                        "ON namespace.oid = constraint_record.connamespace "
                        "WHERE namespace.nspname = 'spine'"
                    )
                )
            ).scalars()
        )
        columns = {
            (row.table_name, row.column_name): (
                row.data_type,
                row.is_nullable,
                row.column_default,
            )
            for row in (
                await connection.execute(
                    text(
                        "SELECT table_name, column_name, data_type, is_nullable, "
                        "column_default FROM information_schema.columns "
                        "WHERE table_schema = 'spine'"
                    )
                )
            ).mappings()
        }
        indexes = set(
            (
                await connection.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = 'spine'"
                    )
                )
            ).scalars()
        )
        tables = set(
            (
                await connection.execute(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'spine'"
                    )
                )
            ).scalars()
        )
        owners = set(
            (
                await connection.execute(
                    text(
                        "SELECT tableowner FROM pg_tables WHERE schemaname = 'spine'"
                    )
                )
            ).scalars()
        )
    assert tables == {
        "alembic_version",
        "environments",
        "initial_workspace_bootstrap",
        "workspaces",
    }
    assert constraints == {
        "alembic_version_pkc",
        "ck_environments_display_name_not_empty",
        "ck_environments_kind",
        "ck_initial_workspace_bootstrap_singleton",
        "ck_workspaces_display_name_not_empty",
        "ck_workspaces_slug_not_empty",
        "fk_environments_workspace_id_workspaces",
        "fk_initial_workspace_bootstrap_workspace_id_workspaces",
        "pk_environments",
        "pk_initial_workspace_bootstrap",
        "pk_workspaces",
        "uq_environments_workspace_id_id",
        "uq_initial_workspace_bootstrap_action_id",
        "uq_initial_workspace_bootstrap_workspace_id",
        "uq_workspaces_slug",
    }
    assert set(columns) == {
        ("alembic_version", "version_num"),
        ("environments", "created_at"),
        ("environments", "display_name"),
        ("environments", "id"),
        ("environments", "kind"),
        ("environments", "workspace_id"),
        ("initial_workspace_bootstrap", "action_id"),
        ("initial_workspace_bootstrap", "created_at"),
        ("initial_workspace_bootstrap", "executed_by"),
        ("initial_workspace_bootstrap", "singleton"),
        ("initial_workspace_bootstrap", "workspace_id"),
        ("workspaces", "created_at"),
        ("workspaces", "display_name"),
        ("workspaces", "id"),
        ("workspaces", "slug"),
    }
    assert columns[("workspaces", "id")] == ("uuid", "NO", None)
    assert columns[("environments", "id")] == ("uuid", "NO", None)
    for table in ("workspaces", "environments", "initial_workspace_bootstrap"):
        data_type, nullable, default = columns[(table, "created_at")]
        assert data_type == "timestamp with time zone"
        assert nullable == "NO"
        assert default is not None and "CURRENT_TIMESTAMP" in default.upper()
    assert indexes == {
        "alembic_version_pkc",
        "ix_environments_workspace_id",
        "pk_environments",
        "pk_initial_workspace_bootstrap",
        "pk_workspaces",
        "uq_environments_workspace_id_id",
        "uq_initial_workspace_bootstrap_action_id",
        "uq_initial_workspace_bootstrap_workspace_id",
        "uq_workspaces_slug",
    }
    assert owners == {MIGRATION_ROLE}


@pytest.mark.asyncio(loop_scope="session")
async def test_environment_workspace_foreign_key_rejects_unknown_owner(
    migrated_database: MigratedDatabase,
) -> None:
    with pytest.raises(IntegrityError):
        async with _connection(migrated_database.migration.url) as connection:
            await connection.execute(
                text(
                    "INSERT INTO spine.environments "
                    "(id, workspace_id, kind, display_name) "
                    "VALUES (:id, :workspace_id, 'development', 'Invalid')"
                ),
                {
                    "id": UUID("50000000-0000-0000-0000-000000000001"),
                    "workspace_id": UUID("50000000-0000-0000-0000-000000000002"),
                },
            )


@pytest.mark.asyncio(loop_scope="session")
async def test_runtime_role_is_restricted_non_owner_without_tenant_table_access(
    migrated_database: MigratedDatabase,
) -> None:
    async with _connection(migrated_database.migration.url) as connection:
        role = (
            await connection.execute(
                text(
                    "SELECT rolsuper, rolcreatedb, rolcreaterole, rolinherit, "
                    "rolbypassrls FROM pg_roles WHERE rolname = :role"
                ),
                {"role": RUNTIME_ROLE},
            )
        ).one()
        owner = await connection.scalar(
            text(
                "SELECT pg_get_userbyid(nspowner) FROM pg_namespace "
                "WHERE nspname = 'spine'"
            )
        )
    assert tuple(role) == (False, False, False, False, False)
    assert owner != RUNTIME_ROLE

    with pytest.raises(ProgrammingError):
        async with _connection(migrated_database.runtime_url) as connection:
            await connection.execute(text("SELECT id FROM spine.workspaces"))
    with pytest.raises(ProgrammingError):
        async with _connection(migrated_database.runtime_url) as connection:
            await connection.execute(text("SET ROLE spine_migration"))


@pytest.mark.asyncio(loop_scope="session")
async def test_operator_reconciles_unsafe_existing_runtime_membership(
    migrated_database: MigratedDatabase,
    postgresql_provision: TestDatabaseProvision,
) -> None:
    async with _connection(postgresql_provision.url) as connection:
        await connection.execute(text("GRANT spine_migration TO spine_runtime"))
        await connection.commit()

    await bootstrap_database_roles(migrated_database.operator)
    async with _connection(migrated_database.migration.url) as connection:
        can_assume = await connection.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_auth_members AS membership "
                "JOIN pg_roles AS parent ON parent.oid = membership.roleid "
                "JOIN pg_roles AS member ON member.oid = membership.member "
                "WHERE parent.rolname = :parent AND member.rolname = :member)"
            ),
            {"parent": MIGRATION_ROLE, "member": RUNTIME_ROLE},
        )
    assert can_assume is False
    await asyncio.to_thread(upgrade_database, migrated_database.migration)


@pytest.mark.asyncio(loop_scope="session")
async def test_real_preflight_rejects_unsafe_role_without_schema_changes(
    migrated_database: MigratedDatabase,
    postgresql_provision: TestDatabaseProvision,
) -> None:
    async with _connection(migrated_database.migration.url) as connection:
        revision_before = await connection.scalar(
            text("SELECT version_num FROM spine.alembic_version")
        )
    async with _connection(postgresql_provision.url) as connection:
        await connection.execute(text("ALTER ROLE spine_runtime BYPASSRLS"))
        await connection.commit()
    try:
        with pytest.raises(MigrationPreflightError):
            await asyncio.to_thread(upgrade_database, migrated_database.migration)
    finally:
        async with _connection(postgresql_provision.url) as connection:
            await connection.execute(text("ALTER ROLE spine_runtime NOBYPASSRLS"))
            await connection.commit()
    async with _connection(migrated_database.migration.url) as connection:
        revision_after = await connection.scalar(
            text("SELECT version_num FROM spine.alembic_version")
        )
    assert revision_after == revision_before


@pytest.mark.asyncio(loop_scope="session")
async def test_initial_workspace_is_created_once_by_sealed_audited_action(
    migrated_database: MigratedDatabase,
) -> None:
    authority = create_initial_workspace_bootstrap_authority()
    adapter = PostgreSQLInitialWorkspaceBootstrap(
        settings=migrated_database.migration,
        authority=authority,
    )
    expected = Workspace(
        id=UUID("10000000-0000-0000-0000-000000000001"),
        slug="initial",
        display_name="Initial Workspace",
    )

    await adapter.create_initial_workspace(authority, expected)
    with pytest.raises(ConstraintConflictError, match="sealed"):
        await adapter.create_initial_workspace(authority, expected)

    async with _connection(migrated_database.migration.url) as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT workspace.id, bootstrap.executed_by "
                    "FROM spine.workspaces AS workspace "
                    "JOIN spine.initial_workspace_bootstrap AS bootstrap "
                    "ON bootstrap.workspace_id = workspace.id"
                )
            )
        ).one()
    assert row.id == expected.id
    assert row.executed_by == MIGRATION_ROLE


def _write_failing_revision(tmp_path: Path) -> Config:
    source = Path(__file__).parents[3] / "migrations"
    target = tmp_path / "migrations"
    (target / "versions").mkdir(parents=True)
    shutil.copy(source / "env.py", target / "env.py")
    shutil.copy(source / "script.py.mako", target / "script.py.mako")
    shutil.copy(
        source / "versions" / "20261009_01_initial_tenancy.py",
        target / "versions" / "20261009_01_initial_tenancy.py",
    )
    (target / "versions" / "20261009_02_injected_failure.py").write_text(
        "from alembic import op\n"
        "import sqlalchemy as sa\n"
        "revision = '20261009_02'\n"
        "down_revision = '20261009_01'\n"
        "branch_labels = None\n"
        "depends_on = None\n"
        "def upgrade():\n"
        "    op.create_table('injected_failure', sa.Column('id', sa.Integer()), schema='spine')\n"
        "    raise RuntimeError('injected migration failure')\n"
        "def downgrade():\n"
        "    pass\n",
        encoding="utf-8",
    )
    config = Config()
    config.set_main_option("script_location", str(target))
    return config


@pytest.mark.asyncio(loop_scope="session")
async def test_transactional_migration_failure_preserves_prior_schema(
    migrated_database: MigratedDatabase,
    tmp_path: Path,
) -> None:
    async with _connection(migrated_database.migration.url) as connection:
        workspaces_before = await connection.scalar(
            text("SELECT count(*) FROM spine.workspaces")
        )
    config = _write_failing_revision(tmp_path)
    config.attributes["migration_settings"] = migrated_database.migration
    with pytest.raises(RuntimeError, match="injected migration failure"):
        await asyncio.to_thread(command.upgrade, config, "head")

    async with _connection(migrated_database.migration.url) as connection:
        revision = await connection.scalar(
            text("SELECT version_num FROM spine.alembic_version")
        )
        workspaces_after = await connection.scalar(
            text("SELECT count(*) FROM spine.workspaces")
        )
        failure_table = await connection.scalar(
            text("SELECT to_regclass('spine.injected_failure')")
        )
    assert revision == INITIAL_REVISION
    assert workspaces_after == workspaces_before
    assert failure_table is None


@pytest.mark.asyncio(loop_scope="session")
async def test_unknown_revision_is_rejected_without_schema_changes(
    migrated_database: MigratedDatabase,
) -> None:
    async with _connection(migrated_database.migration.url) as connection:
        workspaces_before = await connection.scalar(
            text("SELECT count(*) FROM spine.workspaces")
        )
        await connection.execute(
            text("UPDATE spine.alembic_version SET version_num = 'unknown_revision'")
        )
        await connection.commit()
    try:
        with pytest.raises(CommandError, match="unknown_revision"):
            await asyncio.to_thread(upgrade_database, migrated_database.migration)
    finally:
        async with _connection(migrated_database.migration.url) as connection:
            await connection.execute(
                text("UPDATE spine.alembic_version SET version_num = :revision"),
                {"revision": INITIAL_REVISION},
            )
            await connection.commit()
    async with _connection(migrated_database.migration.url) as connection:
        workspaces_after = await connection.scalar(
            text("SELECT count(*) FROM spine.workspaces")
        )
    assert workspaces_after == workspaces_before
