"""Persist governed Google Drive initial snapshot checkpoints."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261125_62"
down_revision = "20261124_61"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, attribute))


def upgrade() -> None:
    runtime_role = _role("runtime_role")
    migration_role = _role("migration_role")
    op.create_table(
        "drive_snapshot_runs",
        sa.Column("sync_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_scope_reference", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("service_principal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("proposed_start_page_token", sa.Text(), nullable=False),
        sa.Column("committed_cursor", sa.Text()),
        sa.Column("next_page_token", sa.Text()),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("sync_run_id", name="pk_drive_snapshot_runs"),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "idempotency_key",
            name="uq_drive_snapshot_runs_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_drive_snapshot_runs_environment",
        ),
        sa.CheckConstraint(
            "state IN ('running', 'succeeded', 'resumable', 'reconciliation_required')",
            name="ck_drive_snapshot_runs_state",
        ),
        sa.CheckConstraint(
            "length(proposed_start_page_token) BETWEEN 1 AND 4096",
            name="ck_drive_snapshot_runs_start_token",
        ),
        schema=SCHEMA,
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.drive_snapshot_runs ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.drive_snapshot_runs FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        f"CREATE POLICY pol_drive_snapshot_runs_tenant_isolation ON {SCHEMA}.drive_snapshot_runs "
        f"FOR ALL TO {runtime_role} "
        "USING (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid) "
        "WITH CHECK (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid)"
    )
    op.execute(
        f"CREATE POLICY pol_drive_snapshot_runs_migration_maintenance ON {SCHEMA}.drive_snapshot_runs "
        f"FOR ALL TO {migration_role} USING (true) WITH CHECK (true)"
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON {SCHEMA}.drive_snapshot_runs TO {runtime_role}"
    )
    op.execute(
        f"COMMENT ON TABLE {SCHEMA}.drive_snapshot_runs IS "
        "'classification=tenant_operational_state; provider_content=forbidden; "
        "page_items_are_safe_external_ids_only'"
    )


def downgrade() -> None:
    op.drop_table("drive_snapshot_runs", schema=SCHEMA)
