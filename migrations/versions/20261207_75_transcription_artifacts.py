"""Persist governed transcription jobs and immutable timestamped artifacts."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261207_75"
down_revision = "20261206_74"
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
        "transcription_jobs",
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("job_id", uuid, nullable=False),
        sa.Column("source_object_id", uuid, nullable=False),
        sa.Column("source_revision_id", uuid, nullable=False),
        sa.Column("source_revision_digest", sa.String(64), nullable=False),
        sa.Column("original_sha256", sa.String(64), nullable=False),
        sa.Column("processing_profile_reference", sa.Text(), nullable=False),
        sa.Column("adapter_reference", sa.Text(), nullable=False),
        sa.Column("model_config_reference", sa.Text(), nullable=False),
        sa.Column("authorization_decision_id", uuid, nullable=False),
        sa.Column("authorization_policy_reference", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("command_digest", sa.String(64), nullable=False),
        sa.Column("execution_lease_id", uuid, nullable=False),
        sa.Column("execution_lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("execution_attempt", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("artifact_id", uuid, nullable=True),
        sa.Column("terminal_code", sa.Text(), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "workspace_id", "environment_id", "job_id", name="pk_transcription_jobs"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "idempotency_key",
            name="uq_transcription_jobs_command",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "source_revision_id"],
            [
                f"{SCHEMA}.source_revisions.workspace_id",
                f"{SCHEMA}.source_revisions.environment_id",
                f"{SCHEMA}.source_revisions.source_object_id",
                f"{SCHEMA}.source_revisions.revision_id",
            ],
            name="fk_transcription_jobs_source_revision",
        ),
        sa.CheckConstraint(
            "execution_attempt > 0",
            name=op.f("ck_transcription_jobs_execution_attempt_positive"),
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'completed', 'failed', 'cancelled')",
            name=op.f("ck_transcription_jobs_state"),
        ),
        sa.CheckConstraint(
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
            name=op.f("ck_transcription_jobs_terminal_shape"),
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "transcription_artifacts",
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("artifact_id", uuid, nullable=False),
        sa.Column("artifact_digest", sa.String(64), nullable=False),
        sa.Column("source_object_id", uuid, nullable=False),
        sa.Column("source_revision_id", uuid, nullable=False),
        sa.Column("source_revision_digest", sa.String(64), nullable=False),
        sa.Column("original_sha256", sa.String(64), nullable=False),
        sa.Column("processing_job_id", uuid, nullable=False),
        sa.Column("processing_profile_reference", sa.Text(), nullable=False),
        sa.Column("adapter_reference", sa.Text(), nullable=False),
        sa.Column("model_config_reference", sa.Text(), nullable=False),
        sa.Column("recording_duration_ms", sa.Integer(), nullable=False),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "environment_id",
            "artifact_id",
            name="pk_transcription_artifacts",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "processing_job_id",
            name="uq_transcription_artifacts_job",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "processing_job_id"],
            [
                f"{SCHEMA}.transcription_jobs.workspace_id",
                f"{SCHEMA}.transcription_jobs.environment_id",
                f"{SCHEMA}.transcription_jobs.job_id",
            ],
            name="fk_transcription_artifacts_job",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "source_revision_id"],
            [
                f"{SCHEMA}.source_revisions.workspace_id",
                f"{SCHEMA}.source_revisions.environment_id",
                f"{SCHEMA}.source_revisions.source_object_id",
                f"{SCHEMA}.source_revisions.revision_id",
            ],
            name="fk_transcription_artifacts_source_revision",
        ),
        sa.CheckConstraint(
            "recording_duration_ms > 0",
            name=op.f("ck_transcription_artifacts_duration_positive"),
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "transcript_segments",
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("segment_id", uuid, nullable=False),
        sa.Column("artifact_id", uuid, nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("anonymous_speaker_label", sa.Text(), nullable=True),
        sa.Column("signals", postgresql.JSONB(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "environment_id",
            "segment_id",
            name="pk_transcript_segments",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "artifact_id",
            "ordinal",
            name="uq_transcript_segments_artifact_ordinal",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "artifact_id"],
            [
                f"{SCHEMA}.transcription_artifacts.workspace_id",
                f"{SCHEMA}.transcription_artifacts.environment_id",
                f"{SCHEMA}.transcription_artifacts.artifact_id",
            ],
            name="fk_transcript_segments_artifact",
        ),
        sa.CheckConstraint("ordinal > 0", name=op.f("ck_transcript_segments_ordinal_positive")),
        sa.CheckConstraint("start_ms >= 0", name=op.f("ck_transcript_segments_start_nonnegative")),
        sa.CheckConstraint("end_ms > start_ms", name=op.f("ck_transcript_segments_bounds")),
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_transcription_jobs_artifact",
        "transcription_jobs",
        "transcription_artifacts",
        ["workspace_id", "environment_id", "artifact_id"],
        ["workspace_id", "environment_id", "artifact_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_index(
        "ix_transcription_jobs_source_revision",
        "transcription_jobs",
        ["workspace_id", "environment_id", "source_revision_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_transcript_segments_artifact",
        "transcript_segments",
        ["workspace_id", "environment_id", "artifact_id", "ordinal"],
        schema=SCHEMA,
    )
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.enforce_transcription_source_lineage()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            canonical_revision_digest text;
            canonical_original_sha256 text;
        BEGIN
            SELECT revision_digest, original_sha256
              INTO canonical_revision_digest, canonical_original_sha256
              FROM {SCHEMA}.source_revisions
             WHERE workspace_id = NEW.workspace_id
               AND environment_id = NEW.environment_id
               AND source_object_id = NEW.source_object_id
               AND revision_id = NEW.source_revision_id;
            IF NOT FOUND
               OR canonical_revision_digest IS DISTINCT FROM NEW.source_revision_digest
               OR canonical_original_sha256 IS DISTINCT FROM NEW.original_sha256 THEN
                RAISE EXCEPTION 'transcription source lineage does not match canonical revision'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_transcription_source_lineage';
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
    for table in ("transcription_jobs", "transcription_artifacts"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_source_lineage
            BEFORE INSERT ON {SCHEMA}.{table}
            FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.enforce_transcription_source_lineage()
            """
        )
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.enforce_transcription_job_transition()
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
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_transcription_jobs_terminal_immutable
        BEFORE UPDATE ON {SCHEMA}.transcription_jobs
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.enforce_transcription_job_transition()
        """
    )
    _tenant_scope("transcription_jobs", mutable=True)
    _tenant_scope("transcription_artifacts")
    _tenant_scope("transcript_segments")


def _tenant_scope(table: str, *, mutable: bool = False) -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    qualified = f'{SCHEMA}."{table}"'
    ws_setting = "pg_catalog.current_setting('spine.workspace_id', true)"
    env_setting = "pg_catalog.current_setting('spine.environment_id', true)"
    pattern = "^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
    ws = f"CASE WHEN {ws_setting} OPERATOR(pg_catalog.~) '{pattern}' THEN CAST({ws_setting} AS pg_catalog.uuid) ELSE NULL END"
    env = f"CASE WHEN {env_setting} OPERATOR(pg_catalog.~) '{pattern}' THEN CAST({env_setting} AS pg_catalog.uuid) ELSE NULL END"
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(
        f'CREATE POLICY "pol_{table}_tenant_isolation" ON {qualified} FOR ALL TO {runtime} '
        f"USING (workspace_id = ({ws}) AND environment_id = ({env})) "
        f"WITH CHECK (workspace_id = ({ws}) AND environment_id = ({env}))"
    )
    op.execute(
        f'CREATE POLICY "pol_{table}_migration_maintenance" ON {qualified} FOR ALL TO {migration} '
        "USING (true) WITH CHECK (true)"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE {qualified} TO {runtime}")
    op.execute(
        f"REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER "
        f"ON TABLE {qualified} FROM {runtime}"
    )
    if mutable:
        op.execute(
            f"GRANT UPDATE (state, artifact_id, terminal_code, retryable, completed_at, "
            f"execution_lease_id, execution_lease_expires_at, execution_attempt) "
            f"ON TABLE {qualified} TO {runtime}"
        )


def downgrade() -> None:
    for table in ("transcription_jobs", "transcription_artifacts"):
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_{table}_source_lineage ON {SCHEMA}.{table}"
        )
    for table in ("transcript_segments", "transcription_artifacts", "transcription_jobs"):
        qualified = f'{SCHEMA}."{table}"'
        op.execute(f'DROP POLICY IF EXISTS "pol_{table}_tenant_isolation" ON {qualified}')
        op.execute(f'DROP POLICY IF EXISTS "pol_{table}_migration_maintenance" ON {qualified}')
    op.drop_index(
        "ix_transcript_segments_artifact", table_name="transcript_segments", schema=SCHEMA
    )
    op.drop_index(
        "ix_transcription_jobs_source_revision", table_name="transcription_jobs", schema=SCHEMA
    )
    op.drop_constraint(
        "fk_transcription_jobs_artifact",
        "transcription_jobs",
        type_="foreignkey",
        schema=SCHEMA,
    )
    op.execute(
        f"DROP TRIGGER IF EXISTS trg_transcription_jobs_terminal_immutable "
        f"ON {SCHEMA}.transcription_jobs"
    )
    op.execute(
        f"DROP FUNCTION IF EXISTS {SCHEMA}.enforce_transcription_job_transition()"
    )
    op.execute(
        f"DROP FUNCTION IF EXISTS {SCHEMA}.enforce_transcription_source_lineage()"
    )
    op.drop_table("transcript_segments", schema=SCHEMA)
    op.drop_table("transcription_artifacts", schema=SCHEMA)
    op.drop_table("transcription_jobs", schema=SCHEMA)
