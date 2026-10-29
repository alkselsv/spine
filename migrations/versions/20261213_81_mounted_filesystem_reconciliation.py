"""Persist mounted-filesystem reconciliation checkpoints and evidence."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings

revision = "20261213_81"
down_revision = "20261212_80"
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
    op.create_table(
        "mounted_fs_removal_policies",
        sa.Column("policy_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_scope_reference", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "authorize_complete_scope_absence", sa.Boolean(), nullable=False
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "policy_version_id", name="pk_mounted_fs_removal_policies"
        ),
        sa.UniqueConstraint(
            "approval_id", name="uq_mounted_fs_removal_policies_approval"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_mounted_fs_removal_policies_environment",
        ),
        schema=SCHEMA,
    )
    policy_table = f"{SCHEMA}.mounted_fs_removal_policies"
    op.execute(f"ALTER TABLE {policy_table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {policy_table} FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY pol_mounted_fs_removal_policies_tenant_isolation "
        f"ON {policy_table} FOR ALL TO {runtime_role} "
        "USING (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid) "
        "WITH CHECK (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid)"
    )
    op.execute(
        "CREATE POLICY pol_mounted_fs_removal_policies_migration_maintenance "
        f"ON {policy_table} FOR ALL TO {migration_role} USING (true) WITH CHECK (true)"
    )
    op.execute(f"REVOKE ALL ON {policy_table} FROM {runtime_role}")
    op.execute(f"GRANT SELECT ON {policy_table} TO {runtime_role}")
    op.create_table(
        "mounted_fs_removal_authorities",
        sa.Column("authority_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("policy_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("authority_generation", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "authority_event_id", name="pk_mounted_fs_removal_authorities"
        ),
        sa.ForeignKeyConstraint(
            ["policy_version_id"],
            [f"{SCHEMA}.mounted_fs_removal_policies.policy_version_id"],
            name="fk_mounted_fs_removal_authorities_policy",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_mounted_fs_removal_authorities_environment",
        ),
        sa.UniqueConstraint(
            "policy_version_id",
            "authority_generation",
            name="uq_mounted_fs_removal_authorities_generation",
        ),
        sa.CheckConstraint(
            "state IN ('active', 'revoked')",
            name=op.f("ck_mounted_fs_removal_authorities_state"),
        ),
        sa.CheckConstraint(
            "authority_generation > 0",
            name=op.f("ck_mounted_fs_removal_authorities_generation"),
        ),
        schema=SCHEMA,
    )
    authority_table = f"{SCHEMA}.mounted_fs_removal_authorities"
    op.execute(f"ALTER TABLE {authority_table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {authority_table} FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY pol_mounted_fs_removal_authorities_tenant_isolation "
        f"ON {authority_table} FOR ALL TO {runtime_role} "
        "USING (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid) "
        "WITH CHECK (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid)"
    )
    op.execute(
        "CREATE POLICY pol_mounted_fs_removal_authorities_migration_maintenance "
        f"ON {authority_table} FOR ALL TO {migration_role} USING (true) WITH CHECK (true)"
    )
    op.execute(f"REVOKE ALL ON {authority_table} FROM {runtime_role}")
    op.execute(f"GRANT SELECT ON {authority_table} TO {runtime_role}")


def downgrade() -> None:
    op.drop_table("mounted_fs_removal_authorities", schema=SCHEMA)
    op.drop_table("mounted_fs_removal_policies", schema=SCHEMA)
    op.drop_table("mounted_fs_reconciliation_runs", schema=SCHEMA)
