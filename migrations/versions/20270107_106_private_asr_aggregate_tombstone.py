"""Add tombstone state for sanitized private ASR aggregate exports."""

from alembic import op
import sqlalchemy as sa
from spine.infrastructure.db.settings import MigrationDatabaseSettings

revision = "20270107_106"
down_revision = "20270106_105"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, attribute))


def upgrade() -> None:
    op.add_column("private_asr_aggregate_exports", sa.Column(
        "tombstoned_at", sa.DateTime(timezone=True), nullable=True), schema=SCHEMA)
    op.execute(f"DROP POLICY pol_private_asr_aggregate_exports_live_report ON {SCHEMA}.private_asr_aggregate_exports")
    runtime_role = _role("runtime_role")
    op.execute(f"""CREATE POLICY pol_private_asr_aggregate_exports_live_report
        ON {SCHEMA}.private_asr_aggregate_exports AS RESTRICTIVE FOR SELECT TO {runtime_role}
        USING ((tombstoned_at IS NULL AND expires_at > statement_timestamp()
          OR nullif(current_setting('spine.operation', true), '') = 'private_asr_retention_expiration')
          AND EXISTS (SELECT 1 FROM {SCHEMA}.private_asr_artifacts AS report
            WHERE report.workspace_id = private_asr_aggregate_exports.workspace_id
              AND report.environment_id = private_asr_aggregate_exports.environment_id
              AND report.artifact_id = private_asr_aggregate_exports.report_artifact_id
              AND report.kind = 'report' AND report.status = 'committed'
              AND report.tombstoned_at IS NULL AND report.expires_at > statement_timestamp()))""")
    op.execute(f"GRANT UPDATE (tombstoned_at, payload) ON {SCHEMA}.private_asr_aggregate_exports TO {runtime_role}")


def downgrade() -> None:
    runtime_role = _role("runtime_role")
    op.execute(f"REVOKE UPDATE (tombstoned_at, payload) ON {SCHEMA}.private_asr_aggregate_exports FROM {runtime_role}")
    op.execute(f"DROP POLICY pol_private_asr_aggregate_exports_live_report ON {SCHEMA}.private_asr_aggregate_exports")
    op.drop_column("private_asr_aggregate_exports", "tombstoned_at", schema=SCHEMA)
    op.execute(f"""CREATE POLICY pol_private_asr_aggregate_exports_live_report
        ON {SCHEMA}.private_asr_aggregate_exports AS RESTRICTIVE FOR SELECT TO {runtime_role}
        USING (expires_at > statement_timestamp()
          AND EXISTS (SELECT 1 FROM {SCHEMA}.private_asr_artifacts AS report
            WHERE report.workspace_id = private_asr_aggregate_exports.workspace_id
              AND report.environment_id = private_asr_aggregate_exports.environment_id
              AND report.artifact_id = private_asr_aggregate_exports.report_artifact_id
              AND report.kind = 'report' AND report.status = 'committed'
              AND report.tombstoned_at IS NULL AND report.expires_at > statement_timestamp()))""")
