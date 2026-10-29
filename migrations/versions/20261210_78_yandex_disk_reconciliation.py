"""Persist conservative Yandex Disk reconciliation checkpoints and evidence."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261210_78"
down_revision = "20261209_77"
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
        "yandex_disk_reconciliation_runs",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_scope_reference", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("committed_checkpoint", sa.Text()),
        sa.Column("next_checkpoint", sa.Text()),
        sa.Column("record_schema_version", sa.Integer(), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("run_id", name="pk_yandex_disk_reconciliation_runs"),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "connection_id",
            "connection_version_id",
            "resource_scope_reference",
            "idempotency_key",
            name="uq_yandex_disk_reconciliation_runs_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_yandex_disk_reconciliation_runs_environment",
        ),
        sa.CheckConstraint(
            "state IN ('running', 'reconciling', 'interrupted', 'complete')",
            name=op.f("ck_yandex_disk_reconciliation_runs_state"),
        ),
        sa.CheckConstraint(
            "committed_checkpoint IS NULL OR length(committed_checkpoint) BETWEEN 1 AND 4096",
            name=op.f("ck_yandex_disk_reconciliation_runs_committed_checkpoint"),
        ),
        sa.CheckConstraint(
            "next_checkpoint IS NULL OR length(next_checkpoint) BETWEEN 1 AND 4096",
            name=op.f("ck_yandex_disk_reconciliation_runs_next_checkpoint"),
        ),
        sa.CheckConstraint(
            "record_schema_version = 1",
            name=op.f("ck_yandex_disk_reconciliation_runs_record_schema_version"),
        ),
        schema=SCHEMA,
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.yandex_disk_reconciliation_runs ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.yandex_disk_reconciliation_runs FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        f"CREATE POLICY pol_yandex_disk_reconciliation_runs_tenant_isolation "
        f"ON {SCHEMA}.yandex_disk_reconciliation_runs FOR ALL TO {runtime_role} "
        "USING (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid) "
        "WITH CHECK (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid)"
    )
    op.execute(
        f"CREATE POLICY pol_yandex_disk_reconciliation_runs_migration_maintenance "
        f"ON {SCHEMA}.yandex_disk_reconciliation_runs FOR ALL TO {migration_role} "
        "USING (true) WITH CHECK (true)"
    )
    op.execute(
        f"REVOKE ALL ON {SCHEMA}.yandex_disk_reconciliation_runs FROM {runtime_role}"
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON {SCHEMA}.yandex_disk_reconciliation_runs "
        f"TO {runtime_role}"
    )
    op.execute(
        f"COMMENT ON TABLE {SCHEMA}.yandex_disk_reconciliation_runs IS "
        "'classification=tenant_operational_state; provider_content=forbidden; "
        "paths_are_stored_only_inside_tenant_scoped_replay_envelope'"
    )


def downgrade() -> None:
    op.drop_table("yandex_disk_reconciliation_runs", schema=SCHEMA)
