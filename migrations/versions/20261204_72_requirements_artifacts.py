"""Persist immutable, scoped requirements artifact versions."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings

revision = "20261204_72"
down_revision = "20261203_71"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "requirements_artifact_versions",
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("artifact_id", uuid, nullable=False),
        sa.Column("artifact_version", sa.Integer(), nullable=False),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("work_item_id", uuid, nullable=False),
        sa.Column("production_identity", sa.Text(), nullable=False),
        sa.Column("production_digest", sa.String(64), nullable=False),
        sa.Column("predecessor_version", sa.Integer(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "environment_id", "artifact_id", "artifact_version",
                                name="pk_requirements_artifact_versions"),
        sa.UniqueConstraint("workspace_id", "environment_id", "run_id", "production_identity",
                            name="uq_requirements_artifact_production"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "run_id"],
            [f"{SCHEMA}.workflow_runs.workspace_id", f"{SCHEMA}.workflow_runs.environment_id",
             f"{SCHEMA}.workflow_runs.run_id"],
            name="fk_requirements_artifact_run",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "work_item_id"],
            [f"{SCHEMA}.work_items.workspace_id", f"{SCHEMA}.work_items.environment_id",
             f"{SCHEMA}.work_items.id"],
            name="fk_requirements_artifact_work_item",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "artifact_id", "predecessor_version"],
            [f"{SCHEMA}.requirements_artifact_versions.workspace_id",
             f"{SCHEMA}.requirements_artifact_versions.environment_id",
             f"{SCHEMA}.requirements_artifact_versions.artifact_id",
             f"{SCHEMA}.requirements_artifact_versions.artifact_version"],
            name="fk_requirements_artifact_predecessor",
        ),
        sa.CheckConstraint("artifact_version >= 1", name=op.f("ck_requirements_artifact_version_positive")),
        sa.CheckConstraint("predecessor_version IS NULL OR predecessor_version = artifact_version - 1",
                           name=op.f("ck_requirements_artifact_predecessor")),
        schema=SCHEMA,
    )
    op.create_index("ix_requirements_artifact_versions_run", "requirements_artifact_versions",
                    ["workspace_id", "environment_id", "run_id", "artifact_version"], schema=SCHEMA)
    _tenant_scope("requirements_artifact_versions")


def _tenant_scope(table: str) -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    qualified = f'{SCHEMA}."{table}"'
    ws_setting = "pg_catalog.current_setting('spine.workspace_id', true)"
    env_setting = "pg_catalog.current_setting('spine.environment_id', true)"
    pattern = "^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
    ws = f"CASE WHEN {ws_setting} OPERATOR(pg_catalog.~) '{pattern}' THEN CAST({ws_setting} AS pg_catalog.uuid) ELSE NULL END"
    env = f"CASE WHEN {env_setting} OPERATOR(pg_catalog.~) '{pattern}' THEN CAST({env_setting} AS pg_catalog.uuid) ELSE NULL END"
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(f'CREATE POLICY "pol_{table}_tenant_isolation" ON {qualified} FOR ALL TO {runtime} '
               f"USING (workspace_id = ({ws}) AND environment_id = ({env})) "
               f"WITH CHECK (workspace_id = ({ws}) AND environment_id = ({env}))")
    op.execute(f'CREATE POLICY "pol_{table}_migration_maintenance" ON {qualified} FOR ALL TO {migration} '
               "USING (true) WITH CHECK (true)")
    op.execute(f"GRANT SELECT, INSERT ON TABLE {qualified} TO {runtime}")
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {qualified} FROM {runtime}")


def downgrade() -> None:
    qualified = f'{SCHEMA}."requirements_artifact_versions"'
    op.execute(f'DROP POLICY IF EXISTS "pol_requirements_artifact_versions_tenant_isolation" ON {qualified}')
    op.execute(f'DROP POLICY IF EXISTS "pol_requirements_artifact_versions_migration_maintenance" ON {qualified}')
    op.drop_index("ix_requirements_artifact_versions_run", table_name="requirements_artifact_versions", schema=SCHEMA)
    op.drop_table("requirements_artifact_versions", schema=SCHEMA)
