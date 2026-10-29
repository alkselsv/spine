"""Persist bounded, partial and cancellable transcription outcomes."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings

revision = "20261211_79"
down_revision = "20261210_78"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def _failure_codes() -> str:
    return (
        "'transient_unavailable', 'permanent_model_error', 'policy_denied', "
        "'unsupported_media', 'corrupt_media', 'resource_limit_exceeded', "
        "'no_speech', 'retry_exhausted', 'cancelled', 'invalid_adapter_output'"
    )


def _v1_failure_codes() -> str:
    return (
        "'transient_unavailable', 'permanent_model_error', 'policy_denied', "
        "'unsupported_media', 'invalid_adapter_output'"
    )


def upgrade() -> None:
    op.add_column(
        "transcription_jobs",
        sa.Column(
            "schema_version",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'transcription-job:v1'"),
        ),
        schema=SCHEMA,
    )
    op.add_column(
        "transcription_jobs",
        sa.Column(
            "cancellation_requested_at", sa.DateTime(timezone=True), nullable=True
        ),
        schema=SCHEMA,
    )
    op.add_column(
        "transcription_artifacts",
        sa.Column("media_inspection", postgresql.JSONB(), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "transcription_artifacts",
        sa.Column("decoder_reference", sa.Text(), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "transcription_artifacts",
        sa.Column("decoded_sha256", sa.String(64), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "transcription_artifacts",
        sa.Column("reassembly_reference", sa.Text(), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "transcription_artifacts",
        sa.Column(
            "chunk_receipts",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        schema=SCHEMA,
    )
    op.add_column(
        "transcription_artifacts",
        sa.Column(
            "outcome", sa.Text(), nullable=False, server_default=sa.text("'completed'")
        ),
        schema=SCHEMA,
    )
    op.add_column(
        "transcription_artifacts",
        sa.Column("failure", postgresql.JSONB(), nullable=True),
        schema=SCHEMA,
    )

    op.drop_constraint(
        "ck_transcription_jobs_state",
        "transcription_jobs",
        schema=SCHEMA,
        type_="check",
    )
    op.drop_constraint(
        "ck_transcription_jobs_terminal_shape",
        "transcription_jobs",
        schema=SCHEMA,
        type_="check",
    )
    op.create_check_constraint(
        "ck_transcription_jobs_state",
        "transcription_jobs",
        "state IN ('pending', 'completed', 'partial', 'failed', 'cancelled')",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_transcription_jobs_contract_version",
        "transcription_jobs",
        "(schema_version = 'transcription-job:v1' "
        "AND state <> 'partial' AND cancellation_requested_at IS NULL) OR "
        "schema_version = 'transcription-job:v2'",
        schema=SCHEMA,
    )
    failure_codes = _failure_codes()
    v1_failure_codes = _v1_failure_codes()
    op.create_check_constraint(
        "ck_transcription_jobs_terminal_shape",
        "transcription_jobs",
        "(state = 'pending' AND artifact_id IS NULL AND terminal_code IS NULL "
        "AND retryable IS NULL AND completed_at IS NULL) OR "
        "(state = 'completed' AND artifact_id IS NOT NULL AND terminal_code = 'completed' "
        "AND retryable IS NULL AND completed_at IS NOT NULL) OR "
        f"(state = 'partial' AND artifact_id IS NOT NULL AND terminal_code IN ({failure_codes}) "
        "AND retryable IS NOT NULL AND completed_at IS NOT NULL) OR "
        "(state = 'cancelled' AND terminal_code = 'cancelled' AND retryable = false "
        "AND completed_at IS NOT NULL) OR "
        "(state = 'failed' AND artifact_id IS NULL AND ("
        f"(schema_version = 'transcription-job:v1' AND terminal_code IN ({v1_failure_codes})) OR "
        f"(schema_version = 'transcription-job:v2' AND terminal_code IN ({failure_codes}))) "
        "AND retryable IS NOT NULL AND completed_at IS NOT NULL)",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_transcription_artifacts_bounded_shape",
        "transcription_artifacts",
        "((schema_version = 'transcription-artifact:v1' "
        "AND media_inspection IS NULL AND decoder_reference IS NULL "
        "AND decoded_sha256 IS NULL AND reassembly_reference IS NULL "
        "AND chunk_receipts = '[]'::jsonb AND outcome = 'completed' "
        "AND failure IS NULL) OR "
        "(schema_version = 'transcription-artifact:v2' "
        "AND media_inspection IS NOT NULL AND decoder_reference IS NOT NULL "
        "AND decoded_sha256 IS NOT NULL AND reassembly_reference IS NOT NULL)) "
        "AND ((outcome = 'completed' AND failure IS NULL) OR "
        "(outcome = 'partial' AND failure IS NOT NULL "
        "AND jsonb_array_length(chunk_receipts) > 0))",
        schema=SCHEMA,
    )

    op.execute(f"""
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
        """)
    runtime = _role("runtime_role")
    op.execute(
        f"GRANT UPDATE (cancellation_requested_at) "
        f"ON TABLE {SCHEMA}.transcription_jobs TO {runtime}"
    )


def downgrade() -> None:
    runtime = _role("runtime_role")
    op.execute(
        f"DROP TRIGGER IF EXISTS trg_transcription_jobs_terminal_immutable "
        f"ON {SCHEMA}.transcription_jobs"
    )
    op.execute(
        f"REVOKE UPDATE (cancellation_requested_at) "
        f"ON TABLE {SCHEMA}.transcription_jobs FROM {runtime}"
    )
    op.drop_constraint(
        "ck_transcription_jobs_contract_version",
        "transcription_jobs",
        schema=SCHEMA,
        type_="check",
    )
    op.drop_constraint(
        "ck_transcription_artifacts_bounded_shape",
        "transcription_artifacts",
        schema=SCHEMA,
        type_="check",
    )
    op.drop_constraint(
        "ck_transcription_jobs_terminal_shape",
        "transcription_jobs",
        schema=SCHEMA,
        type_="check",
    )
    op.drop_constraint(
        "ck_transcription_jobs_state",
        "transcription_jobs",
        schema=SCHEMA,
        type_="check",
    )
    op.execute(
        f"UPDATE {SCHEMA}.transcription_jobs SET state = 'failed', artifact_id = NULL, "
        "terminal_code = CASE WHEN terminal_code IN "
        "('transient_unavailable', 'permanent_model_error', 'policy_denied', "
        "'unsupported_media', 'invalid_adapter_output') THEN terminal_code "
        "ELSE 'permanent_model_error' END "
        "WHERE state = 'partial'"
    )
    op.execute(
        f"UPDATE {SCHEMA}.transcription_jobs SET artifact_id = NULL "
        "WHERE state = 'cancelled'"
    )
    op.create_check_constraint(
        "ck_transcription_jobs_state",
        "transcription_jobs",
        "state IN ('pending', 'completed', 'failed', 'cancelled')",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_transcription_jobs_terminal_shape",
        "transcription_jobs",
        "(state = 'pending' AND artifact_id IS NULL AND terminal_code IS NULL "
        "AND retryable IS NULL AND completed_at IS NULL) OR "
        "(state = 'completed' AND artifact_id IS NOT NULL AND terminal_code = 'completed' "
        "AND retryable IS NULL AND completed_at IS NOT NULL) OR "
        "(state = 'cancelled' AND artifact_id IS NULL AND terminal_code = 'cancelled' "
        "AND retryable = false AND completed_at IS NOT NULL) OR "
        "(state = 'failed' AND artifact_id IS NULL AND terminal_code IN "
        "('transient_unavailable', 'permanent_model_error', 'policy_denied', "
        "'unsupported_media', 'invalid_adapter_output') "
        "AND retryable IS NOT NULL AND completed_at IS NOT NULL)",
        schema=SCHEMA,
    )
    for column in (
        "failure",
        "outcome",
        "chunk_receipts",
        "reassembly_reference",
        "decoded_sha256",
        "decoder_reference",
        "media_inspection",
    ):
        op.drop_column("transcription_artifacts", column, schema=SCHEMA)
    op.drop_column("transcription_jobs", "cancellation_requested_at", schema=SCHEMA)
    op.drop_column("transcription_jobs", "schema_version", schema=SCHEMA)
    op.execute(f"""
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
            IF NEW.state = 'pending' AND (
                NEW.execution_attempt <> OLD.execution_attempt + 1
                OR NEW.execution_lease_id = OLD.execution_lease_id
                OR NEW.execution_lease_expires_at <= OLD.execution_lease_expires_at
                OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
                OR NEW.environment_id IS DISTINCT FROM OLD.environment_id
                OR NEW.job_id IS DISTINCT FROM OLD.job_id
                OR NEW.command_digest IS DISTINCT FROM OLD.command_digest
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
        """)
    op.execute(
        f"CREATE TRIGGER trg_transcription_jobs_terminal_immutable "
        f"BEFORE UPDATE ON {SCHEMA}.transcription_jobs FOR EACH ROW "
        f"EXECUTE FUNCTION {SCHEMA}.enforce_transcription_job_transition()"
    )
