"""Add process-only retry/reprocess ledger contracts for Issue #344."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261031_37"
down_revision = "20261030_36"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, attribute))


def _scope_policy(table: str, role: str) -> None:
    qualified = f'{SCHEMA}."{table}"'
    expression = (
        "workspace_id = (CASE WHEN pg_catalog.current_setting('spine.workspace_id', true) "
        "OPERATOR(pg_catalog.~) '^[0-9A-Fa-f-]{36}$' THEN "
        "CAST(pg_catalog.current_setting('spine.workspace_id', true) AS pg_catalog.uuid) ELSE NULL END) "
        "AND environment_id = (CASE WHEN pg_catalog.current_setting('spine.environment_id', true) "
        "OPERATOR(pg_catalog.~) '^[0-9A-Fa-f-]{36}$' THEN "
        "CAST(pg_catalog.current_setting('spine.environment_id', true) AS pg_catalog.uuid) ELSE NULL END)"
    )
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(
        f'CREATE POLICY "pol_{table}_tenant_isolation" ON {qualified} FOR ALL TO {role} '
        f"USING ({expression}) WITH CHECK ({expression})"
    )
    op.execute(
        f'CREATE POLICY "pol_{table}_migration_maintenance" ON {qualified} FOR ALL TO '
        f'{_role("migration_role")} USING (true) WITH CHECK (true)'
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE {qualified} TO {role}")


def _immutable_trigger(table: str) -> None:
    function = f"reject_{table}_mutation"
    qualified = f'{SCHEMA}."{table}"'
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.{function}() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        BEGIN
            RAISE EXCEPTION 'Process-only lineage rows are immutable.'
                USING ERRCODE = '23514', CONSTRAINT = 'ck_{table}_immutable';
        END; $function$
        """
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {qualified} "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.{function}()"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.{function}() FROM PUBLIC")


def upgrade() -> None:
    runtime = _role("runtime_role")
    op.drop_constraint(
        "ck_ingestion_runs_trigger_kind",
        "ingestion_runs",
        schema=SCHEMA,
        type_="check",
    )
    op.create_check_constraint(
        "ck_ingestion_runs_trigger_kind",
        "ingestion_runs",
        "trigger_kind IN ('upload', 'connector', 'reprocess', 'retry', 'replacement')",
        schema=SCHEMA,
    )
    op.drop_constraint(
        "ck_ingestion_transitions_target_state",
        "ingestion_transitions",
        schema=SCHEMA,
        type_="check",
    )
    op.create_check_constraint(
        "ck_ingestion_transitions_target_state",
        "ingestion_transitions",
        "((target_kind = 'run' AND ingestion_run_source_id IS NULL AND state IN ('requested', 'receiving', 'processing', 'partially_succeeded', 'succeeded', 'failed', 'cancelled')) OR (target_kind = 'item' AND ingestion_run_source_id IS NOT NULL AND state IN ('received', 'stored', 'observed', 'admission_pending', 'processing_ready', 'quarantined', 'rejected', 'accepted', 'processing', 'processed', 'processing_failed', 'projection_requested')))",
        schema=SCHEMA,
    )
    uuid_type = postgresql.UUID(as_uuid=True)
    op.create_table(
        "process_only_operation_lineages",
        sa.Column("operation_id", uuid_type, nullable=False),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("environment_id", uuid_type, nullable=False),
        sa.Column("ingestion_run_id", uuid_type, nullable=False),
        sa.Column("ingestion_run_source_id", uuid_type, nullable=False),
        sa.Column("trigger_kind", sa.Text(), nullable=False),
        sa.Column("source_object_id", uuid_type, nullable=False),
        sa.Column("source_revision_id", uuid_type, nullable=False),
        sa.Column("original_receipt_id", uuid_type, nullable=False),
        sa.Column("original_receipt_digest", sa.String(64), nullable=False),
        sa.Column("parent_failure_id", uuid_type),
        sa.Column("parent_processing_receipt_id", uuid_type),
        sa.Column("pinned_processing_profile_reference", sa.Text(), nullable=False),
        sa.Column("command_digest", sa.String(64), nullable=False),
        sa.Column("idempotency_receipt_id", uuid_type, nullable=False),
        sa.PrimaryKeyConstraint("operation_id", name="pk_process_only_operation_lineages"),
        sa.UniqueConstraint(
            "workspace_id", "environment_id", "operation_id",
            name="uq_process_only_lineages_scope_id",
        ),
        sa.UniqueConstraint(
            "workspace_id", "environment_id", "ingestion_run_id",
            name="uq_process_only_lineages_run",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "ingestion_run_id"],
            [f"{SCHEMA}.ingestion_runs.workspace_id", f"{SCHEMA}.ingestion_runs.environment_id", f"{SCHEMA}.ingestion_runs.ingestion_run_id"],
            name="fk_process_only_lineages_run",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "ingestion_run_source_id"],
            [f"{SCHEMA}.ingestion_run_sources.workspace_id", f"{SCHEMA}.ingestion_run_sources.environment_id", f"{SCHEMA}.ingestion_run_sources.ingestion_run_source_id"],
            name="fk_process_only_lineages_item",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "source_revision_id"],
            [f"{SCHEMA}.source_revisions.workspace_id", f"{SCHEMA}.source_revisions.environment_id", f"{SCHEMA}.source_revisions.source_object_id", f"{SCHEMA}.source_revisions.revision_id"],
            name="fk_process_only_lineages_revision",
        ),
        sa.ForeignKeyConstraint(
            ["idempotency_receipt_id"],
            [f"{SCHEMA}.idempotency_receipts.receipt_id"],
            name="fk_process_only_lineages_receipt",
        ),
        sa.CheckConstraint(
            "trigger_kind IN ('retry', 'reprocess', 'replacement')",
            name="ck_process_only_lineages_trigger_kind",
        ),
        sa.CheckConstraint(
            "original_receipt_digest OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$' AND command_digest OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$'",
            name="ck_process_only_lineages_digests",
        ),
        schema=SCHEMA,
    )
    _scope_policy("process_only_operation_lineages", runtime)
    _immutable_trigger("process_only_operation_lineages")


def downgrade() -> None:
    runtime = _role("runtime_role")
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.reject_process_only_operation_lineages_mutation() FROM {runtime}"
    )
    op.execute(
        f"DROP TRIGGER trg_process_only_operation_lineages_immutable ON {SCHEMA}.process_only_operation_lineages"
    )
    op.execute(
        f"DROP FUNCTION {SCHEMA}.reject_process_only_operation_lineages_mutation()"
    )
    op.drop_table("process_only_operation_lineages", schema=SCHEMA)
    op.drop_constraint(
        "ck_ingestion_transitions_target_state",
        "ingestion_transitions",
        schema=SCHEMA,
        type_="check",
    )
    op.create_check_constraint(
        "ck_ingestion_transitions_target_state",
        "ingestion_transitions",
        "((target_kind = 'run' AND ingestion_run_source_id IS NULL AND state IN ('requested', 'receiving', 'processing', 'partially_succeeded', 'succeeded', 'failed', 'cancelled')) OR (target_kind = 'item' AND ingestion_run_source_id IS NOT NULL AND state IN ('received', 'stored', 'observed', 'admission_pending', 'quarantined', 'rejected', 'accepted', 'processing', 'processed', 'processing_failed', 'projection_requested')))",
        schema=SCHEMA,
    )
    op.drop_constraint(
        "ck_ingestion_runs_trigger_kind",
        "ingestion_runs",
        schema=SCHEMA,
        type_="check",
    )
    op.create_check_constraint(
        "ck_ingestion_runs_trigger_kind",
        "ingestion_runs",
        "trigger_kind IN ('upload', 'connector', 'reprocess', 'retry')",
        schema=SCHEMA,
    )
