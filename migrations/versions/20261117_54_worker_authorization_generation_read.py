"""Permit tenant-scoped workers to read current authorization generation."""

from __future__ import annotations

from alembic import op

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261117_54"
down_revision = "20261116_53"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _runtime_role() -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(settings.runtime_role)


def upgrade() -> None:
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.resolve_current_authorization_generation(
            requested_workspace_id uuid,
            requested_environment_id uuid
        ) RETURNS bigint
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, {SCHEMA}
        AS $$
            SELECT generation
            FROM {SCHEMA}.authorization_generations
            WHERE workspace_id = requested_workspace_id
              AND environment_id = requested_environment_id
              AND current_setting('spine.workspace_id', true) = requested_workspace_id::text
              AND current_setting('spine.environment_id', true) = requested_environment_id::text
        $$
        """
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.resolve_current_authorization_generation"
        "(uuid,uuid) FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {SCHEMA}.resolve_current_authorization_generation"
        f"(uuid,uuid) TO {_runtime_role()}"
    )


def downgrade() -> None:
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION {SCHEMA}.resolve_current_authorization_generation"
        f"(uuid,uuid) FROM {_runtime_role()}"
    )
    op.execute(
        f"DROP FUNCTION {SCHEMA}.resolve_current_authorization_generation(uuid,uuid)"
    )
