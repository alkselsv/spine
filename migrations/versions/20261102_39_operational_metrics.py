"""Add the tenant-scoped operational metrics source read model."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261102_39"
down_revision = "20261101_38"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, attribute))


def _tenant(table: str) -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    qualified = f'{SCHEMA}."{table}"'
    scope = (
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
        f'CREATE POLICY "pol_{table}_tenant_isolation" ON {qualified} '
        f"FOR ALL TO {runtime} USING ({scope}) WITH CHECK ({scope})"
    )
    op.execute(
        f'CREATE POLICY "pol_{table}_migration_maintenance" ON {qualified} '
        f"FOR ALL TO {migration} USING (true) WITH CHECK (true)"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE {qualified} TO {runtime}")
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {qualified} FROM {runtime}")


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "operational_metric_events",
        sa.Column("event_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("source_identity", sa.String(128), nullable=False),
        sa.Column("measurement", sa.String(128), nullable=False),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deduplication_key", sa.String(256), nullable=False),
        sa.Column("logical_operation_id", uuid),
        sa.Column("attempt_id", uuid),
        sa.Column("attempt_number", sa.Integer),
        sa.Column("terminal", sa.Boolean, nullable=False),
        sa.Column("complete_coverage", sa.Boolean, nullable=False),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("sequence_number", sa.BigInteger),
        sa.Column("source_watermark_at", sa.DateTime(timezone=True)),
        sa.Column("projection_watermark_at", sa.DateTime(timezone=True)),
        sa.Column("value", sa.Text),
        sa.Column("currency", sa.String(3)),
        sa.Column("answer_outcome", sa.String(64)),
        sa.Column("accepted", sa.Boolean),
        sa.Column("execution_started", sa.Boolean),
        sa.Column("accepted_evidence", sa.Boolean),
        sa.Column("user_initiated", sa.Boolean),
        sa.Column("feedback_id", uuid),
        sa.Column("feedback_outcome", sa.String(64)),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "environment_id",
            "event_id",
            "measurement",
            name="pk_operational_metric_events",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "deduplication_key",
            name="uq_operational_metric_events_deduplication",
        ),
        sa.CheckConstraint("attempt_number IS NULL OR attempt_number > 0", name="ck_operational_metric_attempt_number"),
        sa.CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="ck_operational_metric_duration"),
        sa.CheckConstraint("sequence_number IS NULL OR sequence_number >= 0", name="ck_operational_metric_sequence"),
        sa.CheckConstraint("currency IS NULL OR currency ~ '^[A-Z]{3}$'", name="ck_operational_metric_currency"),
        sa.CheckConstraint(
            "measurement <> 'qa.cost' OR (value IS NOT NULL AND value ~ '^(0|[1-9][0-9]*)(\\.[0-9]+)?$' AND currency IS NOT NULL)",
            name="ck_operational_metric_cost",
        ),
        sa.CheckConstraint(
            "measurement <> 'qa.answer_rate' OR (answer_outcome IS NOT NULL AND accepted IS NOT NULL AND execution_started IS NOT NULL AND accepted_evidence IS NOT NULL AND user_initiated IS NOT NULL)",
            name="ck_operational_metric_answer_rate",
        ),
        sa.CheckConstraint(
            "measurement <> 'qa.usefulness' OR (source_identity = 'canonical.qa-feedback:v1' AND feedback_id IS NOT NULL AND feedback_outcome IS NOT NULL)",
            name="ck_operational_metric_usefulness",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_operational_metric_events_window",
        "operational_metric_events",
        ["workspace_id", "environment_id", "measurement", "event_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_operational_metric_events_operation",
        "operational_metric_events",
        ["workspace_id", "environment_id", "logical_operation_id", "event_at"],
        schema=SCHEMA,
    )
    _tenant("operational_metric_events")


def downgrade() -> None:
    runtime = _role("runtime_role")
    op.execute(f'REVOKE ALL PRIVILEGES ON TABLE {SCHEMA}."operational_metric_events" FROM {runtime}')
    op.drop_index("ix_operational_metric_events_operation", table_name="operational_metric_events", schema=SCHEMA)
    op.drop_index("ix_operational_metric_events_window", table_name="operational_metric_events", schema=SCHEMA)
    op.drop_table("operational_metric_events", schema=SCHEMA)
