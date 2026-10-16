"""Persist immutable administrator feedback observations."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261030_36"
down_revision = "20261030_35"
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
    op.execute(
        f'CREATE POLICY "pol_{table}_migration_maintenance" ON {qualified} FOR ALL TO '
        f'{_role("migration_role")} USING (true) WITH CHECK (true)'
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE {qualified} TO {role}")


def upgrade() -> None:
    runtime = _role("runtime_role")
    uuid_type = postgresql.UUID(as_uuid=True)
    op.create_table(
        "feedback_records",
        sa.Column("feedback_id", uuid_type, nullable=False),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("environment_id", uuid_type, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("subject_id", uuid_type, nullable=False),
        sa.Column("trace_id", uuid_type, nullable=False),
        sa.Column("run_id", uuid_type, nullable=False),
        sa.Column("message_id", uuid_type, nullable=True),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("target_id", uuid_type, nullable=False),
        sa.Column("citation_id", uuid_type, nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id", "environment_id", "feedback_id", "version",
            name="pk_feedback_records",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_feedback_records_environment",
        ),
        sa.CheckConstraint("version = 1", name="ck_feedback_records_version"),
        sa.CheckConstraint(
            "target_kind IN ('answer', 'citation')",
            name="ck_feedback_records_target_kind",
        ),
        sa.CheckConstraint(
            "outcome IN ('answer.helpful', 'answer.incorrect', 'citation.helpful', 'citation.incorrect')",
            name="ck_feedback_records_outcome",
        ),
        sa.CheckConstraint(
            "length(comment) <= 1000 AND length(reason) <= 256",
            name="ck_feedback_records_bounded_text",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_feedback_records_target",
        "feedback_records",
        ["workspace_id", "environment_id", "target_kind", "target_id", "created_at"],
        schema=SCHEMA,
    )
    _scope_policy("feedback_records", runtime)


def downgrade() -> None:
    runtime = _role("runtime_role")
    op.execute(f'REVOKE ALL PRIVILEGES ON TABLE {SCHEMA}."feedback_records" FROM {runtime}')
    op.drop_index("ix_feedback_records_target", table_name="feedback_records", schema=SCHEMA)
    op.drop_table("feedback_records", schema=SCHEMA)
