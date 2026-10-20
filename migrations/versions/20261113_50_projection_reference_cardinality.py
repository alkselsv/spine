"""Allow one canonical source segment to support multiple projected objects."""

from __future__ import annotations

from alembic import op


revision = "20261113_50"
down_revision = "20261112_49"
branch_labels = None
depends_on = None

SCHEMA = "spine"


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE {SCHEMA}.source_projection_references "
        "DROP CONSTRAINT IF EXISTS uq_source_projection_references_lineage"
    )


def downgrade() -> None:
    op.create_unique_constraint(
        "uq_source_projection_references_lineage",
        "source_projection_references",
        (
            "workspace_id",
            "environment_id",
            "projection_candidate_id",
            "source_object_id",
            "source_revision_id",
            "segment_id",
        ),
        schema=SCHEMA,
    )
