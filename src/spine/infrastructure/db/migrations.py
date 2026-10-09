"""Stable identifiers shared by Alembic and migration verification."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, text
from sqlalchemy.engine import Connection

from spine.infrastructure.db.settings import MigrationDatabaseSettings

SPINE_SCHEMA = "spine"
ALEMBIC_VERSION_TABLE = "alembic_version"

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
MIGRATION_METADATA = MetaData(naming_convention=NAMING_CONVENTION)


class MigrationPreflightError(RuntimeError):
    """Required migration ownership or role separation is absent."""


@dataclass(frozen=True, slots=True)
class MigrationPreflightState:
    current_user: str
    database_owner: str
    migration_role_exists: bool
    migration_role_is_superuser: bool
    migration_role_can_create_roles: bool
    migration_role_can_create_databases: bool
    migration_role_bypasses_rls: bool
    runtime_role_exists: bool
    runtime_role_is_superuser: bool
    runtime_role_bypasses_rls: bool
    runtime_role_can_create_roles: bool
    runtime_role_can_create_databases: bool
    runtime_role_inherits_privileges: bool
    runtime_role_has_memberships: bool
    runtime_role_can_assume_migration: bool
    runtime_role_can_create_in_database: bool
    runtime_role_can_create_in_spine_schema: bool
    runtime_role_has_unsafe_table_privileges: bool
    runtime_role_has_unprotected_tenant_dml: bool
    schema_owner: str | None


def validate_migration_preflight(
    state: MigrationPreflightState,
    *,
    migration_role: str,
    runtime_role: str,
) -> None:
    """Fail closed before Alembic mutates a database with unsafe ownership."""

    valid = (
        migration_role != runtime_role
        and state.migration_role_exists
        and state.runtime_role_exists
        and state.current_user == migration_role
        and state.database_owner == migration_role
        and state.schema_owner in (None, migration_role)
        and not state.migration_role_is_superuser
        and not state.migration_role_can_create_roles
        and not state.migration_role_can_create_databases
        and not state.migration_role_bypasses_rls
        and not state.runtime_role_is_superuser
        and not state.runtime_role_bypasses_rls
        and not state.runtime_role_can_create_roles
        and not state.runtime_role_can_create_databases
        and not state.runtime_role_inherits_privileges
        and not state.runtime_role_has_memberships
        and not state.runtime_role_can_assume_migration
        and not state.runtime_role_can_create_in_database
        and not state.runtime_role_can_create_in_spine_schema
        and not state.runtime_role_has_unsafe_table_privileges
        and not state.runtime_role_has_unprotected_tenant_dml
    )
    if not valid:
        raise MigrationPreflightError("database migration preflight failed")


def collect_migration_preflight(
    connection: Connection,
    *,
    migration_role: str,
    runtime_role: str,
) -> MigrationPreflightState:
    """Read the minimum PostgreSQL catalog state needed by the preflight."""

    row = connection.execute(
        text(
            "SELECT current_user AS current_user, "
            "pg_get_userbyid(database.datdba) AS database_owner, "
            "migration.oid IS NOT NULL AS migration_role_exists, "
            "COALESCE(migration.rolsuper, false) AS migration_role_is_superuser, "
            "COALESCE(migration.rolcreaterole, false) "
            "AS migration_role_can_create_roles, "
            "COALESCE(migration.rolcreatedb, false) "
            "AS migration_role_can_create_databases, "
            "COALESCE(migration.rolbypassrls, false) "
            "AS migration_role_bypasses_rls, "
            "runtime.oid IS NOT NULL AS runtime_role_exists, "
            "COALESCE(runtime.rolsuper, false) AS runtime_role_is_superuser, "
            "COALESCE(runtime.rolbypassrls, false) AS runtime_role_bypasses_rls, "
            "COALESCE(runtime.rolcreaterole, false) AS runtime_role_can_create_roles, "
            "COALESCE(runtime.rolcreatedb, false) "
            "AS runtime_role_can_create_databases, "
            "COALESCE(runtime.rolinherit, false) "
            "AS runtime_role_inherits_privileges, "
            "EXISTS (SELECT 1 FROM pg_auth_members AS membership "
            "WHERE membership.member = runtime.oid) AS runtime_role_has_memberships, "
            "EXISTS (SELECT 1 FROM pg_auth_members AS membership "
            "WHERE membership.member = runtime.oid "
            "AND membership.roleid = migration.oid) "
            "AS runtime_role_can_assume_migration, "
            "COALESCE(has_database_privilege(runtime.oid, database.oid, 'CREATE'), false) "
            "AS runtime_role_can_create_in_database, "
            "COALESCE(has_schema_privilege(runtime.oid, namespace.oid, 'CREATE'), false) "
            "AS runtime_role_can_create_in_spine_schema, "
            "EXISTS (SELECT 1 FROM pg_class AS tenant_table "
            "WHERE tenant_table.relnamespace = namespace.oid "
            "AND ((tenant_table.relkind IN ('r', 'p') "
            "AND has_table_privilege(runtime.oid, tenant_table.oid, "
            "'TRUNCATE,REFERENCES,TRIGGER')) "
            "OR (tenant_table.relkind = 'S' "
            "AND has_sequence_privilege(runtime.oid, tenant_table.oid, 'UPDATE')))) "
            "AS runtime_role_has_unsafe_table_privileges, "
            "EXISTS (SELECT 1 FROM pg_class AS tenant_table "
            "WHERE tenant_table.relnamespace = namespace.oid "
            "AND tenant_table.relkind IN ('r', 'p') "
            "AND has_table_privilege(runtime.oid, tenant_table.oid, "
            "'SELECT,INSERT,UPDATE,DELETE') "
            "AND (NOT tenant_table.relrowsecurity "
            "OR NOT tenant_table.relforcerowsecurity "
            "OR tenant_table.relowner = runtime.oid)) "
            "AS runtime_role_has_unprotected_tenant_dml, "
            "pg_get_userbyid(namespace.nspowner) AS schema_owner "
            "FROM pg_database AS database "
            "LEFT JOIN pg_roles AS migration ON migration.rolname = :migration_role "
            "LEFT JOIN pg_roles AS runtime ON runtime.rolname = :runtime_role "
            "LEFT JOIN pg_namespace AS namespace ON namespace.nspname = :schema_name "
            "WHERE database.datname = current_database()"
        ),
        {
            "migration_role": migration_role,
            "runtime_role": runtime_role,
            "schema_name": SPINE_SCHEMA,
        },
    ).mappings().one()
    return MigrationPreflightState(**row)


def upgrade_database(
    settings: MigrationDatabaseSettings,
    *,
    revision: str = "head",
) -> None:
    """Run the explicit Alembic upgrade with migration-only credentials."""

    config_path, script_location = locate_migration_assets()
    config = Config(str(config_path))
    config.set_main_option("script_location", str(script_location))
    config.attributes["migration_settings"] = settings
    command.upgrade(config, revision)


def locate_migration_assets() -> tuple[Path, Path]:
    """Resolve Alembic assets in a source checkout or an installed wheel."""

    module_directory = Path(__file__).resolve().parent
    installed = (
        module_directory / "alembic.ini",
        module_directory / "alembic_migrations",
    )
    if installed[0].is_file() and installed[1].is_dir():
        return installed

    repository_root = module_directory.parents[3]
    source = repository_root / "alembic.ini", repository_root / "migrations"
    if source[0].is_file() and source[1].is_dir():
        return source
    raise RuntimeError("Alembic migration assets are unavailable")
