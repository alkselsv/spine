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
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from spine.application.persistence.errors import ConstraintConflictError
from spine.domain.workspaces import Workspace
from spine.infrastructure.db.initial_workspace import (
    PostgreSQLInitialWorkspaceBootstrap,
)
from spine.infrastructure.db.migrations import (
    MigrationPreflightError,
    locate_migration_assets,
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
RLS_REVISION = "20261009_02"
IDEMPOTENCY_REVISION = "20261010_03"
OUTBOX_REVISION = "20261010_04"
PRE_K0_REVISION = "20261010_05"
K0_REVISION = "20261010_06"
HEAD_REVISION = "20261010_07"
WORKSPACE_A = UUID("20000000-0000-0000-0000-000000000001")
WORKSPACE_B = UUID("20000000-0000-0000-0000-000000000002")
ENVIRONMENT_A = UUID("30000000-0000-0000-0000-000000000001")
ENVIRONMENT_A_SECOND = UUID("30000000-0000-0000-0000-000000000002")
ENVIRONMENT_B = UUID("30000000-0000-0000-0000-000000000003")

AUTHORIZATION_DIRECTORY_TABLES = {
    "authentication_alias_bindings",
    "authorization_generations",
    "canonical_human_identities",
    "canonical_human_identity_states",
    "environment_memberships",
    "environment_role_bindings",
    "workspace_memberships",
}
AUTHORIZATION_DIRECTORY_CONSTRAINTS = {
    "ck_authentication_alias_bindings_issuer_length",
    "ck_authentication_alias_bindings_recorded_by_length",
    "ck_authentication_alias_bindings_status",
    "ck_authentication_alias_bindings_subject_length",
    "ck_authentication_alias_bindings_version_positive",
    "ck_authorization_generations_generation_positive",
    "ck_canonical_human_identities_created_by_length",
    "ck_canonical_human_identity_states_recorded_by_length",
    "ck_canonical_human_identity_states_status",
    "ck_canonical_human_identity_states_version_positive",
    "ck_environment_memberships_recorded_by_length",
    "ck_environment_memberships_status",
    "ck_environment_memberships_version_positive",
    "ck_environment_role_bindings_recorded_by_length",
    "ck_environment_role_bindings_role",
    "ck_environment_role_bindings_status",
    "ck_environment_role_bindings_version_positive",
    "ck_workspace_memberships_recorded_by_length",
    "ck_workspace_memberships_status",
    "ck_workspace_memberships_version_positive",
    "fk_alias_bindings_identity",
    "fk_authorization_generations_scope_environments",
    "fk_environment_memberships_identity",
    "fk_environment_memberships_scope_environments",
    "fk_environment_role_bindings_identity",
    "fk_environment_role_bindings_scope_environments",
    "fk_identity_states_identity",
    "fk_workspace_memberships_identity",
    "fk_workspace_memberships_workspace_id_workspaces",
    "pk_authentication_alias_bindings",
    "pk_authorization_generations",
    "pk_canonical_human_identities",
    "pk_canonical_human_identity_states",
    "pk_environment_memberships",
    "pk_environment_role_bindings",
    "pk_workspace_memberships",
    "uq_authentication_alias_bindings_alias_version",
    "uq_authentication_alias_bindings_change_id",
    "uq_canonical_human_identities_created_change_id",
    "uq_canonical_human_identity_states_change_id",
    "uq_environment_memberships_change_id",
    "uq_environment_memberships_scope_version",
    "uq_environment_role_bindings_change_id",
    "uq_environment_role_bindings_scope_role_version",
    "uq_workspace_memberships_change_id",
    "uq_workspace_memberships_scope_version",
}
AUTHORIZATION_DIRECTORY_INDEXES = {
    "ix_environment_memberships_current_lookup",
    "ix_environment_role_bindings_current_lookup",
    "ix_workspace_memberships_current_lookup",
    "pk_authentication_alias_bindings",
    "pk_authorization_generations",
    "pk_canonical_human_identities",
    "pk_canonical_human_identity_states",
    "pk_environment_memberships",
    "pk_environment_role_bindings",
    "pk_workspace_memberships",
    "uq_authentication_alias_bindings_alias_version",
    "uq_authentication_alias_bindings_change_id",
    "uq_canonical_human_identities_created_change_id",
    "uq_canonical_human_identity_states_change_id",
    "uq_environment_memberships_change_id",
    "uq_environment_memberships_scope_version",
    "uq_environment_role_bindings_change_id",
    "uq_environment_role_bindings_scope_role_version",
    "uq_workspace_memberships_change_id",
    "uq_workspace_memberships_scope_version",
}
AUTHORIZATION_DIRECTORY_COLUMNS = {
    ("canonical_human_identities", column)
    for column in ("identity_id", "created_change_id", "created_by", "created_at")
} | {
    ("canonical_human_identity_states", column)
    for column in (
        "identity_id", "version", "status", "change_id", "recorded_by", "recorded_at"
    )
} | {
    ("authentication_alias_bindings", column)
    for column in (
        "binding_id", "issuer", "subject", "canonical_human_identity_id",
        "version", "status", "change_id", "recorded_by", "recorded_at",
    )
} | {
    ("workspace_memberships", column)
    for column in (
        "membership_id", "workspace_id", "canonical_human_identity_id",
        "version", "status", "change_id", "recorded_by", "recorded_at",
    )
} | {
    ("environment_memberships", column)
    for column in (
        "membership_id", "workspace_id", "environment_id",
        "canonical_human_identity_id", "version", "status", "change_id",
        "recorded_by", "recorded_at",
    )
} | {
    ("environment_role_bindings", column)
    for column in (
        "binding_id", "workspace_id", "environment_id",
        "canonical_human_identity_id", "role", "version", "status",
        "change_id", "recorded_by", "recorded_at",
    )
} | {
    ("authorization_generations", column)
    for column in ("workspace_id", "environment_id", "generation", "updated_at")
}


@dataclass(frozen=True, slots=True)
class MigratedDatabase:
    operator: OperatorDatabaseSettings
    migration: MigrationDatabaseSettings
    runtime_url: SecretStr
    retained_revisions: tuple[str, ...]


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
    await asyncio.to_thread(upgrade_database, migration, revision=INITIAL_REVISION)
    retained_revisions: list[str] = []
    async with _connection(migration.url) as connection:
        retained_revisions.append(
            await connection.scalar(
                text("SELECT version_num FROM spine.alembic_version")
            )
        )
    await asyncio.to_thread(upgrade_database, migration, revision=RLS_REVISION)
    async with _connection(migration.url) as connection:
        retained_revisions.append(
            await connection.scalar(
                text("SELECT version_num FROM spine.alembic_version")
            )
        )
    await asyncio.to_thread(upgrade_database, migration, revision=IDEMPOTENCY_REVISION)
    async with _connection(migration.url) as connection:
        retained_revisions.append(
            await connection.scalar(
                text("SELECT version_num FROM spine.alembic_version")
            )
        )
    await asyncio.to_thread(upgrade_database, migration, revision=OUTBOX_REVISION)
    async with _connection(migration.url) as connection:
        retained_revisions.append(
            await connection.scalar(
                text("SELECT version_num FROM spine.alembic_version")
            )
        )
    await asyncio.to_thread(upgrade_database, migration, revision=PRE_K0_REVISION)
    async with _connection(migration.url) as connection:
        retained_revisions.append(
            await connection.scalar(
                text("SELECT version_num FROM spine.alembic_version")
            )
        )
    await asyncio.to_thread(upgrade_database, migration, revision=K0_REVISION)
    async with _connection(migration.url) as connection:
        retained_revisions.append(
            await connection.scalar(
                text("SELECT version_num FROM spine.alembic_version")
            )
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
        retained_revisions=tuple(retained_revisions),
    )


@pytest_asyncio.fixture(loop_scope="session")
async def tenant_rows(migrated_database: MigratedDatabase) -> AsyncIterator[None]:
    async with _connection(migrated_database.migration.url) as connection:
        await connection.execute(
            text(
                "INSERT INTO spine.workspaces (id, slug, display_name) VALUES "
                "(:workspace_a, 'workspace-a', 'Workspace A'), "
                "(:workspace_b, 'workspace-b', 'Workspace B')"
            ),
            {"workspace_a": WORKSPACE_A, "workspace_b": WORKSPACE_B},
        )
        await connection.execute(
            text(
                "INSERT INTO spine.environments "
                "(id, workspace_id, kind, display_name) VALUES "
                "(:environment_a, :workspace_a, 'development', 'A Development'), "
                "(:environment_a_second, :workspace_a, 'staging', 'A Staging'), "
                "(:environment_b, :workspace_b, 'production', 'B Production')"
            ),
            {
                "environment_a": ENVIRONMENT_A,
                "environment_a_second": ENVIRONMENT_A_SECOND,
                "environment_b": ENVIRONMENT_B,
                "workspace_a": WORKSPACE_A,
                "workspace_b": WORKSPACE_B,
            },
        )
        await connection.commit()
    yield
    async with _connection(migrated_database.migration.url) as connection:
        await connection.execute(
            text(
                "DELETE FROM spine.environments WHERE id IN "
                "(:environment_a, :environment_a_second, :environment_b)"
            ),
            {
                "environment_a": ENVIRONMENT_A,
                "environment_a_second": ENVIRONMENT_A_SECOND,
                "environment_b": ENVIRONMENT_B,
            },
        )
        await connection.execute(
            text(
                "DELETE FROM spine.workspaces WHERE id IN "
                "(:workspace_a, :workspace_b)"
            ),
            {"workspace_a": WORKSPACE_A, "workspace_b": WORKSPACE_B},
        )
        await connection.commit()


async def _set_tenant_context(
    connection: AsyncConnection,
    *,
    workspace_id: UUID | str | None,
    environment_id: UUID | str | None = None,
) -> None:
    if workspace_id is not None:
        await connection.execute(
            text("SELECT set_config('spine.workspace_id', :value, true)"),
            {"value": str(workspace_id)},
        )
    if environment_id is not None:
        await connection.execute(
            text("SELECT set_config('spine.environment_id', :value, true)"),
            {"value": str(environment_id)},
        )


async def _assert_final_catalog_after_upgrade(connection: AsyncConnection) -> None:
    tables = set(
        (
            await connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'spine'")
            )
        ).scalars()
    )
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
    indexes = set(
        (
            await connection.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname = 'spine'")
            )
        ).scalars()
    )
    owners = set(
        (
            await connection.execute(
                text("SELECT tableowner FROM pg_tables WHERE schemaname = 'spine'")
            )
        ).scalars()
    )
    rls = {
        row.relname: (row.relrowsecurity, row.relforcerowsecurity)
        for row in (
            await connection.execute(
                text(
                    "SELECT relation.relname, relation.relrowsecurity, "
                    "relation.relforcerowsecurity FROM pg_class AS relation "
                    "JOIN pg_namespace AS namespace "
                    "ON namespace.oid = relation.relnamespace "
                    "WHERE namespace.nspname = 'spine' AND relation.relkind = 'r'"
                )
            )
        )
        if row.relname != "alembic_version"
    }
    policies = {
        (row.table_name, row.policy_name): (
            tuple(row.roles),
            row.using_expression,
            row.check_expression,
        )
        for row in (
            await connection.execute(
                text(
                    "SELECT relation.relname AS table_name, policy.polname "
                    "AS policy_name, ARRAY(SELECT role.rolname FROM pg_roles AS role "
                    "WHERE role.oid = ANY(policy.polroles)) AS roles, "
                    "pg_get_expr(policy.polqual, policy.polrelid) AS using_expression, "
                    "pg_get_expr(policy.polwithcheck, policy.polrelid) "
                    "AS check_expression FROM pg_policy AS policy "
                    "JOIN pg_class AS relation ON relation.oid = policy.polrelid "
                    "JOIN pg_namespace AS namespace "
                    "ON namespace.oid = relation.relnamespace "
                    "WHERE namespace.nspname = 'spine'"
                )
            )
        )
    }
    privilege_names = ("select_ok", "insert_ok", "update_ok", "delete_ok")
    privileges = {
        row.table_name: tuple(row[name] for name in privilege_names)
        for row in (
            await connection.execute(
                text(
                    "SELECT relation.relname AS table_name, "
                    "has_table_privilege(:role, relation.oid, 'SELECT') AS select_ok, "
                    "has_table_privilege(:role, relation.oid, 'INSERT') AS insert_ok, "
                    "has_table_privilege(:role, relation.oid, 'UPDATE') AS update_ok, "
                    "has_table_privilege(:role, relation.oid, 'DELETE') AS delete_ok "
                    "FROM pg_class AS relation JOIN pg_namespace AS namespace "
                    "ON namespace.oid = relation.relnamespace "
                    "WHERE namespace.nspname = 'spine' AND relation.relkind = 'r'"
                ),
                {"role": RUNTIME_ROLE},
            )
        ).mappings()
    }
    receipt_update_columns = {
        row.column_name
        for row in (
            await connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'spine' "
                    "AND table_name = 'idempotency_receipts' "
                    "AND has_column_privilege(:role, "
                    "'spine.idempotency_receipts', column_name, 'UPDATE')"
                ),
                {"role": RUNTIME_ROLE},
            )
        ).mappings()
    }
    revision = await connection.scalar(
        text("SELECT version_num FROM spine.alembic_version")
    )

    assert revision == HEAD_REVISION
    assert tables == {
        "alembic_version",
        "authentication_alias_bindings",
        "authorization_generations",
        "canonical_human_identities",
        "canonical_human_identity_states",
        "environment_memberships",
        "environment_role_bindings",
        "environments",
        "idempotency_receipts",
        "initial_workspace_bootstrap",
        "outbox_intents",
        "workspace_memberships",
        "workspaces",
    }
    assert constraints == {
        "alembic_version_pkc",
        "ck_environments_display_name_not_empty",
        "ck_environments_kind",
        "ck_idempotency_receipts_command_digest_sha256",
        "ck_idempotency_receipts_idempotency_key_length",
        "ck_idempotency_receipts_operation_schema_version_positive",
        "ck_idempotency_receipts_result_complete",
        "ck_initial_workspace_bootstrap_singleton",
        "ck_outbox_intents_aggregate_complete",
        "ck_outbox_intents_event_schema_version_positive",
        "ck_outbox_intents_event_type_identifier",
        "ck_outbox_intents_payload_object",
        "ck_outbox_intents_producer_deduplication_id_length",
        "ck_workspaces_display_name_not_empty",
        "ck_workspaces_slug_not_empty",
        "fk_environments_workspace_id_workspaces",
        "fk_idempotency_receipts_scope_environments",
        "fk_idempotency_receipts_workspace_id_workspaces",
        "fk_initial_workspace_bootstrap_workspace_id_workspaces",
        "fk_outbox_intents_scope_environments",
        "fk_outbox_intents_workspace_id_workspaces",
        "pk_environments",
        "pk_idempotency_receipts",
        "pk_initial_workspace_bootstrap",
        "pk_outbox_intents",
        "pk_workspaces",
        "uq_environments_workspace_id_id",
        "uq_initial_workspace_bootstrap_action_id",
        "uq_initial_workspace_bootstrap_workspace_id",
        "uq_workspaces_slug",
    } | AUTHORIZATION_DIRECTORY_CONSTRAINTS
    assert indexes == {
        "alembic_version_pkc",
        "ix_environments_workspace_id",
        "pk_environments",
        "pk_idempotency_receipts",
        "pk_initial_workspace_bootstrap",
        "pk_outbox_intents",
        "pk_workspaces",
        "uq_environments_workspace_id_id",
        "uq_idempotency_receipts_environment_key",
        "uq_idempotency_receipts_workspace_key",
        "uq_initial_workspace_bootstrap_action_id",
        "uq_initial_workspace_bootstrap_workspace_id",
        "uq_outbox_intents_environment_producer",
        "uq_outbox_intents_workspace_producer",
        "uq_workspaces_slug",
    } | AUTHORIZATION_DIRECTORY_INDEXES
    assert owners == {MIGRATION_ROLE}
    expected_rls = {
        "workspaces": (True, True),
        "environments": (True, True),
        "idempotency_receipts": (True, True),
        "initial_workspace_bootstrap": (True, True),
        "outbox_intents": (True, True),
        "workspace_memberships": (True, True),
        "environment_memberships": (True, True),
        "environment_role_bindings": (True, True),
        "authorization_generations": (True, True),
        "canonical_human_identities": (False, False),
        "canonical_human_identity_states": (False, False),
        "authentication_alias_bindings": (False, False),
    }
    assert rls == expected_rls
    tenant_rls_tables = {
        table_name
        for table_name, flags in expected_rls.items()
        if flags == (True, True)
    }
    assert set(policies) == {
        (table_name, f"pol_{table_name}_{policy_kind}")
        for table_name in tenant_rls_tables
        for policy_kind in ("tenant_isolation", "migration_maintenance")
    }
    for table_name in tenant_rls_tables:
        tenant_policy = policies[(table_name, f"pol_{table_name}_tenant_isolation")]
        assert tenant_policy[0] == (RUNTIME_ROLE,)
        assert tenant_policy[1] is not None
        assert tenant_policy[2] is not None
        assert policies[(table_name, f"pol_{table_name}_migration_maintenance")] == (
            (MIGRATION_ROLE,),
            "true",
            "true",
        )
    assert privileges == {
        "alembic_version": (True, False, False, False),
        "workspaces": (True, True, True, True),
        "environments": (True, True, True, True),
        "idempotency_receipts": (True, True, False, False),
        "initial_workspace_bootstrap": (False, False, False, False),
        "outbox_intents": (False, True, False, False),
        "authentication_alias_bindings": (False, False, False, False),
        "authorization_generations": (False, False, False, False),
        "canonical_human_identities": (False, False, False, False),
        "canonical_human_identity_states": (False, False, False, False),
        "environment_memberships": (False, False, False, False),
        "environment_role_bindings": (False, False, False, False),
        "workspace_memberships": (False, False, False, False),
    }
    assert receipt_update_columns == {
        "result_type",
        "result_id",
        "result_schema_version",
    }


@pytest.mark.asyncio(loop_scope="session")
async def test_initial_tenancy_revision_upgrades_to_current_head_and_owned_schema(
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
    assert migrated_database.retained_revisions == (
        INITIAL_REVISION,
        RLS_REVISION,
        IDEMPOTENCY_REVISION,
        OUTBOX_REVISION,
        PRE_K0_REVISION,
        K0_REVISION,
    )
    assert heads == [HEAD_REVISION]
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
        "authentication_alias_bindings",
        "authorization_generations",
        "canonical_human_identities",
        "canonical_human_identity_states",
        "environment_memberships",
        "environment_role_bindings",
        "environments",
        "idempotency_receipts",
        "initial_workspace_bootstrap",
        "outbox_intents",
        "workspace_memberships",
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
        "fk_idempotency_receipts_scope_environments",
        "fk_idempotency_receipts_workspace_id_workspaces",
        "fk_initial_workspace_bootstrap_workspace_id_workspaces",
        "fk_outbox_intents_scope_environments",
        "fk_outbox_intents_workspace_id_workspaces",
        "ck_idempotency_receipts_command_digest_sha256",
        "ck_idempotency_receipts_idempotency_key_length",
        "ck_idempotency_receipts_operation_schema_version_positive",
        "ck_idempotency_receipts_result_complete",
        "pk_environments",
        "pk_idempotency_receipts",
        "pk_initial_workspace_bootstrap",
        "ck_outbox_intents_aggregate_complete",
        "ck_outbox_intents_event_schema_version_positive",
        "ck_outbox_intents_event_type_identifier",
        "ck_outbox_intents_payload_object",
        "ck_outbox_intents_producer_deduplication_id_length",
        "pk_outbox_intents",
        "pk_workspaces",
        "uq_environments_workspace_id_id",
        "uq_initial_workspace_bootstrap_action_id",
        "uq_initial_workspace_bootstrap_workspace_id",
        "uq_workspaces_slug",
    } | AUTHORIZATION_DIRECTORY_CONSTRAINTS
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
        ("idempotency_receipts", "command_digest"),
        ("idempotency_receipts", "created_at"),
        ("idempotency_receipts", "digest_algorithm_version"),
        ("idempotency_receipts", "environment_id"),
        ("idempotency_receipts", "idempotency_key"),
        ("idempotency_receipts", "operation_name"),
        ("idempotency_receipts", "operation_schema_version"),
        ("idempotency_receipts", "receipt_id"),
        ("idempotency_receipts", "result_id"),
        ("idempotency_receipts", "result_schema_version"),
        ("idempotency_receipts", "result_type"),
        ("idempotency_receipts", "workspace_id"),
        ("outbox_intents", "aggregate_id"),
        ("outbox_intents", "aggregate_schema_version"),
        ("outbox_intents", "aggregate_type"),
        ("outbox_intents", "causation_id"),
        ("outbox_intents", "correlation_id"),
        ("outbox_intents", "created_at"),
        ("outbox_intents", "environment_id"),
        ("outbox_intents", "event_id"),
        ("outbox_intents", "event_schema_version"),
        ("outbox_intents", "event_type"),
        ("outbox_intents", "payload"),
        ("outbox_intents", "producer_deduplication_id"),
        ("outbox_intents", "trace_id"),
        ("outbox_intents", "workspace_id"),
        ("workspaces", "created_at"),
        ("workspaces", "display_name"),
        ("workspaces", "id"),
        ("workspaces", "slug"),
    } | AUTHORIZATION_DIRECTORY_COLUMNS
    assert columns[("workspaces", "id")] == ("uuid", "NO", None)
    assert columns[("environments", "id")] == ("uuid", "NO", None)
    event_id_type, event_id_nullable, event_id_default = columns[
        ("outbox_intents", "event_id")
    ]
    assert event_id_type == "uuid"
    assert event_id_nullable == "NO"
    assert event_id_default is not None and "gen_random_uuid()" in event_id_default
    for table in (
        "workspaces",
        "environments",
        "idempotency_receipts",
        "initial_workspace_bootstrap",
        "outbox_intents",
    ):
        data_type, nullable, default = columns[(table, "created_at")]
        assert data_type == "timestamp with time zone"
        assert nullable == "NO"
        assert default is not None and "CURRENT_TIMESTAMP" in default.upper()
    assert indexes == {
        "alembic_version_pkc",
        "ix_environments_workspace_id",
        "pk_environments",
        "pk_idempotency_receipts",
        "pk_initial_workspace_bootstrap",
        "pk_outbox_intents",
        "pk_workspaces",
        "uq_environments_workspace_id_id",
        "uq_idempotency_receipts_environment_key",
        "uq_idempotency_receipts_workspace_key",
        "uq_initial_workspace_bootstrap_action_id",
        "uq_initial_workspace_bootstrap_workspace_id",
        "uq_outbox_intents_environment_producer",
        "uq_outbox_intents_workspace_producer",
        "uq_workspaces_slug",
    } | AUTHORIZATION_DIRECTORY_INDEXES
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
async def test_idempotency_receipt_composite_owner_rejects_mismatched_environment(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
) -> None:
    with pytest.raises(IntegrityError):
        async with _connection(migrated_database.migration.url) as connection:
            await connection.execute(
                text(
                    "INSERT INTO spine.idempotency_receipts "
                    "(receipt_id, workspace_id, environment_id, operation_name, "
                    "operation_schema_version, idempotency_key, "
                    "digest_algorithm_version, command_digest) VALUES "
                    "(:receipt_id, :workspace_id, :environment_id, "
                    "'test.operation', 1, 'mismatched-environment', "
                    "'spine.command-digest.v1', :digest)"
                ),
                {
                    "receipt_id": UUID("51000000-0000-0000-0000-000000000001"),
                    "workspace_id": WORKSPACE_A,
                    "environment_id": ENVIRONMENT_B,
                    "digest": "a" * 64,
                },
            )


@pytest.mark.asyncio(loop_scope="session")
async def test_outbox_intent_composite_owner_rejects_mismatched_environment(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
) -> None:
    with pytest.raises(IntegrityError) as error:
        async with _connection(migrated_database.migration.url) as connection:
            await connection.execute(
                text(
                    "INSERT INTO spine.outbox_intents "
                    "(event_id, workspace_id, environment_id, event_type, "
                    "event_schema_version, payload, trace_id) VALUES "
                    "(:event_id, :workspace_id, :environment_id, "
                    "'workspace.created', 1, '{}'::jsonb, :trace_id)"
                ),
                {
                    "event_id": UUID("52000000-0000-0000-0000-000000000001"),
                    "workspace_id": WORKSPACE_A,
                    "environment_id": ENVIRONMENT_B,
                    "trace_id": UUID("52000000-0000-0000-0000-000000000002"),
                },
            )
    assert (
        error.value.orig.diag.constraint_name
        == "fk_outbox_intents_scope_environments"
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_outbox_intent_is_immutable_after_insert(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
) -> None:
    workspace_id = WORKSPACE_A
    event_id = UUID("52000000-0000-0000-0000-000000000011")
    async with _connection(migrated_database.migration.url) as connection:
        await connection.execute(
            text(
                "INSERT INTO spine.outbox_intents "
                "(event_id, workspace_id, event_type, event_schema_version, "
                "payload, trace_id) VALUES "
                "(:event_id, :workspace_id, 'workspace.created', 1, "
                "'{}'::jsonb, :trace_id)"
            ),
            {
                "event_id": event_id,
                "workspace_id": workspace_id,
                "trace_id": UUID("52000000-0000-0000-0000-000000000012"),
            },
        )
        await connection.commit()

    for statement in (
        "UPDATE spine.outbox_intents SET event_schema_version = 2 "
        "WHERE event_id = :event_id",
        "DELETE FROM spine.outbox_intents WHERE event_id = :event_id",
    ):
        with pytest.raises(IntegrityError) as error:
            async with _connection(migrated_database.migration.url) as connection:
                await connection.execute(text(statement), {"event_id": event_id})
        assert error.value.orig.diag.constraint_name == "ck_outbox_intents_immutable"

    async with _connection(migrated_database.migration.url) as connection:
        await connection.execute(
            text(
                "ALTER TABLE spine.outbox_intents "
                "DISABLE TRIGGER trg_outbox_intents_immutable"
            )
        )
        await connection.execute(
            text("DELETE FROM spine.outbox_intents WHERE event_id = :event_id"),
            {"event_id": event_id},
        )
        await connection.execute(
            text(
                "ALTER TABLE spine.outbox_intents "
                "ENABLE TRIGGER trg_outbox_intents_immutable"
            )
        )
        await connection.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_outbox_runtime_grant_and_rls_allow_only_scoped_insert(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
) -> None:
    with pytest.raises(ProgrammingError):
        async with _connection(migrated_database.runtime_url) as connection:
            await _set_tenant_context(connection, workspace_id=WORKSPACE_A)
            await connection.execute(text("SELECT event_id FROM spine.outbox_intents"))

    with pytest.raises(DBAPIError):
        async with _connection(migrated_database.runtime_url) as connection:
            await _set_tenant_context(
                connection,
                workspace_id=WORKSPACE_A,
                environment_id=ENVIRONMENT_A_SECOND,
            )
            await connection.execute(
                text(
                    "INSERT INTO spine.outbox_intents "
                    "(event_id, workspace_id, environment_id, event_type, "
                    "event_schema_version, payload, trace_id) VALUES "
                    "(:event_id, :workspace_id, :environment_id, "
                    "'workspace.created', 1, '{}'::jsonb, :trace_id)"
                ),
                {
                    "event_id": UUID("52000000-0000-0000-0000-000000000020"),
                    "workspace_id": WORKSPACE_A,
                    "environment_id": ENVIRONMENT_A,
                    "trace_id": UUID("52000000-0000-0000-0000-000000000021"),
                },
            )


@pytest.mark.asyncio(loop_scope="session")
async def test_idempotency_receipt_result_can_be_completed_exactly_once(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
) -> None:
    receipt_id = UUID("51000000-0000-0000-0000-000000000010")
    first_result_id = UUID("51000000-0000-0000-0000-000000000011")
    replacement_result_id = UUID("51000000-0000-0000-0000-000000000012")
    try:
        async with _connection(migrated_database.runtime_url) as connection:
            await _set_tenant_context(connection, workspace_id=WORKSPACE_A)
            await connection.execute(
                text(
                    "INSERT INTO spine.idempotency_receipts "
                    "(receipt_id, workspace_id, operation_name, "
                    "operation_schema_version, idempotency_key, "
                    "digest_algorithm_version, command_digest) VALUES "
                    "(:receipt_id, :workspace_id, 'test.operation', 1, "
                    "'write-once-result', 'spine.command-digest.v1', :digest)"
                ),
                {
                    "receipt_id": receipt_id,
                    "workspace_id": WORKSPACE_A,
                    "digest": "b" * 64,
                },
            )
            await connection.commit()

        with pytest.raises(IntegrityError) as error:
            async with _connection(migrated_database.migration.url) as connection:
                await connection.execute(
                    text(
                        "UPDATE spine.idempotency_receipts SET workspace_id = :workspace_id "
                        "WHERE receipt_id = :receipt_id"
                    ),
                    {"receipt_id": receipt_id, "workspace_id": WORKSPACE_B},
                )
        assert (
            error.value.orig.diag.constraint_name
            == "ck_idempotency_receipt_scope_immutable"
        )

        with pytest.raises(ProgrammingError):
            async with _connection(migrated_database.runtime_url) as connection:
                await _set_tenant_context(connection, workspace_id=WORKSPACE_A)
                await connection.execute(
                    text(
                        "UPDATE spine.idempotency_receipts SET environment_id = :environment_id "
                        "WHERE receipt_id = :receipt_id"
                    ),
                    {
                        "receipt_id": receipt_id,
                        "environment_id": ENVIRONMENT_A,
                    },
                )

        async with _connection(migrated_database.runtime_url) as connection:
            await _set_tenant_context(connection, workspace_id=WORKSPACE_A)
            await connection.execute(
                text(
                    "UPDATE spine.idempotency_receipts SET "
                    "result_type = 'proposal', result_id = :result_id, "
                    "result_schema_version = 1 WHERE receipt_id = :receipt_id"
                ),
                {"receipt_id": receipt_id, "result_id": first_result_id},
            )
            await connection.commit()

        with pytest.raises(IntegrityError) as error:
            async with _connection(migrated_database.runtime_url) as connection:
                await _set_tenant_context(connection, workspace_id=WORKSPACE_A)
                await connection.execute(
                    text(
                        "UPDATE spine.idempotency_receipts SET result_id = :result_id "
                        "WHERE receipt_id = :receipt_id"
                    ),
                    {
                        "receipt_id": receipt_id,
                        "result_id": replacement_result_id,
                    },
                )
        assert (
            error.value.orig.diag.constraint_name
            == "ck_idempotency_receipts_single_completion"
        )

        async with _connection(migrated_database.migration.url) as connection:
            persisted_result = await connection.scalar(
                text(
                    "SELECT result_id FROM spine.idempotency_receipts "
                    "WHERE receipt_id = :receipt_id"
                ),
                {"receipt_id": receipt_id},
            )
        assert persisted_result == first_result_id
    finally:
        async with _connection(migrated_database.migration.url) as connection:
            await connection.execute(
                text(
                    "DELETE FROM spine.idempotency_receipts "
                    "WHERE receipt_id = :receipt_id"
                ),
                {"receipt_id": receipt_id},
            )
            await connection.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_idempotency_receipt_rls_isolates_environment_reads_and_writes(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
) -> None:
    receipt_id = UUID("51000000-0000-0000-0000-000000000002")
    async with _connection(migrated_database.runtime_url) as connection:
        await _set_tenant_context(
            connection,
            workspace_id=WORKSPACE_A,
            environment_id=ENVIRONMENT_A,
        )
        await connection.execute(
            text(
                "INSERT INTO spine.idempotency_receipts "
                "(receipt_id, workspace_id, environment_id, operation_name, "
                "operation_schema_version, idempotency_key, "
                "digest_algorithm_version, command_digest) VALUES "
                "(:receipt_id, :workspace_id, :environment_id, "
                "'test.operation', 1, 'environment-isolation', "
                "'spine.command-digest.v1', :digest)"
            ),
            {
                "receipt_id": receipt_id,
                "workspace_id": WORKSPACE_A,
                "environment_id": ENVIRONMENT_A,
                "digest": "b" * 64,
            },
        )
        await connection.commit()

    try:
        async with _connection(migrated_database.runtime_url) as connection:
            await _set_tenant_context(
                connection,
                workspace_id=WORKSPACE_A,
                environment_id=ENVIRONMENT_A_SECOND,
            )
            visible = (
                await connection.execute(
                    text("SELECT receipt_id FROM spine.idempotency_receipts")
                )
            ).scalars().all()
            assert receipt_id not in visible
            with pytest.raises(DBAPIError):
                await connection.execute(
                    text(
                        "INSERT INTO spine.idempotency_receipts "
                        "(receipt_id, workspace_id, environment_id, operation_name, "
                        "operation_schema_version, idempotency_key, "
                        "digest_algorithm_version, command_digest) VALUES "
                        "(:receipt_id, :workspace_id, :environment_id, "
                        "'test.operation', 1, 'forbidden-environment', "
                        "'spine.command-digest.v1', :digest)"
                    ),
                    {
                        "receipt_id": UUID(
                            "51000000-0000-0000-0000-000000000003"
                        ),
                        "workspace_id": WORKSPACE_A,
                        "environment_id": ENVIRONMENT_A,
                        "digest": "c" * 64,
                    },
                )
    finally:
        async with _connection(migrated_database.migration.url) as connection:
            await connection.execute(
                text(
                    "DELETE FROM spine.idempotency_receipts "
                    "WHERE receipt_id = :receipt_id"
                ),
                {"receipt_id": receipt_id},
            )
            await connection.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_runtime_role_is_restricted_non_owner_with_rls_guarded_dml(
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

    async with _connection(migrated_database.runtime_url) as connection:
        visible = (
            await connection.execute(text("SELECT id FROM spine.workspaces"))
        ).scalars().all()
    assert visible == []
    with pytest.raises(ProgrammingError):
        async with _connection(migrated_database.runtime_url) as connection:
            await connection.execute(text("SET ROLE spine_migration"))


@pytest.mark.asyncio(loop_scope="session")
async def test_rls_catalog_declares_forced_read_and_write_checks(
    migrated_database: MigratedDatabase,
) -> None:
    async with _connection(migrated_database.migration.url) as connection:
        tables = {
            row.relname: (row.relrowsecurity, row.relforcerowsecurity)
            for row in (
                await connection.execute(
                    text(
                        "SELECT relation.relname, relation.relrowsecurity, "
                        "relation.relforcerowsecurity FROM pg_class AS relation "
                        "JOIN pg_namespace AS namespace "
                        "ON namespace.oid = relation.relnamespace "
                        "WHERE namespace.nspname = 'spine' "
                        "AND relation.relname IN "
                        "('workspaces', 'environments', 'idempotency_receipts', "
                        "'initial_workspace_bootstrap', 'outbox_intents', "
                        "'workspace_memberships', 'environment_memberships', "
                        "'environment_role_bindings', 'authorization_generations')"
                    )
                )
            )
        }
        policies = {
            (row.table_name, row.policy_name): (
                row.roles,
                row.using_expression,
                row.check_expression,
            )
            for row in (
                await connection.execute(
                    text(
                        "SELECT relation.relname AS table_name, policy.polname "
                        "AS policy_name, ARRAY(SELECT role.rolname FROM pg_roles AS role "
                        "WHERE role.oid = ANY(policy.polroles)) AS roles, "
                        "pg_get_expr(policy.polqual, policy.polrelid) "
                        "AS using_expression, pg_get_expr(policy.polwithcheck, "
                        "policy.polrelid) AS check_expression "
                        "FROM pg_policy AS policy "
                        "JOIN pg_class AS relation ON relation.oid = policy.polrelid "
                        "JOIN pg_namespace AS namespace "
                        "ON namespace.oid = relation.relnamespace "
                        "WHERE namespace.nspname = 'spine'"
                    )
                )
            )
        }

    assert tables == {
        "workspaces": (True, True),
        "environments": (True, True),
        "idempotency_receipts": (True, True),
        "initial_workspace_bootstrap": (True, True),
        "outbox_intents": (True, True),
        "workspace_memberships": (True, True),
        "environment_memberships": (True, True),
        "environment_role_bindings": (True, True),
        "authorization_generations": (True, True),
    }
    assert set(policies) == {
        (table_name, f"pol_{table_name}_{policy_kind}")
        for table_name in tables
        for policy_kind in ("tenant_isolation", "migration_maintenance")
    }
    for table_name in tables:
        roles, using_expression, check_expression = policies[
            (table_name, f"pol_{table_name}_tenant_isolation")
        ]
        assert roles == [RUNTIME_ROLE]
        assert using_expression is not None
        assert check_expression is not None
        assert "spine.workspace_id" in using_expression
        assert "spine.workspace_id" in check_expression
        maintenance = policies[
            (table_name, f"pol_{table_name}_migration_maintenance")
        ]
        assert maintenance == ([MIGRATION_ROLE], "true", "true")
    assert "spine.environment_id" in policies[
        ("environments", "pol_environments_tenant_isolation")
    ][1]
    assert "spine.environment_id" in policies[
        ("environments", "pol_environments_tenant_isolation")
    ][2]
    assert "spine.environment_id" in policies[
        ("idempotency_receipts", "pol_idempotency_receipts_tenant_isolation")
    ][1]
    assert "spine.environment_id" in policies[
        ("idempotency_receipts", "pol_idempotency_receipts_tenant_isolation")
    ][2]
    assert "spine.environment_id" in policies[
        ("outbox_intents", "pol_outbox_intents_tenant_isolation")
    ][1]
    assert "spine.environment_id" in policies[
        ("outbox_intents", "pol_outbox_intents_tenant_isolation")
    ][2]


@pytest.mark.asyncio(loop_scope="session")
async def test_runtime_grants_are_minimal_and_tables_remain_migration_owned(
    migrated_database: MigratedDatabase,
) -> None:
    async with _connection(migrated_database.migration.url) as connection:
        privilege_names = (
            "select_ok",
            "insert_ok",
            "update_ok",
            "delete_ok",
            "truncate_ok",
            "references_ok",
            "trigger_ok",
        )
        privileges = {
            row["table_name"]: tuple(row[privilege] for privilege in privilege_names)
            for row in (
                await connection.execute(
                    text(
                        "SELECT relation.relname AS table_name, "
                        "has_table_privilege(:role, relation.oid, 'SELECT') AS select_ok, "
                        "has_table_privilege(:role, relation.oid, 'INSERT') AS insert_ok, "
                        "has_table_privilege(:role, relation.oid, 'UPDATE') AS update_ok, "
                        "has_table_privilege(:role, relation.oid, 'DELETE') AS delete_ok, "
                        "has_table_privilege(:role, relation.oid, 'TRUNCATE') AS truncate_ok, "
                        "has_table_privilege(:role, relation.oid, 'REFERENCES') "
                        "AS references_ok, has_table_privilege(:role, relation.oid, "
                        "'TRIGGER') AS trigger_ok FROM pg_class AS relation "
                        "JOIN pg_namespace AS namespace "
                        "ON namespace.oid = relation.relnamespace "
                        "WHERE namespace.nspname = 'spine' "
                        "AND relation.relname IN "
                        "('alembic_version', 'workspaces', 'environments', "
                        "'idempotency_receipts', 'initial_workspace_bootstrap', "
                        "'outbox_intents')"
                    ),
                    {"role": RUNTIME_ROLE},
                )
            ).mappings()
        }
        owners = {
            row.relname: row.owner
            for row in (
                await connection.execute(
                    text(
                        "SELECT relation.relname, pg_get_userbyid(relation.relowner) "
                        "AS owner FROM pg_class AS relation "
                        "JOIN pg_namespace AS namespace "
                        "ON namespace.oid = relation.relnamespace "
                        "WHERE namespace.nspname = 'spine' "
                        "AND relation.relname IN "
                        "('alembic_version', 'workspaces', 'environments', "
                        "'idempotency_receipts', 'initial_workspace_bootstrap', "
                        "'outbox_intents')"
                    )
                )
            )
        }
        schema_privileges = (
            await connection.execute(
                text(
                    "SELECT has_schema_privilege(:role, 'spine', 'USAGE'), "
                    "has_schema_privilege(:role, 'spine', 'CREATE')"
                ),
                {"role": RUNTIME_ROLE},
            )
        ).one()
        receipt_update_columns = {
            row.column_name: row.update_ok
            for row in (
                await connection.execute(
                    text(
                        "SELECT column_name, has_column_privilege("
                        ":role, 'spine.idempotency_receipts', column_name, 'UPDATE'"
                        ") AS update_ok FROM information_schema.columns "
                        "WHERE table_schema = 'spine' "
                        "AND table_name = 'idempotency_receipts'"
                    ),
                    {"role": RUNTIME_ROLE},
                )
            ).mappings()
        }

    assert privileges["workspaces"] == (True, True, True, True, False, False, False)
    assert privileges["alembic_version"] == (
        True,
        False,
        False,
        False,
        False,
        False,
        False,
    )
    assert privileges["environments"] == (True, True, True, True, False, False, False)
    assert privileges["idempotency_receipts"] == (
        True,
        True,
        False,
        False,
        False,
        False,
        False,
    )
    assert privileges["initial_workspace_bootstrap"] == (
        False,
        False,
        False,
        False,
        False,
        False,
        False,
    )
    assert privileges["outbox_intents"] == (
        False,
        True,
        False,
        False,
        False,
        False,
        False,
    )
    assert owners == {
        "alembic_version": MIGRATION_ROLE,
        "workspaces": MIGRATION_ROLE,
        "environments": MIGRATION_ROLE,
        "idempotency_receipts": MIGRATION_ROLE,
        "initial_workspace_bootstrap": MIGRATION_ROLE,
        "outbox_intents": MIGRATION_ROLE,
    }
    assert {
        column for column, allowed in receipt_update_columns.items() if allowed
    } == {"result_type", "result_id", "result_schema_version"}
    assert tuple(schema_privileges) == (True, False)


@pytest.mark.asyncio(loop_scope="session")
async def test_k0_receipt_integrity_objects_are_catalogued_and_restricted(
    migrated_database: MigratedDatabase,
) -> None:
    async with _connection(migrated_database.migration.url) as connection:
        functions = {
            row.proname: (
                row.owner,
                row.language,
                row.volatility,
                row.security_definer,
                tuple(row.config or ()),
                row.public_execute,
                row.runtime_execute,
                row.definition,
            )
            for row in (
                await connection.execute(
                    text(
                        "SELECT procedure.proname, pg_get_userbyid(procedure.proowner) "
                        "AS owner, language.lanname AS language, "
                        "procedure.provolatile AS volatility, procedure.prosecdef "
                        "AS security_definer, procedure.proconfig AS config, "
                        "pg_get_functiondef(procedure.oid) AS definition, "
                        "has_function_privilege('public', procedure.oid, 'EXECUTE') "
                        "AS public_execute, has_function_privilege(:runtime, "
                        "procedure.oid, 'EXECUTE') AS runtime_execute "
                        "FROM pg_proc AS procedure JOIN pg_namespace AS namespace "
                        "ON namespace.oid = procedure.pronamespace "
                        "JOIN pg_language AS language ON language.oid = procedure.prolang "
                        "WHERE namespace.nspname = 'spine' AND procedure.proname IN "
                        "('validate_issue7_receipt_scope', "
                        "'reject_idempotency_receipt_scope_update', "
                        "'validate_idempotency_receipt_tenant', "
                        "'enforce_idempotency_receipt_scope_immutable')"
                    ),
                    {"runtime": RUNTIME_ROLE},
                )
            ).mappings()
        }
        trigger = (
            await connection.execute(
                text(
                    "SELECT trigger.tgname, pg_get_userbyid(relation.relowner) AS owner, "
                    "trigger.tgenabled, trigger.tgdeferrable, trigger.tginitdeferred, "
                    "trigger.tgtype, pg_get_triggerdef(trigger.oid) AS definition "
                    "FROM pg_trigger AS trigger JOIN pg_class AS relation "
                    "ON relation.oid = trigger.tgrelid "
                    "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
                    "WHERE namespace.nspname = 'spine' AND relation.relname = "
                    "'idempotency_receipts' AND NOT trigger.tgisinternal"
                )
            )
        ).mappings().all()

    assert set(functions) == {
        "validate_issue7_receipt_scope",
        "reject_idempotency_receipt_scope_update",
    }
    for owner, language, volatility, security_definer, config, public_execute, runtime_execute, definition in functions.values():
        assert owner == MIGRATION_ROLE
        assert language == "plpgsql"
        assert volatility == "v"
        assert security_definer is False
        assert config == ("search_path=pg_catalog, spine",)
        assert public_execute is False
        assert runtime_execute is False
        if "validate_issue7_receipt_scope" in definition:
            assert "FOR KEY SHARE" in definition

    assert len(trigger) == 2
    scope_trigger = next(
        row for row in trigger if row.tgname == "trg_idempotency_receipts_scope_immutable"
    )
    assert scope_trigger.owner == MIGRATION_ROLE
    assert scope_trigger.tgenabled == "O"
    assert scope_trigger.tgdeferrable is False
    assert scope_trigger.tginitdeferred is False
    assert "BEFORE UPDATE OF workspace_id, environment_id" in scope_trigger.definition
    assert "reject_idempotency_receipt_scope_update" in scope_trigger.definition
    assert scope_trigger.tgtype == 19


@pytest.mark.asyncio(loop_scope="session")
async def test_k0_migration_downgrades_and_reupgrades_cleanly(
    migrated_database: MigratedDatabase,
) -> None:
    config_path, script_location = locate_migration_assets()
    config = Config(str(config_path))
    config.set_main_option("script_location", str(script_location))
    config.attributes["migration_settings"] = migrated_database.migration
    await asyncio.to_thread(command.downgrade, config, PRE_K0_REVISION)
    async with _connection(migrated_database.migration.url) as connection:
        assert await connection.scalar(
            text("SELECT to_regprocedure('spine.validate_issue7_receipt_scope()')")
        ) is None
        assert await connection.scalar(
            text("SELECT version_num FROM spine.alembic_version")
        ) == PRE_K0_REVISION

    await asyncio.to_thread(upgrade_database, migrated_database.migration)
    async with _connection(migrated_database.migration.url) as connection:
        assert await connection.scalar(
            text("SELECT to_regprocedure('spine.validate_issue7_receipt_scope()')")
        ) is not None
        assert await connection.scalar(
            text("SELECT version_num FROM spine.alembic_version")
        ) == HEAD_REVISION


@pytest_asyncio.fixture
async def k0_child_fixture(
    migrated_database: MigratedDatabase,
) -> AsyncIterator[None]:
    async with _connection(migrated_database.migration.url) as connection:
        await connection.execute(
            text(
                "CREATE TABLE spine.k0_receipt_reference_fixture ("
                "fixture_id uuid PRIMARY KEY, workspace_id uuid NOT NULL, "
                "environment_id uuid, idempotency_receipt_id uuid)"
            )
        )
        await connection.execute(
            text(
                "GRANT INSERT, SELECT ON spine.k0_receipt_reference_fixture "
                f"TO {RUNTIME_ROLE}"
            )
        )
        await connection.execute(
            text(
                "CREATE CONSTRAINT TRIGGER trg_k0_fixture_receipt_tenant "
                "AFTER INSERT OR UPDATE ON spine.k0_receipt_reference_fixture "
                "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
                "EXECUTE FUNCTION spine.validate_issue7_receipt_scope()"
            )
        )
        trigger = (
            await connection.execute(
                text(
                    "SELECT trigger.tgtype, trigger.tgdeferrable, "
                    "trigger.tginitdeferred, pg_get_triggerdef(trigger.oid) AS definition "
                    "FROM pg_trigger AS trigger JOIN pg_class AS relation "
                    "ON relation.oid = trigger.tgrelid "
                    "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
                    "WHERE namespace.nspname = 'spine' AND relation.relname = "
                    "'k0_receipt_reference_fixture' AND trigger.tgname = "
                    "'trg_k0_fixture_receipt_tenant'"
                )
            )
        ).one()
        assert trigger.tgtype == 21
        assert trigger.tgdeferrable is True
        assert trigger.tginitdeferred is True
        assert "AFTER INSERT OR UPDATE" in trigger.definition
        assert "FOR EACH ROW" in trigger.definition
        assert "validate_issue7_receipt_scope" in trigger.definition
        await connection.commit()
    try:
        yield
    finally:
        async with _connection(migrated_database.migration.url) as connection:
            await connection.execute(
                text("DROP TABLE IF EXISTS spine.k0_receipt_reference_fixture")
            )
            await connection.execute(
                text(
                    "DELETE FROM spine.idempotency_receipts "
                    "WHERE operation_name = 'k0.fixture'"
                )
            )
            await connection.commit()


async def _insert_environment_receipt(
    database: MigratedDatabase,
    *,
    receipt_id: UUID,
    workspace_id: UUID,
    environment_id: UUID | None,
) -> None:
    async with _connection(database.migration.url) as connection:
        await connection.execute(
            text(
                "INSERT INTO spine.idempotency_receipts "
                "(receipt_id, workspace_id, environment_id, operation_name, "
                "operation_schema_version, idempotency_key, digest_algorithm_version, "
                "command_digest) VALUES (:receipt_id, :workspace_id, :environment_id, "
                "'k0.fixture', 1, :idempotency_key, 'spine.command-digest.v1', "
                ":command_digest)"
            ),
            {
                "receipt_id": receipt_id,
                "workspace_id": workspace_id,
                "environment_id": environment_id,
                "idempotency_key": str(receipt_id),
                "command_digest": "a" * 64,
            },
        )
        await connection.commit()


async def _insert_k0_child(
    database: MigratedDatabase,
    *,
    fixture_id: UUID,
    workspace_id: UUID,
    environment_id: UUID | None,
    receipt_id: UUID | None,
    commit: bool = True,
) -> None:
    async with _connection(database.runtime_url) as connection:
        await _set_tenant_context(
            connection,
            workspace_id=WORKSPACE_A,
            environment_id=ENVIRONMENT_A,
        )
        await connection.execute(
            text(
                "INSERT INTO spine.k0_receipt_reference_fixture "
                "(fixture_id, workspace_id, environment_id, idempotency_receipt_id) "
                "VALUES (:fixture_id, :workspace_id, :environment_id, :receipt_id)"
            ),
            {
                "fixture_id": fixture_id,
                "workspace_id": workspace_id,
                "environment_id": environment_id,
                "receipt_id": receipt_id,
            },
        )
        if commit:
            await connection.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_k0_deferred_validator_accepts_same_tenant_receipt(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
    k0_child_fixture: None,
) -> None:
    receipt_id = UUID("51000000-0000-0000-0000-000000000101")
    await _insert_environment_receipt(
        migrated_database,
        receipt_id=receipt_id,
        workspace_id=WORKSPACE_A,
        environment_id=ENVIRONMENT_A,
    )
    await _insert_k0_child(
        migrated_database,
        fixture_id=UUID("52000000-0000-0000-0000-000000000101"),
        workspace_id=WORKSPACE_A,
        environment_id=ENVIRONMENT_A,
        receipt_id=receipt_id,
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_k0_deferred_validator_rollback_removes_child_and_retry_succeeds(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
    k0_child_fixture: None,
) -> None:
    receipt_id = UUID("51000000-0000-0000-0000-000000000108")
    await _insert_environment_receipt(
        migrated_database,
        receipt_id=receipt_id,
        workspace_id=WORKSPACE_A,
        environment_id=ENVIRONMENT_A,
    )
    async with _connection(migrated_database.runtime_url) as connection:
        await _set_tenant_context(
            connection,
            workspace_id=WORKSPACE_A,
            environment_id=ENVIRONMENT_A,
        )
        await connection.execute(
            text(
                "INSERT INTO spine.k0_receipt_reference_fixture "
                "(fixture_id, workspace_id, environment_id, idempotency_receipt_id) "
                "VALUES (:fixture_id, :workspace_id, :environment_id, :receipt_id)"
            ),
            {
                "fixture_id": UUID("52000000-0000-0000-0000-000000000108"),
                "workspace_id": WORKSPACE_B,
                "environment_id": ENVIRONMENT_A,
                "receipt_id": receipt_id,
            },
        )
        with pytest.raises(DBAPIError):
            await connection.commit()
        await connection.rollback()
        await _set_tenant_context(
            connection,
            workspace_id=WORKSPACE_A,
            environment_id=ENVIRONMENT_A,
        )
        await connection.execute(
            text(
                "INSERT INTO spine.k0_receipt_reference_fixture "
                "(fixture_id, workspace_id, environment_id, idempotency_receipt_id) "
                "VALUES (:fixture_id, :workspace_id, :environment_id, :receipt_id)"
            ),
            {
                "fixture_id": UUID("52000000-0000-0000-0000-000000000109"),
                "workspace_id": WORKSPACE_A,
                "environment_id": ENVIRONMENT_A,
                "receipt_id": receipt_id,
            },
        )
        await connection.commit()

    async with _connection(migrated_database.migration.url) as connection:
        count = await connection.scalar(
            text(
                "SELECT count(*) FROM spine.k0_receipt_reference_fixture "
                "WHERE idempotency_receipt_id = :receipt_id"
            ),
            {"receipt_id": receipt_id},
        )
    assert count == 1


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("workspace_id", "environment_id", "receipt_id"),
    (
        (WORKSPACE_B, ENVIRONMENT_A, "foreign_workspace"),
        (WORKSPACE_A, ENVIRONMENT_A_SECOND, "foreign_environment"),
        (WORKSPACE_A, None, "null_environment"),
        (WORKSPACE_A, ENVIRONMENT_A, "missing"),
        (WORKSPACE_A, ENVIRONMENT_A, "workspace_scoped"),
        (WORKSPACE_A, ENVIRONMENT_A, "hidden"),
    ),
)
async def test_k0_deferred_validator_rejects_foreign_or_unavailable_receipt(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
    k0_child_fixture: None,
    workspace_id: UUID,
    environment_id: UUID | None,
    receipt_id: str,
) -> None:
    receipt_uuid = UUID("51000000-0000-0000-0000-000000000102")
    receipt_ids = {
        "foreign_workspace": UUID("51000000-0000-0000-0000-000000000102"),
        "foreign_environment": UUID("51000000-0000-0000-0000-000000000103"),
        "null_environment": UUID("51000000-0000-0000-0000-000000000104"),
        "hidden": UUID("51000000-0000-0000-0000-000000000105"),
        "workspace_scoped": UUID("51000000-0000-0000-0000-000000000106"),
    }
    if receipt_id in receipt_ids:
        receipt_uuid = receipt_ids[receipt_id]
        await _insert_environment_receipt(
            migrated_database,
            receipt_id=receipt_uuid,
            workspace_id=WORKSPACE_A,
            environment_id=(None if receipt_id == "workspace_scoped" else ENVIRONMENT_A),
        )
    if receipt_id == "hidden":
        async with _connection(migrated_database.runtime_url) as connection:
            await _set_tenant_context(
                connection,
                workspace_id=WORKSPACE_B,
                environment_id=ENVIRONMENT_B,
            )
            with pytest.raises(DBAPIError) as error:
                await connection.execute(
                    text(
                        "INSERT INTO spine.k0_receipt_reference_fixture "
                        "(fixture_id, workspace_id, environment_id, idempotency_receipt_id) "
                        "VALUES (:fixture_id, :workspace_id, :environment_id, :receipt_id)"
                    ),
                    {
                        "fixture_id": UUID("52000000-0000-0000-0000-000000000102"),
                        "workspace_id": WORKSPACE_A,
                        "environment_id": ENVIRONMENT_A,
                        "receipt_id": receipt_uuid,
                    },
                )
                await connection.commit()
            await connection.rollback()
        assert str(receipt_uuid) not in str(error.value)
        return

    selected_receipt = receipt_ids.get(receipt_id)
    with pytest.raises(DBAPIError) as error:
        await _insert_k0_child(
            migrated_database,
            fixture_id=UUID("52000000-0000-0000-0000-000000000102"),
            workspace_id=workspace_id,
            environment_id=environment_id,
            receipt_id=selected_receipt,
        )
    assert str(receipt_uuid) not in str(error.value)


@pytest.mark.asyncio(loop_scope="session")
async def test_k0_deferred_validator_fails_closed_for_malformed_tenant_settings(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
    k0_child_fixture: None,
) -> None:
    receipt_id = UUID("51000000-0000-0000-0000-000000000107")
    await _insert_environment_receipt(
        migrated_database,
        receipt_id=receipt_id,
        workspace_id=WORKSPACE_A,
        environment_id=ENVIRONMENT_A,
    )
    with pytest.raises(DBAPIError) as error:
        async with _connection(migrated_database.runtime_url) as connection:
            await connection.execute(
                text("SELECT set_config('spine.workspace_id', 'not-a-workspace', true)")
            )
            await connection.execute(
                text("SELECT set_config('spine.environment_id', :value, true)"),
                {"value": str(ENVIRONMENT_A)},
            )
            await connection.execute(
                text(
                    "INSERT INTO spine.k0_receipt_reference_fixture "
                    "(fixture_id, workspace_id, environment_id, idempotency_receipt_id) "
                    "VALUES (:fixture_id, :workspace_id, :environment_id, :receipt_id)"
                ),
                {
                    "fixture_id": UUID("52000000-0000-0000-0000-000000000107"),
                    "workspace_id": WORKSPACE_A,
                    "environment_id": ENVIRONMENT_A,
                    "receipt_id": receipt_id,
                },
            )
            await connection.commit()
    assert str(receipt_id) not in str(error.value)
    assert str(WORKSPACE_A) not in str(error.value)
    assert str(ENVIRONMENT_A) not in str(error.value)


@pytest.mark.asyncio(loop_scope="session")
async def test_workspace_rls_hides_and_rejects_another_workspace_rows(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
) -> None:
    async with _connection(migrated_database.runtime_url) as connection:
        await _set_tenant_context(connection, workspace_id=WORKSPACE_A)
        visible = (
            await connection.execute(text("SELECT id FROM spine.workspaces ORDER BY id"))
        ).scalars().all()
        updated = (
            await connection.execute(
                text(
                    "UPDATE spine.workspaces SET display_name = 'Hidden update' "
                    "WHERE id = :workspace_id RETURNING id"
                ),
                {"workspace_id": WORKSPACE_B},
            )
        ).scalars().all()
        own_update = (
            await connection.execute(
                text(
                    "UPDATE spine.workspaces SET display_name = 'Visible update' "
                    "WHERE id = :workspace_id RETURNING id"
                ),
                {"workspace_id": WORKSPACE_A},
            )
        ).scalars().all()
        deleted = (
            await connection.execute(
                text(
                    "DELETE FROM spine.workspaces WHERE id = :workspace_id "
                    "RETURNING id"
                ),
                {"workspace_id": WORKSPACE_B},
            )
        ).scalars().all()

    assert visible == [WORKSPACE_A]
    assert updated == []
    assert deleted == []
    assert own_update == [WORKSPACE_A]

    rejected_id = UUID("20000000-0000-0000-0000-000000000099")
    with pytest.raises(DBAPIError) as error:
        async with _connection(migrated_database.runtime_url) as connection:
            await _set_tenant_context(connection, workspace_id=WORKSPACE_A)
            await connection.execute(
                text(
                    "INSERT INTO spine.workspaces (id, slug, display_name) "
                    "VALUES (:id, 'rejected-workspace', 'Rejected') RETURNING id"
                ),
                {"id": rejected_id},
            )
    assert str(rejected_id) not in str(error.value)


@pytest.mark.asyncio(loop_scope="session")
async def test_environment_rls_isolates_two_environments_in_one_workspace(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
) -> None:
    async with _connection(migrated_database.runtime_url) as connection:
        await _set_tenant_context(
            connection,
            workspace_id=WORKSPACE_A,
            environment_id=ENVIRONMENT_A,
        )
        visible = (
            await connection.execute(text("SELECT id FROM spine.environments"))
        ).scalars().all()
        updated = (
            await connection.execute(
                text(
                    "UPDATE spine.environments SET display_name = 'Hidden update' "
                    "WHERE id = :environment_id RETURNING id"
                ),
                {"environment_id": ENVIRONMENT_A_SECOND},
            )
        ).scalars().all()
        own_delete = (
            await connection.execute(
                text(
                    "DELETE FROM spine.environments WHERE id = :environment_id "
                    "RETURNING id"
                ),
                {"environment_id": ENVIRONMENT_A},
            )
        ).scalars().all()

    assert visible == [ENVIRONMENT_A]
    assert updated == []
    assert own_delete == [ENVIRONMENT_A]

    rejected_environment = UUID("30000000-0000-0000-0000-000000000099")
    rejected_scopes = (
        (ENVIRONMENT_A, WORKSPACE_A),
        (rejected_environment, WORKSPACE_B),
    )
    for environment_setting, row_workspace in rejected_scopes:
        with pytest.raises(DBAPIError) as error:
            async with _connection(migrated_database.runtime_url) as connection:
                await _set_tenant_context(
                    connection,
                    workspace_id=WORKSPACE_A,
                    environment_id=environment_setting,
                )
                await connection.execute(
                    text(
                        "INSERT INTO spine.environments "
                        "(id, workspace_id, kind, display_name) VALUES "
                        "(:id, :workspace_id, 'development', 'Rejected') "
                        "RETURNING id"
                    ),
                    {"id": rejected_environment, "workspace_id": row_workspace},
                )
        assert str(rejected_environment) not in str(error.value)


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("workspace_setting", "environment_setting", "table_name"),
    [
        (None, None, "workspaces"),
        (WORKSPACE_A, None, "environments"),
        ("not-a-workspace", None, "workspaces"),
        (WORKSPACE_A, "not-an-environment", "environments"),
    ],
)
async def test_missing_or_malformed_settings_fail_closed_without_identifiers(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
    workspace_setting: UUID | str | None,
    environment_setting: UUID | str | None,
    table_name: str,
) -> None:
    async with _connection(migrated_database.runtime_url) as connection:
        await _set_tenant_context(
            connection,
            workspace_id=workspace_setting,
            environment_id=environment_setting,
        )
        visible = (
            await connection.execute(text(f"SELECT id FROM spine.{table_name}"))
        ).scalars().all()
    assert visible == []

    protected_id = (
        "40000000-0000-0000-0000-000000000001"
        if table_name == "workspaces"
        else "40000000-0000-0000-0000-000000000002"
    )
    statement = (
        "INSERT INTO spine.workspaces (id, slug, display_name) "
        "VALUES (:id, 'missing-context', 'Missing Context')"
        if table_name == "workspaces"
        else "INSERT INTO spine.environments "
        "(id, workspace_id, kind, display_name) VALUES "
        "(:id, :workspace_id, 'development', 'Missing Context')"
    )
    with pytest.raises(DBAPIError) as error:
        async with _connection(migrated_database.runtime_url) as connection:
            await _set_tenant_context(
                connection,
                workspace_id=workspace_setting,
                environment_id=environment_setting,
            )
            await connection.execute(
                text(statement),
                {"id": protected_id, "workspace_id": WORKSPACE_A},
            )
    assert protected_id not in str(error.value)


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE spine.runtime_ddl_attempt (id integer)",
        "ALTER TABLE spine.workspaces DISABLE ROW LEVEL SECURITY",
        "DROP POLICY pol_workspaces_tenant_isolation ON spine.workspaces",
        "ALTER TABLE spine.idempotency_receipts DISABLE TRIGGER "
        "trg_idempotency_receipts_scope_immutable",
        "ALTER FUNCTION spine.validate_issue7_receipt_scope() "
        "RENAME TO k0_validator_replaced",
        "DROP FUNCTION spine.validate_issue7_receipt_scope()",
        "ALTER ROLE spine_runtime BYPASSRLS",
        "SET ROLE spine_migration",
    ],
)
async def test_runtime_cannot_escalate_database_privileges(
    migrated_database: MigratedDatabase,
    statement: str,
) -> None:
    with pytest.raises(ProgrammingError):
        async with _connection(migrated_database.runtime_url) as connection:
            await connection.execute(text(statement))


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
async def test_real_preflight_rejects_runtime_write_access_to_revision_table(
    migrated_database: MigratedDatabase,
) -> None:
    async with _connection(migrated_database.migration.url) as connection:
        revision_before = await connection.scalar(
            text("SELECT version_num FROM spine.alembic_version")
        )
        await connection.execute(
            text("GRANT UPDATE ON spine.alembic_version TO spine_runtime")
        )
        await connection.commit()
    try:
        with pytest.raises(MigrationPreflightError):
            await asyncio.to_thread(upgrade_database, migrated_database.migration)
    finally:
        async with _connection(migrated_database.migration.url) as connection:
            await connection.execute(
                text("REVOKE UPDATE ON spine.alembic_version FROM spine_runtime")
            )
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
    shutil.copy(
        source / "versions" / "20261009_02_tenant_rls.py",
        target / "versions" / "20261009_02_tenant_rls.py",
    )
    shutil.copy(
        source / "versions" / "20261010_03_idempotency_receipts.py",
        target / "versions" / "20261010_03_idempotency_receipts.py",
    )
    shutil.copy(
        source / "versions" / "20261010_04_outbox_intents.py",
        target / "versions" / "20261010_04_outbox_intents.py",
    )
    shutil.copy(
        source / "versions" / "20261010_05_runtime_readiness.py",
        target / "versions" / "20261010_05_runtime_readiness.py",
    )
    shutil.copy(
        source / "versions" / "20261010_06_receipt_tenant_integrity.py",
        target / "versions" / "20261010_06_receipt_tenant_integrity.py",
    )
    shutil.copy(
        source / "versions" / "20261010_07_authorization_directory.py",
        target / "versions" / "20261010_07_authorization_directory.py",
    )
    (target / "versions" / "20261010_08_injected_failure.py").write_text(
        "from alembic import op\n"
        "import sqlalchemy as sa\n"
        "revision = '20261010_08'\n"
        "down_revision = '20261010_07'\n"
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
    assert revision == HEAD_REVISION
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
                {"revision": HEAD_REVISION},
            )
            await connection.commit()
    async with _connection(migrated_database.migration.url) as connection:
        workspaces_after = await connection.scalar(
            text("SELECT count(*) FROM spine.workspaces")
        )
    assert workspaces_after == workspaces_before


@pytest.mark.asyncio(loop_scope="session")
async def test_every_retained_revision_upgrades_to_one_final_head(
    migrated_database: MigratedDatabase,
) -> None:
    starting_revisions: tuple[str | None, ...] = (
        None,
        INITIAL_REVISION,
        RLS_REVISION,
        IDEMPOTENCY_REVISION,
        OUTBOX_REVISION,
        PRE_K0_REVISION,
        K0_REVISION,
    )
    try:
        for starting_revision in starting_revisions:
            async with _connection(migrated_database.migration.url) as connection:
                await connection.execute(text("DROP SCHEMA spine CASCADE"))
                await connection.commit()

            if starting_revision is not None:
                await asyncio.to_thread(
                    upgrade_database,
                    migrated_database.migration,
                    revision=starting_revision,
                )
                async with _connection(migrated_database.migration.url) as connection:
                    assert await connection.scalar(
                        text("SELECT version_num FROM spine.alembic_version")
                    ) == starting_revision

            await asyncio.to_thread(upgrade_database, migrated_database.migration)
            async with _connection(migrated_database.migration.url) as connection:
                await _assert_final_catalog_after_upgrade(connection)
    finally:
        async with _connection(migrated_database.migration.url) as connection:
            await connection.execute(text("DROP SCHEMA IF EXISTS spine CASCADE"))
            await connection.commit()
        await asyncio.to_thread(upgrade_database, migrated_database.migration)
