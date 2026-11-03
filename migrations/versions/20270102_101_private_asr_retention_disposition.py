"""Persist the exact retention authority used for private ASR reports."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20270102_101"
down_revision = "20270101_100"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def upgrade() -> None:
    op.add_column(
        "private_asr_artifacts",
        sa.Column("retention_disposition", postgresql.JSONB(), nullable=True),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        op.f("ck_private_asr_artifacts_report_retention_disposition"),
        "private_asr_artifacts",
        "kind != 'report' OR (jsonb_typeof(retention_disposition) = 'array' "
        "AND jsonb_array_length(retention_disposition) > 0)",
        schema=SCHEMA,
        postgresql_not_valid=True,
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_private_asr_artifacts_report_retention_disposition"),
        "private_asr_artifacts",
        schema=SCHEMA,
    )
    op.drop_column("private_asr_artifacts", "retention_disposition", schema=SCHEMA)
