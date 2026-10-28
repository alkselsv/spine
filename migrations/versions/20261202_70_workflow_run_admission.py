"""Store first-message WorkItem and pinned WorkflowRun atomically."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings

revision = "20261202_70"
down_revision = "20261201_69"
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
    op.execute(
        f"REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {qualified} FROM {runtime}"
    )


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "work_items",
        sa.Column("id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("workflow_version_id", uuid, nullable=False),
        sa.Column("work_item_key", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("triggered_by", postgresql.JSONB(), nullable=False),
        sa.Column("input", postgresql.JSONB(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "environment_id", "id", name="pk_work_items"),
        sa.UniqueConstraint(
            "workspace_id", "environment_id", "work_item_key",
            name="uq_work_items_scoped_key",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_work_items_environment",
        ),
        sa.CheckConstraint("char_length(work_item_key) BETWEEN 1 AND 255", name="ck_work_items_key_length"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'waiting', 'needs_review', 'blocked', 'failed', 'completed', 'cancelled')",
            name="ck_work_items_status",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "workflow_runs",
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("command_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("work_item_id", uuid, nullable=False),
        sa.Column("workflow_key", sa.Text(), nullable=False),
        sa.Column("workflow_version_id", uuid, nullable=False),
        sa.Column("workflow_version", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("run_version", sa.Integer(), nullable=False),
        sa.Column("started_by", postgresql.JSONB(), nullable=False),
        sa.Column("trace_id", uuid, nullable=False),
        sa.Column("correlation_id", uuid, nullable=False),
        sa.Column("causation_id", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "environment_id", "run_id", name="pk_workflow_runs"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "work_item_id"],
            [f"{SCHEMA}.work_items.workspace_id", f"{SCHEMA}.work_items.environment_id", f"{SCHEMA}.work_items.id"],
            name="fk_workflow_runs_work_item",
        ),
        sa.CheckConstraint(
            "state IN ('admitted', 'running', 'waiting', 'completed', 'failed', 'cancelled')",
            name="ck_workflow_runs_state",
        ),
        sa.CheckConstraint("run_version >= 1", name="ck_workflow_runs_version_positive"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_workflow_runs_environment",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_workflow_runs_work_item",
        "workflow_runs",
        ["workspace_id", "environment_id", "work_item_id"],
        schema=SCHEMA,
    )
    _tenant_scope("work_items")
    _tenant_scope("workflow_runs")


def downgrade() -> None:
    op.drop_index("ix_workflow_runs_work_item", table_name="workflow_runs", schema=SCHEMA)
    for table in ("workflow_runs", "work_items"):
        qualified = f'{SCHEMA}."{table}"'
        op.execute(f'DROP POLICY IF EXISTS "pol_{table}_tenant_isolation" ON {qualified}')
        op.execute(f'DROP POLICY IF EXISTS "pol_{table}_migration_maintenance" ON {qualified}')
        op.drop_table(table, schema=SCHEMA)
