"""Persist immutable current-authority facts for stored-answer disclosure."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261027_32"
down_revision = "20261027_31"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, attribute))


def upgrade() -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    uuid_type = postgresql.UUID(as_uuid=True)
    op.add_column(
        "qa_message_versions",
        sa.Column(
            "disclosure_dependencies",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text(
                "'{\"schema_version\": \"disclosure-dependency-set:v1\", \"dependencies\": []}'::jsonb"
            ),
        ),
        schema=SCHEMA,
    )
    op.alter_column(
        "qa_message_versions",
        "disclosure_dependencies",
        schema=SCHEMA,
        existing_type=postgresql.JSONB(),
        server_default=None,
    )
    op.create_table(
        "qa_disclosure_validation_results",
        sa.Column("request_id", uuid_type, nullable=False),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("environment_id", uuid_type, nullable=False),
        sa.Column("request_digest", sa.Text(), nullable=False),
        sa.Column("checkpoint", sa.Text(), nullable=False),
        sa.Column("disposition", sa.Text(), nullable=False),
        sa.Column("failure_codes", postgresql.JSONB(), nullable=False),
        sa.Column("safe_reason", sa.Text(), nullable=False),
        sa.Column("facts", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "environment_id",
            "request_id",
            name="pk_qa_disclosure_validation_results",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_qa_disclosure_validation_results_environment",
        ),
        sa.CheckConstraint(
            "checkpoint IN ('before_stored_answer_read', 'before_history_read')",
            name="ck_qa_disclosure_validation_results_checkpoint",
        ),
        sa.CheckConstraint(
            "disposition IN ('safe_reuse', 'revalidation_required', 'ineligible', 'retryable_failure', 'cancelled')",
            name="ck_qa_disclosure_validation_results_disposition",
        ),
        schema=SCHEMA,
    )
    _scope_policy("qa_disclosure_validation_results", runtime)
    op.execute(
        f'CREATE POLICY "pol_qa_disclosure_validation_results_migration_maintenance" '
        f'ON {SCHEMA}."qa_disclosure_validation_results" FOR ALL TO {migration} '
        "USING (true) WITH CHECK (true)"
    )


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


def downgrade() -> None:
    runtime = _role("runtime_role")
    op.execute(
        f'REVOKE ALL PRIVILEGES ON TABLE {SCHEMA}."qa_disclosure_validation_results" FROM {runtime}'
    )
    op.drop_table("qa_disclosure_validation_results", schema=SCHEMA)
    op.drop_column(
        "qa_message_versions",
        "disclosure_dependencies",
        schema=SCHEMA,
    )
