"""Persist governed Google Sheets reconciliation checkpoints."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings

revision = "20261127_64"
down_revision = "20261126_63"
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
        "google_sheets_sync_runs",
        sa.Column("sync_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "resource_scope_reference", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "service_principal_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("prior_committed_cursor", sa.Text()),
        sa.Column("proposed_cursor", sa.Text()),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("sync_run_id", name="pk_google_sheets_sync_runs"),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "idempotency_key",
            name="uq_google_sheets_sync_runs_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_google_sheets_sync_runs_environment",
        ),
        sa.CheckConstraint(
            "state IN ('running', 'succeeded', 'resumable', 'reconciliation_required')",
            name="ck_google_sheets_sync_runs_state",
        ),
        schema=SCHEMA,
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.google_sheets_sync_runs ENABLE ROW LEVEL SECURITY"
    )
    op.execute(f"ALTER TABLE {SCHEMA}.google_sheets_sync_runs FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY pol_google_sheets_sync_runs_tenant_isolation ON {SCHEMA}.google_sheets_sync_runs "
        f"FOR ALL TO {runtime_role} "
        "USING (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid) "
        "WITH CHECK (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid)"
    )
    op.execute(
        f"CREATE POLICY pol_google_sheets_sync_runs_migration_maintenance ON {SCHEMA}.google_sheets_sync_runs "
        f"FOR ALL TO {migration_role} USING (true) WITH CHECK (true)"
    )
    op.execute(f"REVOKE ALL ON {SCHEMA}.google_sheets_sync_runs FROM {runtime_role}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON {SCHEMA}.google_sheets_sync_runs TO {runtime_role}"
    )
    op.execute(
        f"COMMENT ON TABLE {SCHEMA}.google_sheets_sync_runs IS "
        "'classification=tenant_operational_state; provider_content=forbidden; "
        "row_items_are_safe_external_ids_only'"
    )


def downgrade() -> None:
    op.drop_table("google_sheets_sync_runs", schema=SCHEMA)
