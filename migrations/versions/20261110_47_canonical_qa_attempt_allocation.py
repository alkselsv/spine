"""Allocate canonical Q&A attempts by a retry-stable logical key.

Revision ID: 20261110_47
Revises: 20261109_46
"""

from __future__ import annotations

from alembic import op


revision = "20261110_47"
down_revision = "20261109_46"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _replace_attempt_guard(*, with_logical_key: bool) -> None:
    identity = (
        "OLD.workspace_id, OLD.environment_id, OLD.attempt_id, OLD.run_id, "
        "OLD.attempt_number, OLD.logical_execution_key, OLD.started_at"
        if with_logical_key
        else "OLD.workspace_id, OLD.environment_id, OLD.attempt_id, OLD.run_id, "
        "OLD.attempt_number, OLD.started_at"
    )
    new_identity = identity.replace("OLD.", "NEW.")
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.guard_answer_question_attempt_update()
        RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ BEGIN
            IF OLD.state <> 'running'
               OR NEW.state NOT IN ('succeeded', 'failed', 'cancelled')
               OR ({identity}) IS DISTINCT FROM ({new_identity})
               OR (OLD.payload - ARRAY['output_artifact_ids', 'context_bundle_ref',
                       'agent_result_ref', 'usage', 'events', 'state', 'outcome',
                       'reason', 'completed_at']::text[])
                  IS DISTINCT FROM
                  (NEW.payload - ARRAY['output_artifact_ids', 'context_bundle_ref',
                       'agent_result_ref', 'usage', 'events', 'state', 'outcome',
                       'reason', 'completed_at']::text[]) THEN
                RAISE EXCEPTION 'Q&A attempt history is immutable.'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_answer_question_attempts_immutable';
            END IF;
            RETURN NEW;
        END; $function$
        """
    )


def upgrade() -> None:
    # ``IF NOT EXISTS`` also recovers safely from a transactional-DDL runner
    # that lost its version-table commit after the additive column became
    # visible. The constraints below still establish the complete contract.
    op.execute(
        f"ALTER TABLE {SCHEMA}.answer_question_attempts "
        "ADD COLUMN IF NOT EXISTS logical_execution_key TEXT"
    )
    op.execute(
        f"""
        DO $block$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_catalog.pg_constraint
                WHERE conname = 'ck_answer_question_attempts_logical_key'
                  AND conrelid = '{SCHEMA}.answer_question_attempts'::regclass
            ) THEN
                ALTER TABLE {SCHEMA}.answer_question_attempts
                ADD CONSTRAINT ck_answer_question_attempts_logical_key CHECK (
                    logical_execution_key IS NULL OR (
                        char_length(logical_execution_key) BETWEEN 1 AND 255
                        AND logical_execution_key ~ '^[A-Za-z0-9._:-]+$'
                    )
                );
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_catalog.pg_constraint
                WHERE conname = 'uq_answer_question_attempts_logical_key'
                  AND conrelid = '{SCHEMA}.answer_question_attempts'::regclass
            ) THEN
                ALTER TABLE {SCHEMA}.answer_question_attempts
                ADD CONSTRAINT uq_answer_question_attempts_logical_key UNIQUE
                    (workspace_id, environment_id, run_id, logical_execution_key);
            END IF;
        END $block$
        """
    )
    op.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS
            uq_answer_question_attempts_one_allocated_running
        ON {SCHEMA}.answer_question_attempts
            (workspace_id, environment_id, run_id)
        WHERE state = 'running' AND logical_execution_key IS NOT NULL
        """
    )
    _replace_attempt_guard(with_logical_key=True)


def downgrade() -> None:
    _replace_attempt_guard(with_logical_key=False)
    op.drop_index(
        "uq_answer_question_attempts_one_allocated_running",
        table_name="answer_question_attempts",
        schema=SCHEMA,
    )
    op.drop_constraint(
        "uq_answer_question_attempts_logical_key",
        "answer_question_attempts",
        type_="unique",
        schema=SCHEMA,
    )
    op.drop_constraint(
        op.f("ck_answer_question_attempts_logical_key"),
        "answer_question_attempts",
        type_="check",
        schema=SCHEMA,
    )
    op.drop_column("answer_question_attempts", "logical_execution_key", schema=SCHEMA)
