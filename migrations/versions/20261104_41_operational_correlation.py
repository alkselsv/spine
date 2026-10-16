"""Add the tenant-scoped operational correlation read model."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261104_41"
down_revision = "20261103_40"
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
    op.execute(
        f"REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {qualified} FROM {runtime}"
    )


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "operational_correlation_records",
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_identity", sa.String(128), nullable=False),
        sa.Column("source_schema_version", sa.String(128), nullable=False),
        sa.Column("source_record_id", uuid, nullable=False),
        sa.Column("deduplication_key", sa.String(256), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trace_id", uuid),
        sa.Column("correlation_id", uuid),
        sa.Column("run_id", uuid),
        sa.Column("attempt_id", uuid),
        sa.Column("message_id", uuid),
        sa.Column("feedback_id", uuid),
        sa.Column("delivery_event_id", uuid),
        sa.Column("metric_measurement", sa.String(128)),
        sa.Column("metric_coverage", sa.String(32)),
        sa.Column("metric_value", sa.Text),
        sa.Column("metric_numerator", sa.Integer),
        sa.Column("metric_denominator", sa.Integer),
        sa.Column("metric_currency", sa.String(3)),
        sa.Column("delivery_status", sa.String(32)),
        sa.Column("delivery_attempt_count", sa.Integer),
        sa.Column("delivery_created_at", sa.DateTime(timezone=True)),
        sa.Column("delivery_updated_at", sa.DateTime(timezone=True)),
        sa.Column("delivery_quarantined_at", sa.DateTime(timezone=True)),
        sa.Column("delivery_last_error_code", sa.String(128)),
        sa.Column("run_state", sa.String(32)),
        sa.Column("run_outcome", sa.String(64)),
        sa.Column("feedback_target_kind", sa.String(32)),
        sa.Column("feedback_outcome", sa.String(64)),
        sa.PrimaryKeyConstraint(
            "workspace_id", "environment_id", "source_kind", "source_record_id", "deduplication_key",
            name="pk_operational_correlation_records",
        ),
        sa.UniqueConstraint(
            "workspace_id", "environment_id", "deduplication_key",
            name="uq_operational_correlation_records_deduplication",
        ),
        sa.CheckConstraint("metric_numerator IS NULL OR metric_numerator >= 0", name="ck_operational_correlation_metric_numerator"),
        sa.CheckConstraint("metric_denominator IS NULL OR metric_denominator >= 0", name="ck_operational_correlation_metric_denominator"),
        sa.CheckConstraint("delivery_attempt_count IS NULL OR delivery_attempt_count >= 1", name="ck_operational_correlation_attempt_count"),
        sa.CheckConstraint("metric_currency IS NULL OR metric_currency ~ '^[A-Z]{3}$'", name="ck_operational_correlation_currency"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_operational_correlation_trace",
        "operational_correlation_records",
        ["workspace_id", "environment_id", "trace_id", "observed_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_operational_correlation_reference",
        "operational_correlation_records",
        ["workspace_id", "environment_id", "correlation_id", "run_id", "delivery_event_id"],
        schema=SCHEMA,
    )
    _tenant("operational_correlation_records")


def downgrade() -> None:
    runtime = _role("runtime_role")
    op.execute(
        f'REVOKE ALL PRIVILEGES ON TABLE {SCHEMA}."operational_correlation_records" FROM {runtime}'
    )
    op.drop_index(
        "ix_operational_correlation_reference",
        table_name="operational_correlation_records",
        schema=SCHEMA,
    )
    op.drop_index(
        "ix_operational_correlation_trace",
        table_name="operational_correlation_records",
        schema=SCHEMA,
    )
    op.drop_table("operational_correlation_records", schema=SCHEMA)
