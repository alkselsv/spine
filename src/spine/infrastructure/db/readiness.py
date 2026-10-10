"""Fail-closed runtime database readiness and schema compatibility."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import cast

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from spine.infrastructure.db.migrations import locate_migration_assets
from spine.infrastructure.db.settings import (
    SUPPORTED_POSTGRESQL_MAJOR,
    RuntimeDatabaseSettings,
)

_ORDERED_REVISION = re.compile(r"^(?P<date>[0-9]{8})_(?P<sequence>[0-9]+)$")


class SchemaRevisionState(str, Enum):
    """Compatibility of database heads with this application build."""

    READY = "ready"
    EMPTY = "empty"
    OLDER = "older"
    NEWER = "newer"
    UNKNOWN = "unknown"
    MULTIPLE_HEADS = "multiple_heads"


class DatabaseReadinessError(RuntimeError):
    """The runtime database cannot safely serve this application build."""


@dataclass(frozen=True, slots=True)
class MigrationInventory:
    """The immutable revision inventory shipped with this application build."""

    revisions: tuple[str, ...]
    heads: tuple[str, ...]

    @property
    def supported_head(self) -> str:
        if len(self.heads) != 1:
            raise RuntimeError("application migration history must have one head")
        return self.heads[0]


@dataclass(frozen=True, slots=True)
class ExpectedTablePrivileges:
    """Required runtime access and tenant protection for one kernel table."""

    tenant_scoped: bool
    select: bool
    insert: bool
    update: bool
    delete: bool


def load_migration_inventory() -> MigrationInventory:
    """Load the packaged Alembic graph without opening a database connection."""

    config_path, script_location = locate_migration_assets()
    config = Config(str(config_path))
    config.set_main_option("script_location", str(script_location))
    scripts = ScriptDirectory.from_config(config)
    return MigrationInventory(
        revisions=tuple(
            reversed(tuple(revision.revision for revision in scripts.walk_revisions()))
        ),
        heads=tuple(sorted(scripts.get_heads())),
    )


def classify_schema_revision(
    *,
    database_heads: tuple[str, ...],
    supported_head: str,
    known_revisions: tuple[str, ...],
) -> SchemaRevisionState:
    """Classify a database revision without changing its migration state."""

    if not database_heads:
        return SchemaRevisionState.EMPTY
    if len(database_heads) != 1:
        return SchemaRevisionState.MULTIPLE_HEADS

    database_head = database_heads[0]
    if database_head == supported_head:
        return SchemaRevisionState.READY
    if database_head in known_revisions:
        return SchemaRevisionState.OLDER

    database_order = _revision_order(database_head)
    supported_order = _revision_order(supported_head)
    if (
        database_order is not None
        and supported_order is not None
        and database_order > supported_order
    ):
        return SchemaRevisionState.NEWER
    return SchemaRevisionState.UNKNOWN


async def verify_database_readiness(
    engine: object,
    settings: RuntimeDatabaseSettings,
) -> None:
    """Verify exact schema, restricted privileges, and local context binding."""

    async_engine = cast(AsyncEngine, engine)
    inventory = load_migration_inventory()
    try:
        async with async_engine.connect() as connection:
            major = await connection.scalar(
                text("SELECT current_setting('server_version_num')::integer / 10000")
            )
            if major != SUPPORTED_POSTGRESQL_MAJOR:
                raise DatabaseReadinessError("database is not ready")

            version_table = await connection.scalar(
                text("SELECT to_regclass('spine.alembic_version')")
            )
            if version_table is None:
                database_heads: tuple[str, ...] = ()
            else:
                database_heads = tuple(
                    (
                        await connection.execute(
                            text(
                                "SELECT version_num FROM spine.alembic_version "
                                "ORDER BY version_num"
                            )
                        )
                    ).scalars()
                )
            revision_state = classify_schema_revision(
                database_heads=database_heads,
                supported_head=inventory.supported_head,
                known_revisions=inventory.revisions,
            )
            if revision_state is not SchemaRevisionState.READY:
                raise DatabaseReadinessError("database schema is not ready")

            role = (
                await connection.execute(
                    text(
                        "SELECT current_user AS current_user, "
                        "role.rolsuper AS is_superuser, "
                        "role.rolbypassrls AS bypasses_rls, "
                        "role.rolcreaterole AS can_create_roles, "
                        "role.rolcreatedb AS can_create_databases, "
                        "role.rolinherit AS inherits_privileges, "
                        "EXISTS (SELECT 1 FROM pg_auth_members AS membership "
                        "WHERE membership.member = role.oid) AS has_memberships, "
                        "has_database_privilege(current_user, current_database(), "
                        "'CREATE') AS can_create_in_database, "
                        "has_schema_privilege(current_user, 'spine', 'USAGE') "
                        "AS can_use_schema, "
                        "has_schema_privilege(current_user, 'spine', 'CREATE') "
                        "AS can_create_in_schema, "
                        "pg_get_userbyid(namespace.nspowner) AS schema_owner "
                        "FROM pg_roles AS role "
                        "JOIN pg_namespace AS namespace ON namespace.nspname = 'spine' "
                        "WHERE role.rolname = current_user"
                    )
                )
            ).mappings().one()
            if not _runtime_role_is_safe(role, settings.runtime_role):
                raise DatabaseReadinessError("database runtime role is not ready")

            table_rows = (
                await connection.execute(
                    text(
                        "SELECT relation.relname AS table_name, "
                        "relation.relrowsecurity AS rls_enabled, "
                        "relation.relforcerowsecurity AS rls_forced, "
                        "pg_get_userbyid(relation.relowner) = current_user AS is_owner, "
                        "has_table_privilege(current_user, relation.oid, 'SELECT') "
                        "AS can_select, "
                        "has_table_privilege(current_user, relation.oid, 'INSERT') "
                        "AS can_insert, "
                        "has_table_privilege(current_user, relation.oid, 'UPDATE') "
                        "AS can_update, "
                        "has_table_privilege(current_user, relation.oid, 'DELETE') "
                        "AS can_delete, "
                        "has_table_privilege(current_user, relation.oid, "
                        "'TRUNCATE,REFERENCES,TRIGGER') AS has_unsafe_privileges "
                        "FROM pg_class AS relation "
                        "JOIN pg_namespace AS namespace "
                        "ON namespace.oid = relation.relnamespace "
                        "WHERE namespace.nspname = 'spine' "
                        "AND relation.relkind IN ('r', 'p')"
                    )
                )
            ).mappings().all()
            tables = {row.table_name: row for row in table_rows}
            if not _runtime_table_privileges_are_safe(tables):
                raise DatabaseReadinessError("database runtime privileges are not ready")

            resolver_privileges = (
                await connection.execute(
                    text(
                        "SELECT has_function_privilege(current_user, "
                        "'spine.resolve_current_authorization_snapshot"
                        "(text,text,uuid,uuid)', 'EXECUTE') AS runtime_execute, "
                        "has_function_privilege('public', "
                        "'spine.resolve_current_authorization_snapshot"
                        "(text,text,uuid,uuid)', 'EXECUTE') AS public_execute"
                    )
                )
            ).mappings().one()
            if (
                not resolver_privileges.runtime_execute
                or resolver_privileges.public_execute
            ):
                raise DatabaseReadinessError("database runtime privileges are not ready")

            receipt_columns = (
                await connection.execute(
                    text(
                        "SELECT "
                        "has_column_privilege(current_user, "
                        "'spine.idempotency_receipts', 'result_type', 'UPDATE') "
                        "AS result_type, "
                        "has_column_privilege(current_user, "
                        "'spine.idempotency_receipts', 'result_id', 'UPDATE') "
                        "AS result_id, "
                        "has_column_privilege(current_user, "
                        "'spine.idempotency_receipts', 'result_schema_version', "
                        "'UPDATE') AS result_schema_version, "
                        "has_column_privilege(current_user, "
                        "'spine.idempotency_receipts', 'workspace_id', 'UPDATE') "
                        "AS workspace_id"
                    )
                )
            ).mappings().one()
            if not (
                receipt_columns.result_type
                and receipt_columns.result_id
                and receipt_columns.result_schema_version
                and not receipt_columns.workspace_id
            ):
                raise DatabaseReadinessError("database runtime privileges are not ready")

            workspace_id = "00000000-0000-0000-0000-000000000048"
            environment_id = "00000000-0000-0000-0000-000000000049"
            bound = (
                await connection.execute(
                    text(
                        "SELECT "
                        "set_config('spine.workspace_id', :workspace_id, true) "
                        "AS workspace_id, "
                        "set_config('spine.environment_id', :environment_id, true) "
                        "AS environment_id"
                    ),
                    {
                        "workspace_id": workspace_id,
                        "environment_id": environment_id,
                    },
                )
            ).one()
            if tuple(bound) != (workspace_id, environment_id):
                raise DatabaseReadinessError("database tenant context is not ready")
    except DatabaseReadinessError:
        raise
    except Exception:
        raise DatabaseReadinessError("database is not ready") from None


def _runtime_role_is_safe(role: object, expected_role: str) -> bool:
    return bool(
        role.current_user == expected_role
        and not role.is_superuser
        and not role.bypasses_rls
        and not role.can_create_roles
        and not role.can_create_databases
        and not role.inherits_privileges
        and not role.has_memberships
        and not role.can_create_in_database
        and role.can_use_schema
        and not role.can_create_in_schema
        and role.schema_owner != expected_role
    )


def _runtime_table_privileges_are_safe(tables: dict[str, object]) -> bool:
    expected = {
        "alembic_version": ExpectedTablePrivileges(
            tenant_scoped=False,
            select=True,
            insert=False,
            update=False,
            delete=False,
        ),
        "workspaces": ExpectedTablePrivileges(
            tenant_scoped=True,
            select=True,
            insert=True,
            update=True,
            delete=True,
        ),
        "environments": ExpectedTablePrivileges(
            tenant_scoped=True,
            select=True,
            insert=True,
            update=True,
            delete=True,
        ),
        "initial_workspace_bootstrap": ExpectedTablePrivileges(
            tenant_scoped=True,
            select=False,
            insert=False,
            update=False,
            delete=False,
        ),
        "idempotency_receipts": ExpectedTablePrivileges(
            tenant_scoped=True,
            select=True,
            insert=True,
            update=False,
            delete=False,
        ),
        "outbox_intents": ExpectedTablePrivileges(
            tenant_scoped=True,
            select=False,
            insert=True,
            update=False,
            delete=False,
        ),
        "canonical_human_identities": ExpectedTablePrivileges(
            tenant_scoped=False,
            select=False,
            insert=False,
            update=False,
            delete=False,
        ),
        "canonical_human_identity_states": ExpectedTablePrivileges(
            tenant_scoped=False,
            select=False,
            insert=False,
            update=False,
            delete=False,
        ),
        "authentication_alias_bindings": ExpectedTablePrivileges(
            tenant_scoped=False,
            select=False,
            insert=False,
            update=False,
            delete=False,
        ),
        "workspace_memberships": ExpectedTablePrivileges(
            tenant_scoped=True,
            select=False,
            insert=False,
            update=False,
            delete=False,
        ),
        "environment_memberships": ExpectedTablePrivileges(
            tenant_scoped=True,
            select=False,
            insert=False,
            update=False,
            delete=False,
        ),
        "environment_role_bindings": ExpectedTablePrivileges(
            tenant_scoped=True,
            select=False,
            insert=False,
            update=False,
            delete=False,
        ),
        "authorization_generations": ExpectedTablePrivileges(
            tenant_scoped=True,
            select=False,
            insert=False,
            update=False,
            delete=False,
        ),
    }
    if set(tables) != set(expected):
        return False
    for table_name, required in expected.items():
        row = tables[table_name]
        if row.is_owner or row.has_unsafe_privileges:
            return False
        if required.tenant_scoped and (not row.rls_enabled or not row.rls_forced):
            return False
        if not required.tenant_scoped and (row.rls_enabled or row.rls_forced):
            return False
        if (
            row.can_select != required.select
            or row.can_insert != required.insert
            or row.can_update != required.update
            or row.can_delete != required.delete
        ):
            return False
    return True


def _revision_order(revision: str) -> tuple[int, int] | None:
    match = _ORDERED_REVISION.fullmatch(revision)
    if match is None:
        return None
    return int(match.group("date")), int(match.group("sequence"))


__all__ = [
    "DatabaseReadinessError",
    "MigrationInventory",
    "SchemaRevisionState",
    "classify_schema_revision",
    "load_migration_inventory",
    "verify_database_readiness",
]
