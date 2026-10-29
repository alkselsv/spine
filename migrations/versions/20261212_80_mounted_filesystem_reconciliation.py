"""Persist mounted-filesystem reconciliation checkpoints and evidence."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings

revision = "20261212_80"
down_revision = "20261211_79"
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
        "mounted_fs_reconciliation_runs",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_scope_reference", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("record_schema_version", sa.Integer(), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "run_id", name="pk_mounted_fs_reconciliation_runs"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "connection_id",
            "connection_version_id",
            "resource_scope_reference",
            "idempotency_key",
            name="uq_mounted_fs_reconciliation_runs_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_mounted_fs_reconciliation_runs_environment",
        ),
        sa.CheckConstraint(
            "state IN ('running', 'unavailable', 'incomplete', 'complete')",
            name=op.f("ck_mounted_fs_reconciliation_runs_state"),
        ),
        sa.CheckConstraint(
            "record_schema_version = 1",
            name=op.f(
                "ck_mounted_fs_reconciliation_runs_record_schema_version"
            ),
        ),
        schema=SCHEMA,
    )
    table = f"{SCHEMA}.mounted_fs_reconciliation_runs"
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY pol_mounted_fs_reconciliation_runs_tenant_isolation "
        f"ON {table} FOR ALL TO {runtime_role} "
        "USING (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid) "
        "WITH CHECK (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid)"
    )
    op.execute(
        "CREATE POLICY pol_mounted_fs_reconciliation_runs_migration_maintenance "
        f"ON {table} FOR ALL TO {migration_role} USING (true) WITH CHECK (true)"
    )
    op.execute(f"REVOKE ALL ON {table} FROM {runtime_role}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON {table} TO {runtime_role}")
    op.execute(
        f"COMMENT ON TABLE {table} IS "
        "'classification=tenant_operational_state; provider_content=forbidden; "
        "paths_are_stored_only_inside_tenant_scoped_replay_envelope'"
    )


def downgrade() -> None:
    op.drop_table("mounted_fs_reconciliation_runs", schema=SCHEMA)
