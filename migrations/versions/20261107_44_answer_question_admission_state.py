"""Add durable pre-dispatch states to answer-question runs.

Revision ID: 20261107_44
Revises: 20261106_43
"""

from __future__ import annotations

from alembic import op
from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261107_44"
down_revision = "20261106_43"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _runtime_role() -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(settings.runtime_role)


def _replace_constraints(*, state: str, terminal: str) -> None:
    op.drop_constraint(
        op.f("ck_answer_question_runs_terminal"),
        "answer_question_runs",
        schema=SCHEMA,
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_answer_question_runs_state"),
        "answer_question_runs",
        schema=SCHEMA,
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_answer_question_runs_state"),
        "answer_question_runs",
        state,
        schema=SCHEMA,
    )
    op.create_check_constraint(
        op.f("ck_answer_question_runs_terminal"),
        "answer_question_runs",
        terminal,
        schema=SCHEMA,
    )


def _replace_run_guard(body: str) -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.guard_answer_question_run_update() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ BEGIN
            {body}
        END; $function$
        """
    )


def upgrade() -> None:
    # SELECT ... FOR UPDATE on the session head also requires UPDATE privilege
    # for the runtime role in PostgreSQL.
    op.execute(
        f'GRANT UPDATE ON TABLE {SCHEMA}."qa_session_versions" TO '
        f"{_runtime_role()}"
    )
    _replace_constraints(
        state="state IN ('admitted', 'dispatch_pending', 'running', 'completed')",
        terminal=(
            "(state IN ('admitted', 'dispatch_pending', 'running') AND "
            "outcome IS NULL AND completed_at IS NULL) OR "
            "(state = 'completed' AND outcome IS NOT NULL AND completed_at IS NOT NULL)"
        ),
    )
    _replace_run_guard(
        """
            IF NOT (
                   (OLD.state = 'admitted' AND NEW.state = 'dispatch_pending')
                OR (OLD.state = 'dispatch_pending' AND NEW.state = 'running')
                OR (OLD.state = 'running' AND NEW.state = 'completed')
               )
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
        """
    )


def downgrade() -> None:
    op.execute(
        f'REVOKE UPDATE ON TABLE {SCHEMA}."qa_session_versions" FROM '
        f"{_runtime_role()}"
    )
    _replace_constraints(
        state="state IN ('running', 'completed')",
        terminal=(
            "(state = 'running' AND outcome IS NULL AND completed_at IS NULL) OR "
            "(state = 'completed' AND outcome IS NOT NULL AND completed_at IS NOT NULL)"
        ),
    )
    _replace_run_guard(
        """
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
        """
    )
