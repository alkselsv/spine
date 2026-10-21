"""Permit tenant-scoped operational correlation reads of outbox intent identity."""

from __future__ import annotations

from alembic import op

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261118_55"
down_revision = "20261117_54"
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
        CREATE FUNCTION {SCHEMA}.resolve_run_outbox_delivery_correlation(
            requested_workspace_id uuid,
            requested_environment_id uuid,
            requested_run_id uuid
        ) RETURNS TABLE (
            event_id uuid,
            workspace_id uuid,
            environment_id uuid,
            status text,
            attempt_count integer,
            correlation_id uuid,
            trace_id uuid,
            created_at timestamptz,
            updated_at timestamptz,
            quarantined_at timestamptz,
            last_error_code text,
            version integer
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, {SCHEMA}
        AS $$
            SELECT delivery.event_id,
                   delivery.workspace_id,
                   delivery.environment_id,
                   delivery.status::text,
                   delivery.attempt_count,
                   delivery.correlation_id,
                   intent.trace_id,
                   delivery.created_at,
                   delivery.updated_at,
                   delivery.quarantined_at,
                   delivery.last_error_code,
                   delivery.version
            FROM {SCHEMA}.outbox_deliveries AS delivery
            JOIN {SCHEMA}.outbox_intents AS intent
              ON intent.event_id = delivery.event_id
             AND intent.workspace_id = delivery.workspace_id
            WHERE intent.workspace_id = requested_workspace_id
              AND intent.environment_id = requested_environment_id
              AND intent.aggregate_type = 'answer_question_run'
              AND intent.aggregate_id = requested_run_id
              AND delivery.status IN ('delivered', 'quarantined')
              AND current_setting('spine.workspace_id', true) = requested_workspace_id::text
              AND current_setting('spine.environment_id', true) = requested_environment_id::text
            ORDER BY delivery.updated_at
        $$
        """
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.resolve_run_outbox_delivery_correlation"
        "(uuid,uuid,uuid) FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {SCHEMA}.resolve_run_outbox_delivery_correlation"
        f"(uuid,uuid,uuid) TO {_runtime_role()}"
    )


def downgrade() -> None:
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION {SCHEMA}.resolve_run_outbox_delivery_correlation"
        f"(uuid,uuid,uuid) FROM {_runtime_role()}"
    )
    op.execute(
        f"DROP FUNCTION {SCHEMA}.resolve_run_outbox_delivery_correlation(uuid,uuid,uuid)"
    )
