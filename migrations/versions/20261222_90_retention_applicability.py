"""Add immutable retention applicability and current target pointers."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261222_90"
down_revision = "20261221_89"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, attribute))


def _tenant_table(name: str, *, mutable: bool) -> None:
    runtime_role = _role("runtime_role")
    migration_role = _role("migration_role")
    op.execute(f"ALTER TABLE {SCHEMA}.{name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {SCHEMA}.{name} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY pol_{name}_tenant_isolation ON {SCHEMA}.{name} "
        f"FOR ALL TO {runtime_role} "
        "USING (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid) "
        "WITH CHECK (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid)"
    )
    op.execute(
        f"CREATE POLICY pol_{name}_migration_maintenance ON {SCHEMA}.{name} "
        f"FOR ALL TO {migration_role} USING (true) WITH CHECK (true)"
    )
    grants = "SELECT, INSERT, UPDATE" if mutable else "SELECT, INSERT"
    op.execute(f"GRANT {grants} ON TABLE {SCHEMA}.{name} TO {runtime_role}")
    op.execute(
        f"REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {SCHEMA}.{name} "
        f"FROM {runtime_role}"
    )


def upgrade() -> None:
    runtime_role = _role("runtime_role")
    op.create_table(
        "retention_applicabilities",
        sa.Column("applicability_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("target_reference", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_object_id", postgresql.UUID(as_uuid=True)),
        sa.Column("source_class", sa.Text()),
        sa.Column("source_class_reference", postgresql.UUID(as_uuid=True)),
        sa.Column("source_class_version_id", postgresql.UUID(as_uuid=True)),
        sa.Column("source_class_digest", sa.String(length=64)),
        sa.Column("retention_policy_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("retention_policy_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("retention_policy_digest", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("audit_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("outbox_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("applicability_id", name="pk_retention_applicabilities"),
        sa.UniqueConstraint(
            "workspace_id", "environment_id", "target_kind", "target_reference",
            "applicability_id", "generation",
            name="uq_retention_applicabilities_target_generation",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_retention_applicabilities_environment",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id"],
            [
                f"{SCHEMA}.source_objects.workspace_id",
                f"{SCHEMA}.source_objects.environment_id",
                f"{SCHEMA}.source_objects.source_object_id",
            ],
            name="fk_retention_applicabilities_source",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "audit_event_id"],
            [
                f"{SCHEMA}.audit_events.workspace_id",
                f"{SCHEMA}.audit_events.environment_id",
                f"{SCHEMA}.audit_events.audit_event_id",
            ],
            name="fk_retention_applicabilities_audit",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "outbox_event_id"],
            [
                f"{SCHEMA}.outbox_intents.workspace_id",
                f"{SCHEMA}.outbox_intents.environment_id",
                f"{SCHEMA}.outbox_intents.event_id",
            ],
            name="fk_retention_applicabilities_outbox",
        ),
        sa.CheckConstraint(
            "target_kind IN ('exact_source', 'source_class')",
            name="ck_retention_applicabilities_target_kind",
        ),
        sa.CheckConstraint(
            "(target_kind = 'exact_source' AND source_object_id IS NOT NULL "
            "AND target_reference = source_object_id AND source_class IS NULL "
            "AND source_class_reference IS NULL AND source_class_version_id IS NULL "
            "AND source_class_digest IS NULL) OR "
            "(target_kind = 'source_class' AND source_object_id IS NULL "
            "AND source_class IS NOT NULL AND source_class_reference IS NOT NULL "
            "AND target_reference = source_class_reference "
            "AND source_class_version_id IS NOT NULL AND source_class_digest IS NOT NULL)",
            name="ck_retention_applicabilities_target_shape",
        ),
        sa.CheckConstraint("generation >= 1", name="ck_retention_applicabilities_generation"),
        sa.CheckConstraint(
            "retention_policy_digest ~ '^[0-9a-f]{64}$'",
            name="ck_retention_applicabilities_policy_digest",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "current_retention_applicabilities",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("target_reference", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("current_applicability_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("predecessor_applicability_id", postgresql.UUID(as_uuid=True)),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id", "environment_id", "target_kind", "target_reference",
            name="pk_current_retention_applicabilities",
        ),
        sa.ForeignKeyConstraint(
            [
                "workspace_id", "environment_id", "target_kind", "target_reference",
                "current_applicability_id", "generation",
            ],
            [
                f"{SCHEMA}.retention_applicabilities.workspace_id",
                f"{SCHEMA}.retention_applicabilities.environment_id",
                f"{SCHEMA}.retention_applicabilities.target_kind",
                f"{SCHEMA}.retention_applicabilities.target_reference",
                f"{SCHEMA}.retention_applicabilities.applicability_id",
                f"{SCHEMA}.retention_applicabilities.generation",
            ],
            name="fk_current_retention_applicabilities_binding",
        ),
        sa.CheckConstraint("generation >= 1", name="ck_current_retention_applicabilities_generation"),
        schema=SCHEMA,
    )
    for name in ("retention_applicabilities", "current_retention_applicabilities"):
        _tenant_table(name, mutable=name.startswith("current_"))
        op.execute(
            f"COMMENT ON TABLE {SCHEMA}.{name} IS "
            "'classification=tenant_governance_state; source_content=forbidden; "
            "retention policy references only; no cleanup authority'"
        )
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.reject_retention_applicabilities_mutation() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ BEGIN
            RAISE EXCEPTION 'Retention applicability rows are immutable.'
                USING ERRCODE = '23514', CONSTRAINT = 'ck_retention_applicabilities_immutable';
        END; $function$
        """
    )
    op.execute(
        f"CREATE TRIGGER trg_retention_applicabilities_immutable BEFORE UPDATE OR DELETE "
        f"ON {SCHEMA}.retention_applicabilities FOR EACH ROW EXECUTE FUNCTION "
        f"{SCHEMA}.reject_retention_applicabilities_mutation()"
    )
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.guard_current_retention_applicability_step() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.generation <> 1 OR NEW.predecessor_applicability_id IS NOT NULL THEN
                    RAISE EXCEPTION 'Retention applicability baseline is invalid.'
                        USING ERRCODE = '23514', CONSTRAINT = 'ck_current_retention_applicability_step';
                END IF;
            ELSIF NEW.generation <> OLD.generation + 1
               OR NEW.predecessor_applicability_id IS DISTINCT FROM OLD.current_applicability_id THEN
                RAISE EXCEPTION 'Retention applicability expected-current CAS failed.'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_current_retention_applicability_step';
            END IF;
            RETURN NEW;
        END; $function$
        """
    )
    op.execute(
        f"CREATE TRIGGER trg_current_retention_applicability_step BEFORE INSERT OR UPDATE "
        f"ON {SCHEMA}.current_retention_applicabilities FOR EACH ROW EXECUTE FUNCTION "
        f"{SCHEMA}.guard_current_retention_applicability_step()"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.reject_retention_applicabilities_mutation() FROM PUBLIC"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.guard_current_retention_applicability_step() FROM PUBLIC"
    )
    op.execute(
        f"REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE "
        f"{SCHEMA}.retention_applicabilities FROM {runtime_role}"
    )


def downgrade() -> None:
    op.drop_table("current_retention_applicabilities", schema=SCHEMA)
    op.drop_table("retention_applicabilities", schema=SCHEMA)
    op.execute(f"DROP FUNCTION {SCHEMA}.guard_current_retention_applicability_step()")
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_retention_applicabilities_mutation()")
