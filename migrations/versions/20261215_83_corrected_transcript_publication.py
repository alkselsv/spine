"""Allow transcript publications to reference reviewed correction artifacts."""

from alembic import op

revision = "20261215_83"
down_revision = "20261214_82"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def upgrade() -> None:
    op.drop_constraint(
        "fk_transcript_publications_artifact",
        "transcript_publications",
        schema=SCHEMA,
        type_="foreignkey",
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
