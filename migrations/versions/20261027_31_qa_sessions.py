"""Persist immutable Q&A session and message versions."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261027_31"
down_revision = "20261026_30"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, attribute))


def _scope_policy(table: str, role: str) -> None:
    qualified = f'{SCHEMA}."{table}"'
    expression = (
        "workspace_id = (CASE WHEN pg_catalog.current_setting('spine.workspace_id', true) "
        "OPERATOR(pg_catalog.~) '^[0-9A-Fa-f-]{36}$' THEN "
        "CAST(pg_catalog.current_setting('spine.workspace_id', true) AS pg_catalog.uuid) ELSE NULL END) "
        "AND environment_id = (CASE WHEN pg_catalog.current_setting('spine.environment_id', true) "
        "OPERATOR(pg_catalog.~) '^[0-9A-Fa-f-]{36}$' THEN "
        "CAST(pg_catalog.current_setting('spine.environment_id', true) AS pg_catalog.uuid) ELSE NULL END)"
    )
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(
        f'CREATE POLICY "pol_{table}_tenant_isolation" ON {qualified} FOR ALL TO {role} '
        f"USING ({expression}) WITH CHECK ({expression})"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE {qualified} TO {role}")


def upgrade() -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    uuid_type = postgresql.UUID(as_uuid=True)
    op.create_table(
        "qa_session_versions",
        sa.Column("session_id", uuid_type, nullable=False),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("environment_id", uuid_type, nullable=False),
        sa.Column("subject_id", uuid_type, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "environment_id", "session_id", "version", name="pk_qa_session_versions"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id"], [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"], name="fk_qa_session_versions_environment"),
        sa.CheckConstraint("version > 0", name="ck_qa_session_versions_version_positive"),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_qa_session_versions_status"),
        schema=SCHEMA,
    )
    op.create_index("ix_qa_session_versions_navigation", "qa_session_versions", ["workspace_id", "environment_id", "created_at", "session_id", "version"], schema=SCHEMA)
    op.create_table(
        "qa_message_versions",
        sa.Column("message_id", uuid_type, nullable=False),
        sa.Column("session_id", uuid_type, nullable=False),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("environment_id", uuid_type, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("trace_id", uuid_type, nullable=False),
        sa.Column("run_id", uuid_type, nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("terminal_outcome", sa.Text(), nullable=True),
        sa.Column("cancelled", sa.Boolean(), nullable=False),
        sa.Column("disclosure_dependency_ids", postgresql.JSONB(), nullable=False),
        sa.Column("disclosure_eligibility", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "environment_id", "message_id", "version", name="pk_qa_message_versions"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id"], [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"], name="fk_qa_message_versions_environment"),
        sa.CheckConstraint("version > 0", name="ck_qa_message_versions_version_positive"),
        sa.CheckConstraint("ordinal > 0", name="ck_qa_message_versions_ordinal_positive"),
        sa.CheckConstraint("state IN ('pending', 'running', 'terminal')", name="ck_qa_message_versions_state"),
        sa.CheckConstraint("terminal_outcome IS NULL OR terminal_outcome IN ('answered', 'clarification_required', 'abstained', 'denied', 'failed', 'cancelled')", name="ck_qa_message_versions_outcome"),
        schema=SCHEMA,
    )
    op.create_index("ix_qa_message_versions_navigation", "qa_message_versions", ["workspace_id", "environment_id", "session_id", "ordinal", "message_id", "version"], schema=SCHEMA)
    _scope_policy("qa_session_versions", runtime)
    _scope_policy("qa_message_versions", runtime)
    op.execute(f'CREATE POLICY "pol_qa_session_versions_migration_maintenance" ON {SCHEMA}."qa_session_versions" FOR ALL TO {migration} USING (true) WITH CHECK (true)')
    op.execute(f'CREATE POLICY "pol_qa_message_versions_migration_maintenance" ON {SCHEMA}."qa_message_versions" FOR ALL TO {migration} USING (true) WITH CHECK (true)')


def downgrade() -> None:
    runtime = _role("runtime_role")
    op.execute(f'REVOKE ALL PRIVILEGES ON TABLE {SCHEMA}."qa_message_versions" FROM {runtime}')
    op.execute(f'REVOKE ALL PRIVILEGES ON TABLE {SCHEMA}."qa_session_versions" FROM {runtime}')
    op.drop_index("ix_qa_message_versions_navigation", table_name="qa_message_versions", schema=SCHEMA)
    op.drop_table("qa_message_versions", schema=SCHEMA)
    op.drop_index("ix_qa_session_versions_navigation", table_name="qa_session_versions", schema=SCHEMA)
    op.drop_table("qa_session_versions", schema=SCHEMA)
