"""Add tombstone state for sanitized private ASR aggregate exports."""

from alembic import op
import sqlalchemy as sa

revision = "20270106_105"
down_revision = "20270105_104"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def upgrade() -> None:
    op.add_column("private_asr_aggregate_exports", sa.Column(
        "tombstoned_at", sa.DateTime(timezone=True), nullable=True), schema=SCHEMA)
    op.execute(f"DROP POLICY pol_private_asr_aggregate_exports_live_report ON {SCHEMA}.private_asr_aggregate_exports")
    op.execute(f"""CREATE POLICY pol_private_asr_aggregate_exports_live_report
        ON {SCHEMA}.private_asr_aggregate_exports AS RESTRICTIVE FOR SELECT TO spine_runtime
        USING ((tombstoned_at IS NULL AND expires_at > statement_timestamp()
          OR nullif(current_setting('spine.operation', true), '') = 'private_asr_retention_expiration')
          AND EXISTS (SELECT 1 FROM {SCHEMA}.private_asr_artifacts AS report
            WHERE report.workspace_id = private_asr_aggregate_exports.workspace_id
              AND report.environment_id = private_asr_aggregate_exports.environment_id
              AND report.artifact_id = private_asr_aggregate_exports.report_artifact_id
              AND report.kind = 'report' AND report.status = 'committed'
              AND report.tombstoned_at IS NULL AND report.expires_at > statement_timestamp()))""")
    op.execute(f"GRANT UPDATE (tombstoned_at, payload) ON {SCHEMA}.private_asr_aggregate_exports TO spine_runtime")


def downgrade() -> None:
    op.execute(f"REVOKE UPDATE (tombstoned_at, payload) ON {SCHEMA}.private_asr_aggregate_exports FROM spine_runtime")
    op.execute(f"DROP POLICY pol_private_asr_aggregate_exports_live_report ON {SCHEMA}.private_asr_aggregate_exports")
    op.drop_column("private_asr_aggregate_exports", "tombstoned_at", schema=SCHEMA)
