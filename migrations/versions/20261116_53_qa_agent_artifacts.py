"""Persist governed Q&A input/output artifacts and agent results."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261116_53"
down_revision = "20261115_52"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def _tenant_table(name: str) -> None:
    qualified = f'{SCHEMA}."{name}"'
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    uuid_setting = lambda setting: (
        "CASE WHEN pg_catalog.current_setting('spine.%s', true) "
        "OPERATOR(pg_catalog.~) "
        "'^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$' "
        "THEN CAST(pg_catalog.current_setting('spine.%s', true) AS pg_catalog.uuid) ELSE NULL END"
        % (setting, setting)
    )
    scope = (
        f"workspace_id = ({uuid_setting('workspace_id')}) AND "
        f"environment_id = ({uuid_setting('environment_id')})"
    )
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(
        f'CREATE POLICY "pol_{name}_tenant_isolation" ON {qualified} '
        f"FOR ALL TO {runtime} USING ({scope}) WITH CHECK ({scope})"
    )
    op.execute(
        f'CREATE POLICY "pol_{name}_migration_maintenance" ON {qualified} '
        f"FOR ALL TO {migration} USING (true) WITH CHECK (true)"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE {qualified} TO {runtime}")
    op.execute(
        f"REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER "
        f"ON TABLE {qualified} FROM {runtime}"
    )


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    op.create_table(
        "qa_agent_artifacts",
        sa.Column("artifact_id", uuid_type, nullable=False),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("environment_id", uuid_type, nullable=False),
        sa.Column("work_item_id", uuid_type, nullable=False),
        sa.Column("artifact", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("artifact_id", name="pk_qa_agent_artifacts"),
        sa.UniqueConstraint(
            "workspace_id", "environment_id", "artifact_id",
            name="uq_qa_agent_artifacts_scope_id",
        ),
        sa.CheckConstraint("jsonb_typeof(artifact) = 'object'", name="ck_qa_agent_artifact_json"),
        schema=SCHEMA,
    )
    op.create_table(
        "qa_agent_results",
        sa.Column("result_id", uuid_type, nullable=False),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("environment_id", uuid_type, nullable=False),
        sa.Column("work_item_id", uuid_type, nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("result_id", name="pk_qa_agent_results"),
        sa.UniqueConstraint(
            "workspace_id", "environment_id", "result_id",
            name="uq_qa_agent_results_scope_id",
        ),
        sa.CheckConstraint("jsonb_typeof(result) = 'object'", name="ck_qa_agent_result_json"),
        schema=SCHEMA,
    )
    _tenant_table("qa_agent_artifacts")
    _tenant_table("qa_agent_results")


def downgrade() -> None:
    runtime = _role("runtime_role")
    for name in ("qa_agent_results", "qa_agent_artifacts"):
        op.execute(
            f"REVOKE ALL PRIVILEGES ON TABLE {SCHEMA}.{name} FROM {runtime}"
        )
        op.drop_table(name, schema=SCHEMA)
