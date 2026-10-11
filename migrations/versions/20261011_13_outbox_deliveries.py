"""Add lease-protected operational outbox delivery state.

Revision ID: 20261011_13
Revises: 20261010_12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261011_13"
down_revision = "20261010_12"
branch_labels = None
depends_on = None

SCHEMA = "spine"
TABLE = "outbox_deliveries"
INTENTS = "outbox_intents"
_UUID_PATTERN = (
    "^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    "[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)
_IDENTIFIER_PATTERN = "^[a-z][a-z0-9_.:-]{0,127}$"


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
    dispatcher_role = _quoted_role("dispatcher_role")
    migration_role = _quoted_role("migration_role")

    op.create_table(
        TABLE,
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("consumer_name", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_outbox_deliveries"),
        sa.ForeignKeyConstraint(
            ["event_id"],
            [f"{SCHEMA}.{INTENTS}.event_id"],
            name="fk_outbox_deliveries_event_id_outbox_intents",
        ),
        sa.CheckConstraint(
            "consumer_name IS NULL OR "
            f"consumer_name OPERATOR(pg_catalog.~) '{_IDENTIFIER_PATTERN}'",
            name=op.f("ck_outbox_deliveries_consumer_identifier"),
        ),
        sa.CheckConstraint(
            "lease_owner IS NULL OR "
            f"lease_owner OPERATOR(pg_catalog.~) '{_IDENTIFIER_PATTERN}'",
            name=op.f("ck_outbox_deliveries_lease_owner_identifier"),
        ),
        sa.CheckConstraint(
            "last_error_code IS NULL OR "
            f"last_error_code OPERATOR(pg_catalog.~) '{_IDENTIFIER_PATTERN}'",
            name=op.f("ck_outbox_deliveries_error_code_identifier"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_outbox_deliveries_attempt_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "version >= 0",
            name=op.f("ck_outbox_deliveries_version_nonnegative"),
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND attempt_count = 0 "
            "AND next_attempt_at IS NOT NULL "
            "AND lease_owner IS NULL AND lease_token IS NULL "
            "AND lease_expires_at IS NULL AND last_error_code IS NULL "
            "AND delivered_at IS NULL AND quarantined_at IS NULL) OR "
            "(status = 'in_flight' AND attempt_count > 0 "
            "AND next_attempt_at IS NULL "
            "AND lease_owner IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL "
            "AND delivered_at IS NULL AND quarantined_at IS NULL) OR "
            "(status = 'retry_scheduled' AND attempt_count > 0 "
            "AND next_attempt_at IS NOT NULL "
            "AND lease_owner IS NULL AND lease_token IS NULL "
            "AND lease_expires_at IS NULL AND last_error_code IS NOT NULL "
            "AND delivered_at IS NULL AND quarantined_at IS NULL) OR "
            "(status = 'delivered' AND attempt_count > 0 "
            "AND next_attempt_at IS NULL "
            "AND lease_owner IS NULL AND lease_token IS NULL "
            "AND lease_expires_at IS NULL AND last_error_code IS NULL "
            "AND delivered_at IS NOT NULL AND quarantined_at IS NULL) OR "
            "(status = 'quarantined' AND attempt_count > 0 "
            "AND next_attempt_at IS NULL "
            "AND lease_owner IS NULL AND lease_token IS NULL "
            "AND lease_expires_at IS NULL AND last_error_code IS NOT NULL "
            "AND delivered_at IS NULL AND quarantined_at IS NOT NULL)",
            name=op.f("ck_outbox_deliveries_state_complete"),
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_outbox_deliveries_due",
        TABLE,
        ["status", "next_attempt_at", "lease_expires_at", "event_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_outbox_deliveries_tenant_status",
        TABLE,
        ["workspace_id", "environment_id", "status"],
        schema=SCHEMA,
    )

    qualified = f'{SCHEMA}."{TABLE}"'
    intents = f'{SCHEMA}."{INTENTS}"'
    workspace = _setting_uuid("workspace_id")
    environment = _setting_uuid("environment_id")
    tenant_expression = (
        f"workspace_id = ({workspace}) AND "
        f"((environment_id IS NULL AND ({environment}) IS NULL) OR "
        f"environment_id = ({environment}))"
    )
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(
        f'CREATE POLICY "pol_{TABLE}_tenant_read" ON {qualified} '
        f"FOR SELECT TO {runtime_role} USING ({tenant_expression})"
    )
    op.execute(
        f'CREATE POLICY "pol_{TABLE}_dispatcher" ON {qualified} '
        f"FOR ALL TO {dispatcher_role} USING (true) WITH CHECK (true)"
    )
    op.execute(
        f'CREATE POLICY "pol_{TABLE}_migration_maintenance" ON {qualified} '
        f"FOR ALL TO {migration_role} USING (true) WITH CHECK (true)"
    )
    op.execute(
        f'CREATE POLICY "pol_{INTENTS}_dispatcher_read" ON {intents} '
        f"FOR SELECT TO {dispatcher_role} USING (true)"
    )

    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.enforce_outbox_delivery_scope()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM {intents} AS intent
                WHERE intent.event_id = NEW.event_id
                  AND intent.workspace_id = NEW.workspace_id
                  AND intent.environment_id IS NOT DISTINCT FROM NEW.environment_id
            ) THEN
                RAISE EXCEPTION 'Outbox delivery scope is invalid.'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_outbox_deliveries_intent_scope';
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(
        f"CREATE TRIGGER trg_outbox_deliveries_scope "
        f"BEFORE INSERT OR UPDATE ON {qualified} FOR EACH ROW "
        f"EXECUTE FUNCTION {SCHEMA}.enforce_outbox_delivery_scope()"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.enforce_outbox_delivery_scope() "
        "FROM PUBLIC"
    )
    op.execute(f"GRANT SELECT ON TABLE {intents} TO {dispatcher_role}")
    op.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {dispatcher_role}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON TABLE {qualified} TO {dispatcher_role}"
    )
    op.execute(f"GRANT SELECT ON TABLE {qualified} TO {runtime_role}")
    op.execute(
        f"COMMENT ON TABLE {qualified} IS "
        "'classification=operational_delivery; mutable_control_state=true'"
    )


def downgrade() -> None:
    runtime_role = _quoted_role("runtime_role")
    dispatcher_role = _quoted_role("dispatcher_role")
    qualified = f'{SCHEMA}."{TABLE}"'
    intents = f'{SCHEMA}."{INTENTS}"'
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {qualified} FROM {runtime_role}")
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {qualified} FROM {dispatcher_role}")
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {intents} FROM {dispatcher_role}")
    op.execute(f"REVOKE USAGE ON SCHEMA {SCHEMA} FROM {dispatcher_role}")
    op.execute(f'DROP POLICY "pol_{INTENTS}_dispatcher_read" ON {intents}')
    op.drop_table(TABLE, schema=SCHEMA)
    op.execute(f"DROP FUNCTION {SCHEMA}.enforce_outbox_delivery_scope()")
