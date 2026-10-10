"""Persist immutable tenant-scoped outbox intents.

Revision ID: 20261010_04
Revises: 20261010_03
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261010_04"
down_revision = "20261010_03"
branch_labels = None
depends_on = None

SCHEMA = "spine"
TABLE = "outbox_intents"
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
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("event_schema_version", sa.Integer(), nullable=False),
        sa.Column("aggregate_type", sa.Text(), nullable=True),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("aggregate_schema_version", sa.Integer(), nullable=True),
        sa.Column("producer_deduplication_id", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("causation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_outbox_intents"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            [f"{SCHEMA}.workspaces.id"],
            name="fk_outbox_intents_workspace_id_workspaces",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_outbox_intents_scope_environments",
        ),
        sa.CheckConstraint(
            "event_schema_version > 0",
            name=op.f("ck_outbox_intents_event_schema_version_positive"),
        ),
        sa.CheckConstraint(
            "event_type OPERATOR(pg_catalog.~) '^[a-z][a-z0-9_.:-]{0,127}$'",
            name=op.f("ck_outbox_intents_event_type_identifier"),
        ),
        sa.CheckConstraint(
            "producer_deduplication_id IS NULL OR "
            "char_length(producer_deduplication_id) BETWEEN 1 AND 255",
            name=op.f("ck_outbox_intents_producer_deduplication_id_length"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name=op.f("ck_outbox_intents_payload_object"),
        ),
        sa.CheckConstraint(
            "(aggregate_type IS NULL AND aggregate_id IS NULL "
            "AND aggregate_schema_version IS NULL) OR "
            "(aggregate_type OPERATOR(pg_catalog.~) "
            "'^[a-z][a-z0-9_.:-]{0,127}$' AND aggregate_id IS NOT NULL "
            "AND aggregate_schema_version > 0)",
            name=op.f("ck_outbox_intents_aggregate_complete"),
        ),
        schema=SCHEMA,
    )
    producer_columns = [
        "workspace_id",
        "event_type",
        "producer_deduplication_id",
    ]
    op.create_index(
        "uq_outbox_intents_workspace_producer",
        TABLE,
        producer_columns,
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text(
            "environment_id IS NULL AND producer_deduplication_id IS NOT NULL"
        ),
    )
    op.create_index(
        "uq_outbox_intents_environment_producer",
        TABLE,
        ["workspace_id", "environment_id", *producer_columns[1:]],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text(
            "environment_id IS NOT NULL AND producer_deduplication_id IS NOT NULL"
        ),
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
        CREATE FUNCTION {SCHEMA}.reject_outbox_intent_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        BEGIN
            RAISE EXCEPTION 'Outbox intents are immutable.'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'ck_outbox_intents_immutable';
        END;
        $function$
        """
    )
    op.execute(
        f"CREATE TRIGGER trg_outbox_intents_immutable "
        f"BEFORE UPDATE OR DELETE ON {qualified_table} FOR EACH ROW "
        f"EXECUTE FUNCTION {SCHEMA}.reject_outbox_intent_mutation()"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.reject_outbox_intent_mutation() FROM PUBLIC"
    )
    op.execute(f"GRANT INSERT ON TABLE {qualified_table} TO {runtime_role}")


def downgrade() -> None:
    runtime_role = _quoted_role("runtime_role")
    qualified_table = f'{SCHEMA}."{TABLE}"'
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {qualified_table} FROM {runtime_role}")
    op.drop_table(TABLE, schema=SCHEMA)
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_outbox_intent_mutation()")
