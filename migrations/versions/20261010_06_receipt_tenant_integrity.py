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
VALIDATOR = "validate_idempotency_receipt_tenant"
SCOPE_GUARD = "enforce_idempotency_receipt_scope_immutable"
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
        BEGIN
            IF NEW.workspace_id IS NULL
               OR NEW.environment_id IS NULL
               OR NEW.idempotency_receipt_id IS NULL
               OR NOT EXISTS (
                   SELECT 1
                   FROM {SCHEMA}."{RECEIPT_TABLE}" AS receipt
                   WHERE receipt.receipt_id = NEW.idempotency_receipt_id
                     AND receipt.workspace_id = NEW.workspace_id
                     AND receipt.environment_id = NEW.environment_id
               )
            THEN
                RAISE EXCEPTION 'Idempotency receipt tenant validation failed.'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_idempotency_receipt_tenant_match';
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
            IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
               OR NEW.environment_id IS DISTINCT FROM OLD.environment_id
            THEN
                RAISE EXCEPTION 'Idempotency receipt tenant scope is immutable.'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_idempotency_receipts_scope_immutable';
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(
        f"COMMENT ON FUNCTION {SCHEMA}.{SCOPE_GUARD}() IS "
        "'Prevents relabeling an idempotency receipt after insertion.'"
    )
    op.execute(
        f"CREATE TRIGGER {SCOPE_TRIGGER} "
        f"BEFORE UPDATE ON {qualified_receipts} FOR EACH ROW "
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
