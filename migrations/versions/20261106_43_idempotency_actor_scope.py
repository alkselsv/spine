"""Bind idempotency receipts to the authenticated actor or service principal.

The column is nullable for backward compatibility with receipts created before
this migration. New claims include the trusted actor in their uniqueness scope.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20261106_43"
down_revision = "20261105_42"
branch_labels = None
depends_on = None
SCHEMA = "spine"
TABLE = "idempotency_receipts"
ZERO_UUID = "00000000-0000-0000-0000-000000000000"


def upgrade() -> None:
    qualified = f'{SCHEMA}."{TABLE}"'
    op.execute(
        f'ALTER TABLE {qualified} ADD COLUMN IF NOT EXISTS '
        "actor_id pg_catalog.uuid"
    )
    op.execute(
        f"DROP INDEX IF EXISTS {SCHEMA}.uq_idempotency_receipts_workspace_key"
    )
    op.execute(
        f"DROP INDEX IF EXISTS {SCHEMA}.uq_idempotency_receipts_environment_key"
    )
    op.execute(
        f"DROP TRIGGER IF EXISTS trg_idempotency_receipts_actor_immutable ON {qualified}"
    )
    op.execute(
        f"DROP FUNCTION IF EXISTS {SCHEMA}.enforce_idempotency_receipt_actor_immutable()"
    )
    op.execute(
        f"""
        CREATE UNIQUE INDEX uq_idempotency_receipts_workspace_key
          ON {qualified} (
              workspace_id,
              operation_name,
              operation_schema_version,
              idempotency_key,
              COALESCE(actor_id, '{ZERO_UUID}'::uuid)
          )
         WHERE environment_id IS NULL
        """
    )
    op.execute(
        f"""
        CREATE UNIQUE INDEX uq_idempotency_receipts_environment_key
          ON {qualified} (
              workspace_id,
              environment_id,
              operation_name,
              operation_schema_version,
              idempotency_key,
              COALESCE(actor_id, '{ZERO_UUID}'::uuid)
          )
         WHERE environment_id IS NOT NULL
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.enforce_idempotency_receipt_actor_immutable()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        BEGIN
            IF OLD.actor_id IS DISTINCT FROM NEW.actor_id THEN
                RAISE EXCEPTION 'Idempotency receipt actor scope is immutable.'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_idempotency_receipts_actor_immutable';
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_idempotency_receipts_actor_immutable
        BEFORE UPDATE OF actor_id ON {qualified}
        FOR EACH ROW
        EXECUTE FUNCTION {SCHEMA}.enforce_idempotency_receipt_actor_immutable()
        """
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.enforce_idempotency_receipt_actor_immutable() FROM PUBLIC"
    )


def downgrade() -> None:
    qualified = f'{SCHEMA}."{TABLE}"'
    op.execute(f"DROP TRIGGER trg_idempotency_receipts_actor_immutable ON {qualified}")
    op.execute(f"DROP FUNCTION {SCHEMA}.enforce_idempotency_receipt_actor_immutable()")
    op.execute(
        f"DROP INDEX IF EXISTS {SCHEMA}.uq_idempotency_receipts_workspace_key"
    )
    op.execute(
        f"DROP INDEX IF EXISTS {SCHEMA}.uq_idempotency_receipts_environment_key"
    )
    op.create_index(
        "uq_idempotency_receipts_workspace_key",
        TABLE,
        ["workspace_id", "operation_name", "operation_schema_version", "idempotency_key"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("environment_id IS NULL"),
    )
    op.create_index(
        "uq_idempotency_receipts_environment_key",
        TABLE,
        ["workspace_id", "environment_id", "operation_name", "operation_schema_version", "idempotency_key"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("environment_id IS NOT NULL"),
    )
    op.execute(f"ALTER TABLE {qualified} DROP COLUMN IF EXISTS actor_id")
