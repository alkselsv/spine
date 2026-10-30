"""Allow transcript publications to reference reviewed correction artifacts."""

from alembic import op

revision = "20261216_84"
down_revision = "20261215_83"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def upgrade() -> None:
    op.execute(
        f'ALTER TABLE {SCHEMA}."transcript_publications" '
        'DROP CONSTRAINT IF EXISTS "fk_transcript_publications_artifact"'
    )


def downgrade() -> None:
    op.create_foreign_key(
        "fk_transcript_publications_artifact",
        "transcript_publications",
        "transcription_artifacts",
        ["workspace_id", "environment_id", "transcript_artifact_id"],
        ["workspace_id", "environment_id", "artifact_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )
