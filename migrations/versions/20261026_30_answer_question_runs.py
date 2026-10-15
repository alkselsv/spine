"""Persist durable answer_question runs and immutable terminal attempts.

Revision ID: 20261026_30
Revises: 20261025_29
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261026_30"
down_revision = "20261025_29"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def _tenant(table: str) -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    qualified = f'{SCHEMA}."{table}"'
    scope = (
        "workspace_id = (CASE WHEN pg_catalog.current_setting('spine.workspace_id', true) "
        "OPERATOR(pg_catalog.~) '^[0-9A-Fa-f-]{36}$' THEN "
        "CAST(pg_catalog.current_setting('spine.workspace_id', true) AS pg_catalog.uuid) ELSE NULL END) "
        "AND environment_id = (CASE WHEN pg_catalog.current_setting('spine.environment_id', true) "
        "OPERATOR(pg_catalog.~) '^[0-9A-Fa-f-]{36}$' THEN "
        "CAST(pg_catalog.current_setting('spine.environment_id', true) AS pg_catalog.uuid) ELSE NULL END)"
    )
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(
        f'CREATE POLICY "pol_{table}_tenant_isolation" ON {qualified} '
        f"FOR ALL TO {runtime} USING ({scope}) WITH CHECK ({scope})"
    )
    op.execute(
        f'CREATE POLICY "pol_{table}_migration_maintenance" ON {qualified} '
        f"FOR ALL TO {migration} USING (true) WITH CHECK (true)"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE {qualified} TO {runtime}")
    op.execute(f"REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {qualified} FROM {runtime}")


def _immutability() -> None:
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.guard_answer_question_run_update() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ BEGIN
            IF OLD.state <> 'running'
               OR NEW.state <> 'completed'
               OR (OLD.workspace_id, OLD.environment_id, OLD.run_id, OLD.idempotency_key,
                   OLD.command_digest, OLD.created_at)
                  IS DISTINCT FROM
                  (NEW.workspace_id, NEW.environment_id, NEW.run_id, NEW.idempotency_key,
                   NEW.command_digest, NEW.created_at)
               OR (OLD.payload - ARRAY['output_artifact_ids', 'state', 'outcome',
                       'final_attempt_id', 'completed_at']::text[])
                  IS DISTINCT FROM
                  (NEW.payload - ARRAY['output_artifact_ids', 'state', 'outcome',
                       'final_attempt_id', 'completed_at']::text[]) THEN
                RAISE EXCEPTION 'Q&A run history is immutable.'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_answer_question_runs_immutable';
            END IF;
            RETURN NEW;
        END; $function$
        """
    )
    op.execute(
        f"CREATE TRIGGER trg_answer_question_runs_immutable BEFORE UPDATE ON "
        f'{SCHEMA}."answer_question_runs" FOR EACH ROW EXECUTE FUNCTION '
        f"{SCHEMA}.guard_answer_question_run_update()"
    )
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.guard_answer_question_attempt_update() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ BEGIN
            IF OLD.state <> 'running'
               OR NEW.state NOT IN ('succeeded', 'failed', 'cancelled')
               OR (OLD.workspace_id, OLD.environment_id, OLD.attempt_id, OLD.run_id,
                   OLD.attempt_number, OLD.started_at)
                  IS DISTINCT FROM
                  (NEW.workspace_id, NEW.environment_id, NEW.attempt_id, NEW.run_id,
                   NEW.attempt_number, NEW.started_at)
               OR (OLD.payload - ARRAY['output_artifact_ids', 'context_bundle_ref',
                       'agent_result_ref', 'usage', 'events', 'state', 'outcome',
                       'reason', 'completed_at']::text[])
                  IS DISTINCT FROM
                  (NEW.payload - ARRAY['output_artifact_ids', 'context_bundle_ref',
                       'agent_result_ref', 'usage', 'events', 'state', 'outcome',
                       'reason', 'completed_at']::text[]) THEN
                RAISE EXCEPTION 'Q&A attempt history is immutable.'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_answer_question_attempts_immutable';
            END IF;
            RETURN NEW;
        END; $function$
        """
    )
    op.execute(
        f"CREATE TRIGGER trg_answer_question_attempts_immutable BEFORE UPDATE ON "
        f'{SCHEMA}."answer_question_attempts" FOR EACH ROW EXECUTE FUNCTION '
        f"{SCHEMA}.guard_answer_question_attempt_update()"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.guard_answer_question_run_update() FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.guard_answer_question_attempt_update() FROM PUBLIC")


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    outcome = "outcome IS NULL OR outcome IN ('answered', 'clarification_required', 'abstained', 'denied', 'failed', 'cancelled')"
    op.create_unique_constraint(
        "uq_context_bundles_exact_identity",
        "context_bundles",
        [
            "workspace_id",
            "environment_id",
            "bundle_id",
            "bundle_version",
            "bundle_digest",
        ],
        schema=SCHEMA,
    )
    op.create_table(
        "answer_question_runs",
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("command_digest", sa.String(64), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "environment_id", "run_id", name="pk_answer_question_runs"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_answer_question_runs_environment",
        ),
        sa.UniqueConstraint(
            "workspace_id", "environment_id", "idempotency_key",
            name="uq_answer_question_runs_idempotency",
        ),
        sa.CheckConstraint("command_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_answer_question_runs_digest")),
        sa.CheckConstraint("state IN ('running', 'completed')", name=op.f("ck_answer_question_runs_state")),
        sa.CheckConstraint(outcome, name=op.f("ck_answer_question_runs_outcome")),
        sa.CheckConstraint(
            "(state = 'running' AND outcome IS NULL AND completed_at IS NULL) OR "
            "(state = 'completed' AND outcome IS NOT NULL AND completed_at IS NOT NULL)",
            name=op.f("ck_answer_question_runs_terminal"),
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "answer_question_attempts",
        sa.Column("attempt_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("context_bundle_id", uuid, nullable=True),
        sa.Column("context_bundle_version", sa.Integer(), nullable=True),
        sa.Column("context_bundle_digest", sa.String(64), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "environment_id", "attempt_id", name="pk_answer_question_attempts"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "run_id"],
            [f"{SCHEMA}.answer_question_runs.workspace_id", f"{SCHEMA}.answer_question_runs.environment_id", f"{SCHEMA}.answer_question_runs.run_id"],
            name="fk_answer_question_attempts_run",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "context_bundle_id", "context_bundle_version", "context_bundle_digest"],
            [f"{SCHEMA}.context_bundles.workspace_id", f"{SCHEMA}.context_bundles.environment_id", f"{SCHEMA}.context_bundles.bundle_id", f"{SCHEMA}.context_bundles.bundle_version", f"{SCHEMA}.context_bundles.bundle_digest"],
            name="fk_answer_question_attempts_context_bundle",
        ),
        sa.UniqueConstraint(
            "workspace_id", "environment_id", "run_id", "attempt_number",
            name="uq_answer_question_attempts_number",
        ),
        sa.CheckConstraint("attempt_number > 0", name=op.f("ck_answer_question_attempts_positive")),
        sa.CheckConstraint("state IN ('running', 'succeeded', 'failed', 'cancelled')", name=op.f("ck_answer_question_attempts_state")),
        sa.CheckConstraint(outcome, name=op.f("ck_answer_question_attempts_outcome")),
        sa.CheckConstraint(
            "(state = 'running' AND outcome IS NULL AND completed_at IS NULL) OR "
            "(state <> 'running' AND outcome IS NOT NULL AND completed_at IS NOT NULL)",
            name=op.f("ck_answer_question_attempts_terminal"),
        ),
        sa.CheckConstraint(
            "(context_bundle_id IS NULL AND context_bundle_version IS NULL AND context_bundle_digest IS NULL) OR "
            "(context_bundle_id IS NOT NULL AND context_bundle_version > 0 AND context_bundle_digest ~ '^[0-9a-f]{64}$')",
            name=op.f("ck_answer_question_attempts_bundle_ref"),
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_answer_question_attempts_run", "answer_question_attempts",
        ["workspace_id", "environment_id", "run_id", "attempt_number"], schema=SCHEMA,
    )
    _tenant("answer_question_runs")
    _tenant("answer_question_attempts")
    _immutability()


def downgrade() -> None:
    runtime = _role("runtime_role")
    for table in ("answer_question_attempts", "answer_question_runs"):
        op.execute(f'REVOKE ALL PRIVILEGES ON TABLE {SCHEMA}."{table}" FROM {runtime}')
    op.drop_index(
        "ix_answer_question_attempts_run", table_name="answer_question_attempts", schema=SCHEMA
    )
    op.drop_table("answer_question_attempts", schema=SCHEMA)
    op.drop_table("answer_question_runs", schema=SCHEMA)
    op.drop_constraint(
        "uq_context_bundles_exact_identity",
        "context_bundles",
        type_="unique",
        schema=SCHEMA,
    )
    op.execute(f"DROP FUNCTION {SCHEMA}.guard_answer_question_attempt_update()")
    op.execute(f"DROP FUNCTION {SCHEMA}.guard_answer_question_run_update()")
