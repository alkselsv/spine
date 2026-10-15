"""Persist accepted-answer publication acknowledgements separately."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261029_34"
down_revision = "20261028_33"
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
        "qa_answer_disclosure_deliveries",
        sa.Column("request_id", uuid_type, nullable=False),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("environment_id", uuid_type, nullable=False),
        sa.Column("output_reference", uuid_type, nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id", "environment_id", "request_id",
            name="pk_qa_answer_disclosure_deliveries",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "request_id"],
            [
                f"{SCHEMA}.qa_answer_disclosure_records.workspace_id",
                f"{SCHEMA}.qa_answer_disclosure_records.environment_id",
                f"{SCHEMA}.qa_answer_disclosure_records.request_id",
            ],
            name="fk_qa_answer_disclosure_deliveries_record",
        ),
        schema=SCHEMA,
    )
    _scope_policy("qa_answer_disclosure_deliveries", runtime)


def downgrade() -> None:
    runtime = _role("runtime_role")
    op.execute(
        f'REVOKE ALL PRIVILEGES ON TABLE {SCHEMA}."qa_answer_disclosure_deliveries" FROM {runtime}'
    )
    op.drop_table("qa_answer_disclosure_deliveries", schema=SCHEMA)
