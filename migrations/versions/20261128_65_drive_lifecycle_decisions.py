"""Persist fail-closed Drive lifecycle decisions."""

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
        "drive_lifecycle_decisions",
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reconciliation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("change_sync_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("committed_cursor", sa.Text(), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_scope_reference", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("policy_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_id", sa.Text(), nullable=False),
        sa.Column("source_object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("policy_authorized", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("decision_id", name="pk_drive_lifecycle_decisions"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_drive_lifecycle_decisions_environment",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'tombstoned', 'retained')",
            name="ck_drive_lifecycle_decisions_state",
        ),
        sa.CheckConstraint("length(file_id) BETWEEN 1 AND 256", name="ck_drive_lifecycle_file_id"),
        sa.CheckConstraint(
            "length(committed_cursor) BETWEEN 1 AND 4096",
            name="ck_drive_lifecycle_committed_cursor",
        ),
        schema=SCHEMA,
    )
    op.execute(f"ALTER TABLE {SCHEMA}.drive_lifecycle_decisions ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {SCHEMA}.drive_lifecycle_decisions FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY pol_drive_lifecycle_decisions_tenant_isolation ON {SCHEMA}.drive_lifecycle_decisions "
        f"FOR ALL TO {runtime_role} "
        "USING (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid) "
        "WITH CHECK (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid)"
    )
    op.execute(
        f"CREATE POLICY pol_drive_lifecycle_decisions_migration_maintenance ON {SCHEMA}.drive_lifecycle_decisions "
        f"FOR ALL TO {migration_role} USING (true) WITH CHECK (true)"
    )
    op.execute(f"REVOKE ALL ON {SCHEMA}.drive_lifecycle_decisions FROM {runtime_role}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON {SCHEMA}.drive_lifecycle_decisions TO {runtime_role}"
    )
    op.execute(
        f"COMMENT ON TABLE {SCHEMA}.drive_lifecycle_decisions IS "
        "'classification=tenant_operational_state; provider_content=forbidden; "
        "idempotent policy decision evidence'"
    )


def downgrade() -> None:
    op.drop_table("drive_lifecycle_decisions", schema=SCHEMA)
