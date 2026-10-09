"""Enforce tenant RLS and grant restricted runtime DML.

Revision ID: 20261009_02
Revises: 20261009_01
"""

from __future__ import annotations

from alembic import op

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261009_02"
down_revision = "20261009_01"
branch_labels = None
depends_on = None

SCHEMA = "spine"
TENANT_TABLES = (
    "workspaces",
    "environments",
    "initial_workspace_bootstrap",
)
RUNTIME_DML_TABLES = ("workspaces", "environments")
_UUID_PATTERN = (
    "^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    "[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)


def _quoted_role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    role = getattr(settings, attribute)
    return op.get_bind().dialect.identifier_preparer.quote(role)


def _setting_uuid(name: str) -> str:
    setting = f"pg_catalog.current_setting('spine.{name}', true)"
    return (
        f"CASE WHEN {setting} OPERATOR(pg_catalog.~) '{_UUID_PATTERN}' "
        f"THEN CAST({setting} AS pg_catalog.uuid) ELSE NULL END"
    )


def _tenant_policy_expression(table: str) -> str:
    workspace = _setting_uuid("workspace_id")
    if table == "workspaces":
        return f"id = ({workspace})"
    if table == "environments":
        environment = _setting_uuid("environment_id")
        return f"workspace_id = ({workspace}) AND id = ({environment})"
    return f"workspace_id = ({workspace})"


def upgrade() -> None:
    runtime_role = _quoted_role("runtime_role")
    migration_role = _quoted_role("migration_role")

    for table in TENANT_TABLES:
        qualified_table = f'{SCHEMA}."{table}"'
        expression = _tenant_policy_expression(table)
        op.execute(f"ALTER TABLE {qualified_table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {qualified_table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f'CREATE POLICY "pol_{table}_tenant_isolation" '
            f"ON {qualified_table} FOR ALL TO {runtime_role} "
            f"USING ({expression}) WITH CHECK ({expression})"
        )
        op.execute(
            f'CREATE POLICY "pol_{table}_migration_maintenance" '
            f"ON {qualified_table} FOR ALL TO {migration_role} "
            "USING (true) WITH CHECK (true)"
        )

    op.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {runtime_role}")
    for table in RUNTIME_DML_TABLES:
        op.execute(
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {SCHEMA}."{table}" '
            f"TO {runtime_role}"
        )


def downgrade() -> None:
    runtime_role = _quoted_role("runtime_role")

    for table in RUNTIME_DML_TABLES:
        op.execute(
            f'REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLE {SCHEMA}."{table}" '
            f"FROM {runtime_role}"
        )
    op.execute(f"REVOKE USAGE ON SCHEMA {SCHEMA} FROM {runtime_role}")

    for table in reversed(TENANT_TABLES):
        qualified_table = f'{SCHEMA}."{table}"'
        op.execute(
            f'DROP POLICY "pol_{table}_migration_maintenance" ON {qualified_table}'
        )
        op.execute(
            f'DROP POLICY "pol_{table}_tenant_isolation" ON {qualified_table}'
        )
        op.execute(f"ALTER TABLE {qualified_table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {qualified_table} DISABLE ROW LEVEL SECURITY")
