"""Persist the tenant-scoped append-only Audit Event ledger.

Revision ID: 20261010_08
Revises: 20261010_07
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261010_08"
down_revision = "20261010_07"
branch_labels = None
depends_on = None

SCHEMA = "spine"
TABLE = "audit_events"
_IDENTIFIER_PATTERN = "^[a-z][a-z0-9_.:-]{0,127}$"
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
            "audit_event_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("acting_subject_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "service_principal_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("causation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "appended_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("target_type", sa.Text(), nullable=True),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_schema_version", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("producer_deduplication_id", sa.Text(), nullable=True),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.PrimaryKeyConstraint("audit_event_id", name="pk_audit_events"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            [f"{SCHEMA}.workspaces.id"],
            name="fk_audit_events_workspace_id_workspaces",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_audit_events_scope_environments",
        ),
        sa.CheckConstraint(
            "schema_version > 0",
            name=op.f("ck_audit_events_schema_version_positive"),
        ),
        sa.CheckConstraint(
            f"event_type OPERATOR(pg_catalog.~) '{_IDENTIFIER_PATTERN}'",
            name=op.f("ck_audit_events_event_type_identifier"),
        ),
        sa.CheckConstraint(
            "origin IN ('interactive', 'worker')",
            name=op.f("ck_audit_events_origin"),
        ),
        sa.CheckConstraint(
            "(origin = 'interactive' AND acting_subject_id IS NOT NULL) OR "
            "(origin = 'worker' AND acting_subject_id IS NULL "
            "AND service_principal_id IS NOT NULL)",
            name=op.f("ck_audit_events_actor_scope"),
        ),
        sa.CheckConstraint(
            "(target_type IS NULL AND target_id IS NULL "
            "AND target_schema_version IS NULL) OR "
            f"(target_type OPERATOR(pg_catalog.~) '{_IDENTIFIER_PATTERN}' "
            "AND target_id IS NOT NULL AND target_schema_version > 0)",
            name=op.f("ck_audit_events_target_complete"),
        ),
        sa.CheckConstraint(
            f"outcome OPERATOR(pg_catalog.~) '{_IDENTIFIER_PATTERN}'",
            name=op.f("ck_audit_events_outcome_identifier"),
        ),
        sa.CheckConstraint(
            f"reason OPERATOR(pg_catalog.~) '{_IDENTIFIER_PATTERN}'",
            name=op.f("ck_audit_events_reason_identifier"),
        ),
        sa.CheckConstraint(
            "producer_deduplication_id IS NULL OR "
            f"producer_deduplication_id OPERATOR(pg_catalog.~) '{_IDENTIFIER_PATTERN}'",
            name=op.f("ck_audit_events_producer_identifier"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name=op.f("ck_audit_events_payload_object"),
        ),
        schema=SCHEMA,
    )

    producer_columns = [
        "workspace_id",
        "event_type",
        "producer_deduplication_id",
    ]
    op.create_index(
        "uq_audit_events_workspace_producer",
        TABLE,
        producer_columns,
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text(
            "environment_id IS NULL AND producer_deduplication_id IS NOT NULL"
        ),
    )
    op.create_index(
        "uq_audit_events_environment_producer",
        TABLE,
        ["workspace_id", "environment_id", *producer_columns[1:]],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text(
            "environment_id IS NOT NULL AND producer_deduplication_id IS NOT NULL"
        ),
    )
    op.create_index(
        "ix_audit_events_tenant_appended_at",
        TABLE,
        ["workspace_id", "environment_id", sa.text("appended_at DESC")],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_audit_events_workspace_trace_id",
        TABLE,
        ["workspace_id", "trace_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_audit_events_workspace_target",
        TABLE,
        ["workspace_id", "target_type", "target_id"],
        schema=SCHEMA,
        postgresql_where=sa.text("target_id IS NOT NULL"),
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
        f'CREATE POLICY "pol_{TABLE}_tenant_read" '
        f"ON {qualified_table} FOR SELECT TO {runtime_role} "
        f"USING ({tenant_expression})"
    )
    op.execute(
        f'CREATE POLICY "pol_{TABLE}_tenant_append" '
        f"ON {qualified_table} FOR INSERT TO {runtime_role} "
        f"WITH CHECK ({tenant_expression})"
    )
    op.execute(
        f'CREATE POLICY "pol_{TABLE}_migration_maintenance" '
        f"ON {qualified_table} FOR ALL TO {migration_role} "
        "USING (true) WITH CHECK (true)"
    )

    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.reject_audit_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        BEGIN
            RAISE EXCEPTION 'Audit Events are immutable.'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'ck_audit_events_immutable';
        END;
        $function$
        """
    )
    op.execute(
        f"CREATE TRIGGER trg_audit_events_immutable "
        f"BEFORE UPDATE OR DELETE ON {qualified_table} FOR EACH ROW "
        f"EXECUTE FUNCTION {SCHEMA}.reject_audit_event_mutation()"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.reject_audit_event_mutation() FROM PUBLIC"
    )
    op.execute(
        f"COMMENT ON TABLE {qualified_table} IS "
        "'classification=tenant_audit; runtime_table_access=append_scoped_read'"
    )
    op.execute(
        f"GRANT SELECT, INSERT ON TABLE {qualified_table} TO {runtime_role}"
    )


def downgrade() -> None:
    runtime_role = _quoted_role("runtime_role")
    qualified_table = f'{SCHEMA}."{TABLE}"'
    op.execute(
        f"REVOKE ALL PRIVILEGES ON TABLE {qualified_table} FROM {runtime_role}"
    )
    op.drop_table(TABLE, schema=SCHEMA)
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_audit_event_mutation()")
