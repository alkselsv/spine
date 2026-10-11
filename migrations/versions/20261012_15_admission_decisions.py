"""Persist immutable, tenant-scoped source admission decisions."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261012_15"
down_revision = "20261012_14"
branch_labels = None
depends_on = None
SCHEMA = "spine"
_IDENTIFIER = "^[a-z][a-z0-9_.:-]{0,63}$"
_UUID = "^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def _uuid_setting(name: str) -> str:
    setting = f"pg_catalog.current_setting('spine.{name}', true)"
    return f"CASE WHEN {setting} OPERATOR(pg_catalog.~) '{_UUID}' THEN CAST({setting} AS pg_catalog.uuid) ELSE NULL END"


def _tenant(table: str) -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    qualified = f'{SCHEMA}."{table}"'
    scope = f"workspace_id = ({_uuid_setting('workspace_id')}) AND environment_id = ({_uuid_setting('environment_id')})"
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(f'CREATE POLICY "pol_{table}_tenant_isolation" ON {qualified} FOR ALL TO {runtime} USING ({scope}) WITH CHECK ({scope})')
    op.execute(f'CREATE POLICY "pol_{table}_migration_maintenance" ON {qualified} FOR ALL TO {migration} USING (true) WITH CHECK (true)')
    op.execute(f"GRANT SELECT, INSERT ON TABLE {qualified} TO {runtime}")
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {qualified} FROM {runtime}")


def _immutable() -> None:
    qualified = f'{SCHEMA}."admission_decisions"'
    op.execute(f"""
        CREATE FUNCTION {SCHEMA}.reject_admission_decisions_mutation() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ BEGIN
            RAISE EXCEPTION 'Admission decisions are immutable.'
                USING ERRCODE = '23514', CONSTRAINT = 'ck_admission_decisions_immutable';
        END; $function$
    """)
    op.execute(f"CREATE TRIGGER trg_admission_decisions_immutable BEFORE UPDATE OR DELETE ON {qualified} FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_admission_decisions_mutation()")
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.reject_admission_decisions_mutation() FROM PUBLIC")


def _validate_admitted_revision() -> None:
    qualified = f'{SCHEMA}."admission_decisions"'
    op.execute(f"""
        CREATE FUNCTION {SCHEMA}.validate_admitted_source_revision() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE revision_kind pg_catalog.text;
        BEGIN
            IF NEW.disposition <> 'admitted' THEN
                RETURN NEW;
            END IF;
            SELECT kind INTO revision_kind
            FROM {SCHEMA}.source_revisions
            WHERE workspace_id = NEW.workspace_id
              AND environment_id = NEW.environment_id
              AND source_object_id = NEW.source_object_id
              AND revision_id = NEW.source_revision_id;
            IF revision_kind IS DISTINCT FROM 'content' THEN
                RAISE EXCEPTION 'Tombstone admission requires deletion-specific checks.'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_admission_decisions_content_only';
            END IF;
            IF NOT EXISTS (
                SELECT 1
                FROM {SCHEMA}.source_revision_provenance
                WHERE workspace_id = NEW.workspace_id
                  AND environment_id = NEW.environment_id
                  AND source_object_id = NEW.source_object_id
                  AND revision_id = NEW.source_revision_id
            ) THEN
                RAISE EXCEPTION 'Admission provenance is incomplete.'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_admission_decisions_provenance';
            END IF;
            RETURN NEW;
        END;
        $function$
    """)
    op.execute(f"CREATE TRIGGER trg_admission_decisions_validate BEFORE INSERT ON {qualified} FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.validate_admitted_source_revision()")
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.validate_admitted_source_revision() FROM PUBLIC")


def upgrade() -> None:
    runtime = _role("runtime_role")
    op.execute(f"""
        CREATE FUNCTION {SCHEMA}.validate_admission_reason_codes(payload pg_catalog.jsonb)
        RETURNS boolean LANGUAGE sql IMMUTABLE SECURITY INVOKER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
            SELECT CASE
                WHEN pg_catalog.jsonb_typeof(payload) <> 'array' THEN false
                WHEN pg_catalog.jsonb_array_length(payload) = 0 THEN false
                ELSE NOT EXISTS (
                    SELECT 1
                    FROM pg_catalog.jsonb_array_elements_text(payload) AS reason(value)
                    WHERE pg_catalog.length(reason.value) < 1
                       OR pg_catalog.length(reason.value) > 64
                       OR reason.value OPERATOR(pg_catalog.!~) '{_IDENTIFIER}'
                )
            END
        $function$
    """)
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.validate_admission_reason_codes(pg_catalog.jsonb) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SCHEMA}.validate_admission_reason_codes(pg_catalog.jsonb) TO {runtime}")
    op.create_table(
        "admission_decisions",
        sa.Column("admission_decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision_version", sa.Integer(), nullable=False),
        sa.Column("predecessor_decision_id", postgresql.UUID(as_uuid=True)),
        sa.Column("disposition", sa.Text(), nullable=False),
        sa.Column("validator_profile_version", sa.Text(), nullable=False),
        sa.Column("reason_schema_version", sa.Text(), nullable=False),
        sa.Column("reason_codes", postgresql.JSONB(), nullable=False),
        sa.Column("authority_reference", postgresql.JSONB(), nullable=False),
        sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("audit_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("admission_decision_id", name="pk_admission_decisions"),
        sa.UniqueConstraint("workspace_id", "environment_id", "source_revision_id", "admission_decision_id", name="uq_admission_decisions_scope_revision_id"),
        sa.UniqueConstraint("workspace_id", "environment_id", "source_revision_id", "decision_version", name="uq_admission_decisions_revision_version"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "source_revision_id"],
            [f"{SCHEMA}.source_revisions.workspace_id", f"{SCHEMA}.source_revisions.environment_id", f"{SCHEMA}.source_revisions.source_object_id", f"{SCHEMA}.source_revisions.revision_id"],
            name="fk_admission_decisions_revision",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_revision_id", "predecessor_decision_id"],
            [f"{SCHEMA}.admission_decisions.workspace_id", f"{SCHEMA}.admission_decisions.environment_id", f"{SCHEMA}.admission_decisions.source_revision_id", f"{SCHEMA}.admission_decisions.admission_decision_id"],
            name="fk_admission_decisions_predecessor",
        ),
        sa.CheckConstraint("decision_version > 0", name="ck_admission_decisions_version_positive"),
        sa.CheckConstraint("disposition IN ('admitted', 'quarantined', 'rejected')", name="ck_admission_decisions_disposition"),
        sa.CheckConstraint(f"validator_profile_version OPERATOR(pg_catalog.~) '{_IDENTIFIER}'", name="ck_admission_decisions_profile_identifier"),
        sa.CheckConstraint(f"reason_schema_version OPERATOR(pg_catalog.~) '{_IDENTIFIER}'", name="ck_admission_decisions_reason_schema_identifier"),
        sa.CheckConstraint(f"{SCHEMA}.validate_admission_reason_codes(reason_codes)", name="ck_admission_decisions_reason_codes"),
        sa.CheckConstraint("jsonb_typeof(authority_reference) = 'object' AND (authority_reference - 'reference_type' - 'reference_id' - 'schema_version') = '{}'::pg_catalog.jsonb AND authority_reference ?& ARRAY['reference_type', 'reference_id', 'schema_version']", name="ck_admission_decisions_authority_reference"),
        sa.CheckConstraint(f"trace_id <> '00000000-0000-0000-0000-000000000000'::pg_catalog.uuid AND audit_event_id <> '00000000-0000-0000-0000-000000000000'::pg_catalog.uuid", name="ck_admission_decisions_references_nonzero"),
        sa.CheckConstraint("(predecessor_decision_id IS NULL AND decision_version = 1) OR predecessor_decision_id IS NOT NULL", name="ck_admission_decisions_root_version"),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_admission_decisions_root",
        "admission_decisions",
        ["workspace_id", "environment_id", "source_revision_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("predecessor_decision_id IS NULL"),
    )
    op.create_index(
        "uq_admission_decisions_successor",
        "admission_decisions",
        ["workspace_id", "environment_id", "predecessor_decision_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("predecessor_decision_id IS NOT NULL"),
    )
    op.create_index("ix_admission_decisions_effective", "admission_decisions", ["workspace_id", "environment_id", "source_revision_id", "decision_version"], schema=SCHEMA)
    _tenant("admission_decisions")
    _immutable()
    _validate_admitted_revision()


def downgrade() -> None:
    runtime = _role("runtime_role")
    qualified = f'{SCHEMA}."admission_decisions"'
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {qualified} FROM {runtime}")
    op.drop_table("admission_decisions", schema=SCHEMA)
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_admission_decisions_mutation()")
    op.execute(f"DROP FUNCTION {SCHEMA}.validate_admitted_source_revision()")
    op.execute(f"DROP FUNCTION {SCHEMA}.validate_admission_reason_codes(pg_catalog.jsonb)")
