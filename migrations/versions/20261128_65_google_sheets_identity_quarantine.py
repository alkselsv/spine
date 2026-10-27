"""Persist safe append-only Google Sheets identity quarantine outcomes."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings

revision = "20261128_65"
down_revision = "20261127_64"
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
        "google_sheets_identity_dispositions",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sync_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_identity", sa.Text(), nullable=False),
        sa.Column("disposition", sa.String(length=16), nullable=False),
        sa.Column("failure_code", sa.String(length=64)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name="pk_google_sheets_identity_dispositions"),
        sa.UniqueConstraint(
            "sync_run_id", "scope_identity",
            name="uq_google_sheets_identity_disposition_run_scope",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_google_sheets_identity_disposition_environment",
        ),
        sa.ForeignKeyConstraint(
            ["sync_run_id"], [f"{SCHEMA}.google_sheets_sync_runs.sync_run_id"],
            name="fk_google_sheets_identity_disposition_run",
        ),
        sa.CheckConstraint(
            "(disposition = 'quarantined' AND failure_code IS NOT NULL) OR "
            "(disposition = 'cleared' AND failure_code IS NULL)",
            name="shape",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_google_sheets_identity_disposition_scope",
        "google_sheets_identity_dispositions",
        ["workspace_id", "connection_id", "scope_identity", "occurred_at"],
        schema=SCHEMA,
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.google_sheets_identity_dispositions ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.google_sheets_identity_dispositions FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        f"CREATE POLICY pol_google_sheets_identity_dispositions_tenant_isolation ON {SCHEMA}.google_sheets_identity_dispositions "
        f"FOR ALL TO {runtime_role} USING (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid) "
        "WITH CHECK (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid)"
    )
    op.execute(
        f"CREATE POLICY pol_google_sheets_identity_dispositions_migration_maintenance ON {SCHEMA}.google_sheets_identity_dispositions "
        f"FOR ALL TO {migration_role} USING (true) WITH CHECK (true)"
    )
    op.execute(
        f"REVOKE ALL ON {SCHEMA}.google_sheets_identity_dispositions FROM {runtime_role}"
    )
    op.execute(
        f"GRANT SELECT, INSERT ON {SCHEMA}.google_sheets_identity_dispositions TO {runtime_role}"
    )
    op.execute(
        f"COMMENT ON TABLE {SCHEMA}.google_sheets_identity_dispositions IS "
        "'classification=tenant_operational_state; provider_content=forbidden; append_only=true'"
    )


def downgrade() -> None:
    op.drop_table("google_sheets_identity_dispositions", schema=SCHEMA)
