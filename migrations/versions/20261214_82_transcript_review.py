"""Persist append-only transcript reviews and immutable human corrections."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings

revision = "20261214_82"
down_revision = "20261213_81"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "transcript_reviews",
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("review_id", uuid, nullable=False),
        sa.Column("review_digest", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("command_digest", sa.String(64), nullable=False),
        sa.Column("source_object_id", uuid, nullable=False),
        sa.Column("source_revision_id", uuid, nullable=False),
        sa.Column("source_revision_digest", sa.String(64), nullable=False),
        sa.Column("input_artifact_id", uuid, nullable=False),
        sa.Column("input_artifact_digest", sa.String(64), nullable=False),
        sa.Column("input_artifact_schema_version", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("actor_id", uuid, nullable=False),
        sa.Column("actor_authority", sa.Text(), nullable=False),
        sa.Column("policy_reference", sa.Text(), nullable=False),
        sa.Column("publication_eligible", sa.Boolean(), nullable=False),
        sa.Column("correlation_id", uuid, nullable=False),
        sa.Column("corrected_artifact_id", uuid, nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id", "environment_id", "review_id", name="pk_transcript_reviews"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "input_artifact_id",
            name="uq_transcript_reviews_artifact",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "actor_id",
            "idempotency_key",
            name="uq_transcript_reviews_command",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "input_artifact_id"],
            [
                f"{SCHEMA}.transcription_artifacts.workspace_id",
                f"{SCHEMA}.transcription_artifacts.environment_id",
                f"{SCHEMA}.transcription_artifacts.artifact_id",
            ],
            name="fk_transcript_reviews_input_artifact",
        ),
        sa.CheckConstraint(
            "decision IN ('approve', 'correct', 'reject')",
            name=op.f("ck_transcript_reviews_decision"),
        ),
        sa.CheckConstraint(
            "(decision = 'correct' AND corrected_artifact_id IS NOT NULL) OR "
            "(decision <> 'correct' AND corrected_artifact_id IS NULL)",
            name=op.f("ck_transcript_reviews_correction_shape"),
        ),
        sa.CheckConstraint(
            "length(review_digest) = 64 AND length(command_digest) = 64",
            name=op.f("ck_transcript_reviews_digest"),
        ),
        schema=SCHEMA,
        if_not_exists=True,
    )
    op.create_table(
        "corrected_transcript_artifacts",
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("artifact_id", uuid, nullable=False),
        sa.Column("artifact_digest", sa.String(64), nullable=False),
        sa.Column("original_artifact_id", uuid, nullable=False),
        sa.Column("review_id", uuid, nullable=False),
        sa.Column("artifact", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "environment_id",
            "artifact_id",
            name="pk_corrected_transcript_artifacts",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "review_id",
            name="uq_corrected_transcript_artifacts_review",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "original_artifact_id"],
            [
                f"{SCHEMA}.transcription_artifacts.workspace_id",
                f"{SCHEMA}.transcription_artifacts.environment_id",
                f"{SCHEMA}.transcription_artifacts.artifact_id",
            ],
            name="fk_corrected_transcripts_original_artifact",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "review_id"],
            [
                f"{SCHEMA}.transcript_reviews.workspace_id",
                f"{SCHEMA}.transcript_reviews.environment_id",
                f"{SCHEMA}.transcript_reviews.review_id",
            ],
            name="fk_corrected_transcripts_review",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint(
            "length(artifact_digest) = 64",
            name=op.f("ck_corrected_transcript_artifacts_digest"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(artifact) = 'object' AND artifact->>'schema_version' = 'human-corrected-transcript:v1'",
            name=op.f("ck_corrected_transcript_artifacts_shape"),
        ),
        schema=SCHEMA,
        if_not_exists=True,
    )
    _tenant_scope("transcript_reviews")
    _tenant_scope("corrected_transcript_artifacts")


def _tenant_scope(table: str) -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    qualified = f'{SCHEMA}."{table}"'
    ws_setting = "pg_catalog.current_setting('spine.workspace_id', true)"
    env_setting = "pg_catalog.current_setting('spine.environment_id', true)"
    pattern = (
        "^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
    )
    ws = f"CASE WHEN {ws_setting} OPERATOR(pg_catalog.~) '{pattern}' THEN CAST({ws_setting} AS pg_catalog.uuid) ELSE NULL END"
    env = f"CASE WHEN {env_setting} OPERATOR(pg_catalog.~) '{pattern}' THEN CAST({env_setting} AS pg_catalog.uuid) ELSE NULL END"
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(
        f'CREATE POLICY "pol_{table}_tenant_isolation" ON {qualified} FOR ALL TO {runtime} USING (workspace_id = ({ws}) AND environment_id = ({env})) WITH CHECK (workspace_id = ({ws}) AND environment_id = ({env}))'
    )
    op.execute(
        f'CREATE POLICY "pol_{table}_migration_maintenance" ON {qualified} FOR ALL TO {migration} USING (true) WITH CHECK (true)'
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE {qualified} TO {runtime}")
    op.execute(
        f"REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {qualified} FROM {runtime}"
    )


def downgrade() -> None:
    op.drop_table("corrected_transcript_artifacts", schema=SCHEMA)
    op.drop_table("transcript_reviews", schema=SCHEMA)
