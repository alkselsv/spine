"""Permit one immutable Q&A cancellation intent on admitted runs.

Revision ID: 20261108_45
Revises: 20261107_44
"""

from __future__ import annotations

from alembic import op


revision = "20261108_45"
down_revision = "20261107_44"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _replace_run_guard(*, allow_cancellation_intent: bool) -> None:
    intent_change = (
        """
        OR (
            OLD.payload->'cancellation_requested_at' IS DISTINCT FROM
                NEW.payload->'cancellation_requested_at'
            AND (
                OLD.state = 'completed'
                OR OLD.payload->'cancellation_requested_at' IS DISTINCT FROM 'null'::jsonb
                OR NEW.payload->'cancellation_requested_at' IS NOT DISTINCT FROM 'null'::jsonb
            )
        )
        """
        if allow_cancellation_intent
        else ""
    )
    intent_transition = (
        """
                OR (
                    OLD.state = NEW.state
                    AND OLD.state <> 'completed'
                    AND OLD.payload->'cancellation_requested_at' IS NOT DISTINCT FROM 'null'::jsonb
                    AND NEW.payload->'cancellation_requested_at' IS DISTINCT FROM 'null'::jsonb
                )
        """
        if allow_cancellation_intent
        else ""
    )
    mutable_payload_fields = (
        "'output_artifact_ids', 'state', 'outcome', 'final_attempt_id', 'completed_at', "
        "'cancellation_requested_at'"
        if allow_cancellation_intent
        else "'output_artifact_ids', 'state', 'outcome', 'final_attempt_id', 'completed_at'"
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.guard_answer_question_run_update() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ BEGIN
            IF NOT (
                   (OLD.state = 'admitted' AND NEW.state = 'dispatch_pending')
                OR (OLD.state = 'admitted' AND NEW.state = 'running')
                OR (OLD.state = 'dispatch_pending' AND NEW.state = 'running')
                OR (OLD.state = 'running' AND NEW.state = 'completed')
                {intent_transition}
               )
               OR (OLD.workspace_id, OLD.environment_id, OLD.run_id, OLD.idempotency_key,
                   OLD.command_digest, OLD.created_at)
                  IS DISTINCT FROM
                  (NEW.workspace_id, NEW.environment_id, NEW.run_id, NEW.idempotency_key,
                   NEW.command_digest, NEW.created_at)
               OR (OLD.payload - ARRAY[{mutable_payload_fields}]::text[])
                  IS DISTINCT FROM
                  (NEW.payload - ARRAY[{mutable_payload_fields}]::text[])
               {intent_change} THEN
                RAISE EXCEPTION 'Q&A run history is immutable.'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_answer_question_runs_immutable';
            END IF;
            RETURN NEW;
        END; $function$
        """
    )


def upgrade() -> None:
    _replace_run_guard(allow_cancellation_intent=True)


def downgrade() -> None:
    _replace_run_guard(allow_cancellation_intent=False)
