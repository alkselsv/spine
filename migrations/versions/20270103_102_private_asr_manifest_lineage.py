"""Persist exact manifest identity and source lineage for private ASR reports."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20270103_102"
down_revision = "20270102_101"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def upgrade() -> None:
    op.add_column(
        "private_asr_artifacts",
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "private_asr_artifacts",
        sa.Column("manifest_version", sa.Text(), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "private_asr_artifacts",
        sa.Column("manifest_sha256", sa.String(64), nullable=True),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        op.f("ck_private_asr_artifacts_report_manifest_identity"),
        "private_asr_artifacts",
        "kind != 'report' OR (dataset_id IS NOT NULL AND manifest_version IS NOT NULL "
        "AND manifest_sha256 ~ '^[0-9a-f]{64}$')",
        schema=SCHEMA,
    )
    op.add_column(
        "private_asr_report_sources",
        sa.Column("source_object_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "private_asr_report_sources",
        sa.Column("source_revision_digest", sa.String(64), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "private_asr_report_sources",
        sa.Column("split", sa.Text(), nullable=True),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        op.f("ck_private_asr_report_sources_lineage"),
        "private_asr_report_sources",
        "(source_object_id IS NULL AND source_revision_digest IS NULL AND split IS NULL) OR "
        "(source_object_id IS NOT NULL AND source_revision_digest ~ '^[0-9a-f]{64}$' "
        "AND split IN ('development','qualification'))",
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_private_asr_report_sources_lineage"),
        "private_asr_report_sources", schema=SCHEMA,
    )
    op.drop_column("private_asr_report_sources", "split", schema=SCHEMA)
    op.drop_column(
        "private_asr_report_sources", "source_revision_digest", schema=SCHEMA
    )
    op.drop_column("private_asr_report_sources", "source_object_id", schema=SCHEMA)
    op.drop_constraint(
        op.f("ck_private_asr_artifacts_report_manifest_identity"),
        "private_asr_artifacts", schema=SCHEMA,
    )
    op.drop_column("private_asr_artifacts", "manifest_sha256", schema=SCHEMA)
    op.drop_column("private_asr_artifacts", "manifest_version", schema=SCHEMA)
    op.drop_column("private_asr_artifacts", "dataset_id", schema=SCHEMA)
