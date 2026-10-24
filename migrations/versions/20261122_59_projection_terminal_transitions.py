"""Allow terminal projection lifecycle transitions after candidate sealing."""

from __future__ import annotations

from alembic import op


revision = "20261122_59"
down_revision = "20261121_58"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.guard_projection_transition_after_seal() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE candidate_state text;
        BEGIN
            SELECT run.candidate_state
              INTO candidate_state
              FROM {SCHEMA}.projection_runs AS run
             WHERE run.workspace_id = NEW.workspace_id
               AND run.environment_id = NEW.environment_id
               AND run.projection_run_id = NEW.projection_run_id
               AND run.projection_candidate_id = NEW.projection_candidate_id
             FOR KEY SHARE;
            IF candidate_state IS DISTINCT FROM 'open'
               AND NEW.state NOT IN ('complete', 'failed')
            THEN
                RAISE EXCEPTION 'Projection candidate evidence is sealed.'
                    USING ERRCODE = '40001', CONSTRAINT = 'ck_projection_candidate_open';
            END IF;
            RETURN NEW;
        END; $function$
        """
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION "
        f"{SCHEMA}.guard_projection_transition_after_seal() FROM PUBLIC"
    )
    op.execute(
        f"DROP TRIGGER trg_projection_run_transitions_candidate_open "
        f"ON {SCHEMA}.projection_run_transitions"
    )
    op.execute(
        f"CREATE TRIGGER trg_projection_run_transitions_candidate_open "
        f"BEFORE INSERT ON {SCHEMA}.projection_run_transitions FOR EACH ROW "
        f"EXECUTE FUNCTION {SCHEMA}.guard_projection_transition_after_seal()"
    )


def downgrade() -> None:
    op.execute(
        f"DROP TRIGGER trg_projection_run_transitions_candidate_open "
        f"ON {SCHEMA}.projection_run_transitions"
    )
    op.execute(
        f"CREATE TRIGGER trg_projection_run_transitions_candidate_open "
        f"BEFORE INSERT ON {SCHEMA}.projection_run_transitions FOR EACH ROW "
        f"EXECUTE FUNCTION {SCHEMA}.reject_projection_evidence_after_seal()"
    )
    op.execute(
        f"DROP FUNCTION {SCHEMA}.guard_projection_transition_after_seal()"
    )
