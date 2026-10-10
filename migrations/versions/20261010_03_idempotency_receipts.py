"""Persist tenant-scoped idempotency receipts.

Revision ID: 20261010_03
Revises: 20261009_02
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261010_03"
down_revision = "20261009_02"
branch_labels = None
depends_on = None

SCHEMA = "spine"
TABLE = "idempotency_receipts"
_UUID_PATTERN = (
    "^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    "[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)


def _quoted_role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    role = getattr(settings, attribute)
    return op.get_bind().dialect.identifier_preparer.quote(role)


def _setting_uuid(name: str) -> str:
    setting = f"pg_catalog.current_setting('spine.{name}', true)"
    return (
        f"CASE WHEN {setting} OPERATOR(pg_catalog.~) '{_UUID_PATTERN}' "
        f"THEN CAST({setting} AS pg_catalog.uuid) ELSE NULL END"
    )


def upgrade() -> None:
    runtime_role = _quoted_role("runtime_role")
    migration_role = _quoted_role("migration_role")

    op.create_table(
        TABLE,
        sa.Column("receipt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("operation_name", sa.Text(), nullable=False),
        sa.Column("operation_schema_version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("digest_algorithm_version", sa.Text(), nullable=False),
        sa.Column("command_digest", sa.String(length=64), nullable=False),
        sa.Column("result_type", sa.Text(), nullable=True),
        sa.Column("result_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("result_schema_version", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("receipt_id", name="pk_idempotency_receipts"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            [f"{SCHEMA}.workspaces.id"],
            name="fk_idempotency_receipts_workspace_id_workspaces",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_idempotency_receipts_scope_environments",
        ),
        sa.CheckConstraint(
            "operation_schema_version > 0",
            name=op.f(
                "ck_idempotency_receipts_operation_schema_version_positive"
            ),
        ),
        sa.CheckConstraint(
            "char_length(idempotency_key) BETWEEN 1 AND 255",
            name=op.f("ck_idempotency_receipts_idempotency_key_length"),
        ),
        sa.CheckConstraint(
            "command_digest OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$'",
            name=op.f("ck_idempotency_receipts_command_digest_sha256"),
        ),
        sa.CheckConstraint(
            "(result_type IS NULL AND result_id IS NULL "
            "AND result_schema_version IS NULL) OR "
            "(result_type IS NOT NULL AND result_id IS NOT NULL "
            "AND result_schema_version > 0)",
            name=op.f("ck_idempotency_receipts_result_complete"),
        ),
        schema=SCHEMA,
    )
    uniqueness_columns = [
        "workspace_id",
        "operation_name",
        "operation_schema_version",
        "idempotency_key",
    ]
    op.create_index(
        "uq_idempotency_receipts_workspace_key",
        TABLE,
        uniqueness_columns,
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("environment_id IS NULL"),
    )
    op.create_index(
        "uq_idempotency_receipts_environment_key",
        TABLE,
        ["workspace_id", "environment_id", *uniqueness_columns[1:]],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("environment_id IS NOT NULL"),
    )

    workspace = _setting_uuid("workspace_id")
    environment = _setting_uuid("environment_id")
    tenant_expression = (
        f"workspace_id = ({workspace}) AND "
        f"((environment_id IS NULL AND ({environment}) IS NULL) OR "
        f"environment_id = ({environment}))"
    )
    qualified_table = f'{SCHEMA}."{TABLE}"'
    op.execute(f"ALTER TABLE {qualified_table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified_table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f'CREATE POLICY "pol_{TABLE}_tenant_isolation" '
        f"ON {qualified_table} FOR ALL TO {runtime_role} "
        f"USING ({tenant_expression}) WITH CHECK ({tenant_expression})"
    )
    op.execute(
        f'CREATE POLICY "pol_{TABLE}_migration_maintenance" '
        f"ON {qualified_table} FOR ALL TO {migration_role} "
        "USING (true) WITH CHECK (true)"
    )
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.enforce_idempotency_receipt_completion()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        BEGIN
            IF OLD.result_type IS NOT NULL
               OR OLD.result_id IS NOT NULL
               OR OLD.result_schema_version IS NOT NULL
               OR NEW.result_type IS NULL
               OR NEW.result_id IS NULL
               OR NEW.result_schema_version IS NULL
               OR ROW(
                    NEW.receipt_id,
                    NEW.workspace_id,
                    NEW.environment_id,
                    NEW.operation_name,
                    NEW.operation_schema_version,
                    NEW.idempotency_key,
                    NEW.digest_algorithm_version,
                    NEW.command_digest,
                    NEW.created_at
               ) IS DISTINCT FROM ROW(
                    OLD.receipt_id,
                    OLD.workspace_id,
                    OLD.environment_id,
                    OLD.operation_name,
                    OLD.operation_schema_version,
                    OLD.idempotency_key,
                    OLD.digest_algorithm_version,
                    OLD.command_digest,
                    OLD.created_at
               )
            THEN
                RAISE EXCEPTION 'Idempotency receipt completion is immutable.'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_idempotency_receipts_single_completion';
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(
        f"CREATE TRIGGER trg_idempotency_receipts_single_completion "
        f"BEFORE UPDATE ON {qualified_table} FOR EACH ROW "
        f"EXECUTE FUNCTION {SCHEMA}.enforce_idempotency_receipt_completion()"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION "
        f"{SCHEMA}.enforce_idempotency_receipt_completion() FROM PUBLIC"
    )
    op.execute(
        f"GRANT SELECT, INSERT ON TABLE {qualified_table} TO {runtime_role}"
    )
    op.execute(
        f"GRANT UPDATE (result_type, result_id, result_schema_version) "
        f"ON TABLE {qualified_table} TO {runtime_role}"
    )


def downgrade() -> None:
    runtime_role = _quoted_role("runtime_role")
    qualified_table = f'{SCHEMA}."{TABLE}"'
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {qualified_table} FROM {runtime_role}")
    op.drop_table(TABLE, schema=SCHEMA)
    op.execute(
        f"DROP FUNCTION {SCHEMA}.enforce_idempotency_receipt_completion()"
    )
