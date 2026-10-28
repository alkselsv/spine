"""Persist typed workflow commands and disclosure-safe run events."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings

revision = "20261203_71"
down_revision = "20261202_70"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def _tenant_scope(table: str) -> None:
    runtime, migration = _role("runtime_role"), _role("migration_role")
    qualified = f'{SCHEMA}."{table}"'
    workspace_setting = "pg_catalog.current_setting('spine.workspace_id', true)"
    environment_setting = "pg_catalog.current_setting('spine.environment_id', true)"
    pattern = "^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
    workspace = f"CASE WHEN {workspace_setting} OPERATOR(pg_catalog.~) '{pattern}' THEN CAST({workspace_setting} AS pg_catalog.uuid) ELSE NULL END"
    environment = f"CASE WHEN {environment_setting} OPERATOR(pg_catalog.~) '{pattern}' THEN CAST({environment_setting} AS pg_catalog.uuid) ELSE NULL END"
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(
        f'CREATE POLICY "pol_{table}_tenant_isolation" ON {qualified} '
        f"FOR ALL TO {runtime} USING (workspace_id = ({workspace}) AND environment_id = ({environment})) "
        f"WITH CHECK (workspace_id = ({workspace}) AND environment_id = ({environment}))"
    )
    op.execute(
        f'CREATE POLICY "pol_{table}_migration_maintenance" ON {qualified} '
        f"FOR ALL TO {migration} USING (true) WITH CHECK (true)"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE {qualified} TO {runtime}")
    denied = "UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER"
    op.execute(f"REVOKE {denied} ON TABLE {qualified} FROM {runtime}")


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "workflow_command_receipts",
        sa.Column("command_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("work_item_id", uuid, nullable=False),
        sa.Column("workflow_version_id", uuid, nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("command_type", sa.Text(), nullable=False),
        sa.Column("command_schema_version", sa.Integer(), nullable=False),
        sa.Column("expected_run_version", sa.Integer(), nullable=False),
        sa.Column("resulting_run_version", sa.Integer(), nullable=False),
        sa.Column("run_state", sa.Text(), nullable=False),
        sa.Column("actor", postgresql.JSONB(), nullable=False),
        sa.Column("command_payload", postgresql.JSONB(), nullable=False),
        sa.Column("artifact_refs", postgresql.JSONB(), nullable=False),
        sa.Column("command_digest", sa.String(length=64), nullable=False),
        sa.Column("trace_id", uuid, nullable=False),
        sa.Column("correlation_id", uuid, nullable=False),
        sa.Column("causation_id", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id", "environment_id", "command_id",
            name="pk_workflow_command_receipts",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "run_id"],
            [f"{SCHEMA}.workflow_runs.workspace_id", f"{SCHEMA}.workflow_runs.environment_id", f"{SCHEMA}.workflow_runs.run_id"],
            name="fk_workflow_command_receipts_run",
        ),
        sa.CheckConstraint(
            "command_type = 'add_context'",
            name=op.f("ck_workflow_command_receipts_type"),
        ),
        sa.CheckConstraint(
            "command_schema_version = 1",
            name=op.f("ck_workflow_command_receipts_schema"),
        ),
        sa.CheckConstraint(
            "expected_run_version >= 1",
            name=op.f("ck_workflow_command_receipts_expected_version"),
        ),
        sa.CheckConstraint(
            "resulting_run_version = expected_run_version + 1",
            name=op.f("ck_workflow_command_receipts_resulting_version"),
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "workflow_run_events",
        sa.Column("event_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("event_schema_version", sa.Integer(), nullable=False),
        sa.Column("run_version", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("correlation_id", uuid, nullable=False),
        sa.Column("causation_id", uuid, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id", "environment_id", "event_id",
            name="pk_workflow_run_events",
        ),
        sa.UniqueConstraint(
            "workspace_id", "environment_id", "run_id", "sequence",
            name="uq_workflow_run_events_sequence",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "run_id"],
            [f"{SCHEMA}.workflow_runs.workspace_id", f"{SCHEMA}.workflow_runs.environment_id", f"{SCHEMA}.workflow_runs.run_id"],
            name="fk_workflow_run_events_run",
        ),
        sa.CheckConstraint(
            "sequence >= 1", name=op.f("ck_workflow_run_events_sequence")
        ),
        sa.CheckConstraint(
            "run_version >= 1", name=op.f("ck_workflow_run_events_version")
        ),
        sa.CheckConstraint(
            "event_schema_version = 1",
            name=op.f("ck_workflow_run_events_schema"),
        ),
        sa.CheckConstraint(
            "event_type IN ('workflow.run-started', 'workflow.context-added')",
            name=op.f("ck_workflow_run_events_type"),
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_workflow_run_events_feed",
        "workflow_run_events",
        ["workspace_id", "environment_id", "run_id", "sequence"],
        schema=SCHEMA,
    )
    _tenant_scope("workflow_command_receipts")
    _tenant_scope("workflow_run_events")
    runtime = _role("runtime_role")
    op.execute(f'GRANT UPDATE ON TABLE {SCHEMA}."workflow_runs" TO {runtime}')


def downgrade() -> None:
    runtime = _role("runtime_role")
    op.execute(f'REVOKE UPDATE ON TABLE {SCHEMA}."workflow_runs" FROM {runtime}')
    op.drop_index("ix_workflow_run_events_feed", table_name="workflow_run_events", schema=SCHEMA)
    for table in ("workflow_run_events", "workflow_command_receipts"):
        qualified = f'{SCHEMA}."{table}"'
        op.execute(f'DROP POLICY IF EXISTS "pol_{table}_tenant_isolation" ON {qualified}')
        op.execute(f'DROP POLICY IF EXISTS "pol_{table}_migration_maintenance" ON {qualified}')
        op.drop_table(table, schema=SCHEMA)
