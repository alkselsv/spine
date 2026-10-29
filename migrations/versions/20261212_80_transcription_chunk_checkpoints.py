"""Persist fenced cumulative transcription chunk checkpoints."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings

revision = "20261212_80"
down_revision = "20261211_79"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def _transition_function(*, checkpoint_column: bool) -> str:
    checkpoint_branch = ""
    if checkpoint_column:
        checkpoint_branch = """
            IF NEW.state = 'pending' AND NEW.chunk_checkpoint IS DISTINCT FROM OLD.chunk_checkpoint THEN
                IF NEW.chunk_checkpoint IS NULL
                   OR NEW.execution_attempt IS DISTINCT FROM OLD.execution_attempt
                   OR NEW.execution_lease_id IS DISTINCT FROM OLD.execution_lease_id
                   OR NEW.execution_lease_expires_at IS DISTINCT FROM OLD.execution_lease_expires_at
                   OR NEW.cancellation_requested_at IS DISTINCT FROM OLD.cancellation_requested_at
                   OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
                   OR NEW.environment_id IS DISTINCT FROM OLD.environment_id
                   OR NEW.job_id IS DISTINCT FROM OLD.job_id
                   OR NEW.command_digest IS DISTINCT FROM OLD.command_digest THEN
                    RAISE EXCEPTION 'invalid transcription chunk checkpoint'
                        USING ERRCODE = '23514',
                              CONSTRAINT = 'ck_transcription_jobs_chunk_checkpoint';
                END IF;
                RETURN NEW;
            END IF;
        """
    return f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.enforce_transcription_job_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        BEGIN
            IF OLD.state <> 'pending' THEN
                RAISE EXCEPTION 'transcription job transition is immutable'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_transcription_jobs_terminal_immutable';
            END IF;
            {checkpoint_branch}
            IF NEW.state = 'pending' AND NEW.cancellation_requested_at IS DISTINCT FROM OLD.cancellation_requested_at THEN
                IF OLD.cancellation_requested_at IS NOT NULL
                   OR NEW.cancellation_requested_at IS NULL
                   OR NEW.execution_attempt IS DISTINCT FROM OLD.execution_attempt
                   OR NEW.execution_lease_id IS DISTINCT FROM OLD.execution_lease_id
                   OR NEW.execution_lease_expires_at IS DISTINCT FROM OLD.execution_lease_expires_at
                   OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
                   OR NEW.environment_id IS DISTINCT FROM OLD.environment_id
                   OR NEW.job_id IS DISTINCT FROM OLD.job_id
                   OR NEW.command_digest IS DISTINCT FROM OLD.command_digest THEN
                    RAISE EXCEPTION 'invalid transcription cancellation request'
                        USING ERRCODE = '23514',
                              CONSTRAINT = 'ck_transcription_jobs_cancellation_request';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.state = 'pending' AND (
                NEW.execution_attempt <> OLD.execution_attempt + 1
                OR NEW.execution_lease_id = OLD.execution_lease_id
                OR NEW.execution_lease_expires_at <= OLD.execution_lease_expires_at
                OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
                OR NEW.environment_id IS DISTINCT FROM OLD.environment_id
                OR NEW.job_id IS DISTINCT FROM OLD.job_id
                OR NEW.command_digest IS DISTINCT FROM OLD.command_digest
                OR NEW.cancellation_requested_at IS DISTINCT FROM OLD.cancellation_requested_at
            ) THEN
                RAISE EXCEPTION 'invalid transcription lease reclaim'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_transcription_jobs_lease_reclaim';
            END IF;
            IF NEW.state <> 'pending' AND (
                NEW.execution_lease_id IS DISTINCT FROM OLD.execution_lease_id
                OR NEW.execution_lease_expires_at IS DISTINCT FROM OLD.execution_lease_expires_at
                OR NEW.execution_attempt IS DISTINCT FROM OLD.execution_attempt
            ) THEN
                RAISE EXCEPTION 'terminal transition cannot replace execution lease'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_transcription_jobs_terminal_lease_immutable';
            END IF;
            RETURN NEW;
        END;
        $function$
    """


def upgrade() -> None:
    op.add_column(
        "transcription_jobs",
        sa.Column("chunk_checkpoint", postgresql.JSONB(), nullable=True),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        op.f("ck_transcription_jobs_chunk_checkpoint_shape"),
        "transcription_jobs",
        "chunk_checkpoint IS NULL OR (schema_version = 'transcription-job:v2' "
        "AND jsonb_typeof(chunk_checkpoint) = 'object' "
        "AND chunk_checkpoint->>'schema_version' = 'transcription-chunk-checkpoint:v1' "
        "AND chunk_checkpoint->>'job_id' = job_id::text "
        "AND chunk_checkpoint->>'source_revision_id' = source_revision_id::text "
        "AND chunk_checkpoint->>'source_revision_digest' = source_revision_digest "
        "AND chunk_checkpoint->>'processing_profile_reference' = processing_profile_reference "
        "AND jsonb_array_length(chunk_checkpoint->'chunk_receipts') > 0)",
        schema=SCHEMA,
    )
    op.execute(_transition_function(checkpoint_column=True))
    runtime = _role("runtime_role")
    op.execute(
        f"GRANT UPDATE (chunk_checkpoint) "
        f"ON TABLE {SCHEMA}.transcription_jobs TO {runtime}"
    )


def downgrade() -> None:
    runtime = _role("runtime_role")
    op.execute(
        f"REVOKE UPDATE (chunk_checkpoint) "
        f"ON TABLE {SCHEMA}.transcription_jobs FROM {runtime}"
    )
    op.drop_constraint(
        op.f("ck_transcription_jobs_chunk_checkpoint_shape"),
        "transcription_jobs",
        schema=SCHEMA,
        type_="check",
    )
    op.drop_column("transcription_jobs", "chunk_checkpoint", schema=SCHEMA)
    op.execute(_transition_function(checkpoint_column=False))
