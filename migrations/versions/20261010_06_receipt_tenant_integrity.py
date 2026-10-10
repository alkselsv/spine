"""Enforce tenant integrity for future idempotency-receipt references.

Revision ID: 20261010_06
Revises: 20261010_05

K0 exposes a reusable deferred trigger function for Issue #7 child tables and
protects the receipt's immutable tenant scope. It intentionally creates no
Issue #7 child table or child trigger.
"""

from __future__ import annotations

from alembic import op


revision = "20261010_06"
down_revision = "20261010_05"
branch_labels = None
depends_on = None

SCHEMA = "spine"
RECEIPT_TABLE = "idempotency_receipts"
VALIDATOR = "validate_issue7_receipt_scope"
SCOPE_GUARD = "reject_idempotency_receipt_scope_update"
SCOPE_TRIGGER = "trg_idempotency_receipts_scope_immutable"


def upgrade() -> None:
    qualified_receipts = f'{SCHEMA}."{RECEIPT_TABLE}"'

    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.{VALIDATOR}()
        RETURNS trigger
        LANGUAGE plpgsql
        VOLATILE
        SECURITY INVOKER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE
            receipt_workspace uuid;
            receipt_environment uuid;
        BEGIN
            SELECT receipt.workspace_id, receipt.environment_id
              INTO receipt_workspace, receipt_environment
              FROM {SCHEMA}."{RECEIPT_TABLE}" AS receipt
             WHERE receipt.receipt_id = NEW.idempotency_receipt_id
             FOR KEY SHARE;

            IF NEW.workspace_id IS NULL
               OR NEW.environment_id IS NULL
               OR NEW.idempotency_receipt_id IS NULL
               OR NOT FOUND
               OR receipt_workspace IS DISTINCT FROM NEW.workspace_id
               OR receipt_environment IS DISTINCT FROM NEW.environment_id
            THEN
                RAISE EXCEPTION 'Receipt scope conflict.'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_issue7_receipt_scope';
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(
        f"COMMENT ON FUNCTION {SCHEMA}.{VALIDATOR}() IS "
        "'Deferred AFTER INSERT OR UPDATE row validator for environment-scoped child receipts.'"
    )
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.{SCOPE_GUARD}()
        RETURNS trigger
        LANGUAGE plpgsql
        VOLATILE
        SECURITY INVOKER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        BEGIN
            RAISE EXCEPTION 'Idempotency receipt scope is immutable.'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'ck_idempotency_receipt_scope_immutable';
        END;
        $function$
        """
    )
    op.execute(
        f"COMMENT ON FUNCTION {SCHEMA}.{SCOPE_GUARD}() IS "
        "'Rejects every update that targets an idempotency receipt tenant scope column.'"
    )
    op.execute(
        f"CREATE TRIGGER {SCOPE_TRIGGER} "
        f"BEFORE UPDATE OF workspace_id, environment_id ON {qualified_receipts} "
        f"FOR EACH ROW "
        f"EXECUTE FUNCTION {SCHEMA}.{SCOPE_GUARD}()"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.{VALIDATOR}() FROM PUBLIC"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.{SCOPE_GUARD}() FROM PUBLIC"
    )


def downgrade() -> None:
    qualified_receipts = f'{SCHEMA}."{RECEIPT_TABLE}"'
    op.execute(f"DROP TRIGGER {SCOPE_TRIGGER} ON {qualified_receipts}")
    op.execute(f"DROP FUNCTION {SCHEMA}.{SCOPE_GUARD}()")
    op.execute(f"DROP FUNCTION {SCHEMA}.{VALIDATOR}()")
