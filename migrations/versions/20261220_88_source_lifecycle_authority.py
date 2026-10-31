"""Add immutable provider-neutral lifecycle evidence and current authority CAS."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261220_88"
down_revision = "20261219_87"
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
    op.execute(f"REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {SCHEMA}.{name} FROM {runtime_role}")


def _immutable(name: str) -> None:
    function = f"reject_{name}_mutation"
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.{function}() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ BEGIN
            RAISE EXCEPTION 'Lifecycle evidence rows are immutable.'
                USING ERRCODE = '23514', CONSTRAINT = 'ck_{name}_immutable';
        END; $function$
        """
    )
    op.execute(
        f"CREATE TRIGGER trg_{name}_immutable BEFORE UPDATE OR DELETE ON "
        f"{SCHEMA}.{name} FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.{function}()"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.{function}() FROM PUBLIC")


def upgrade() -> None:
    runtime_role = _role("runtime_role")
    migration_role = _role("migration_role")

    op.create_unique_constraint(
        "uq_audit_events_tenant_event",
        "audit_events",
        ["workspace_id", "environment_id", "audit_event_id"],
        schema=SCHEMA,
    )
    op.create_unique_constraint(
        "uq_outbox_intents_tenant_event",
        "outbox_intents",
        ["workspace_id", "environment_id", "event_id"],
        schema=SCHEMA,
    )
    op.create_unique_constraint(
        "uq_source_objects_lifecycle_connection",
        "source_objects",
        ["workspace_id", "environment_id", "source_object_id", "connection_id"],
        schema=SCHEMA,
    )

    op.create_table(
        "lifecycle_observations",
        sa.Column("observation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_reference", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_digest", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("evidence_reference", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("observation_id", name="pk_lifecycle_observations"),
        sa.UniqueConstraint(
            "workspace_id", "environment_id", "source_object_id", "observation_id",
            name="uq_lifecycle_observations_scope_id",
        ),
        sa.UniqueConstraint(
            "workspace_id", "environment_id", "source_object_id", "observation_id",
            "connection_id", "connection_version_id", "scope_reference", "scope_version_id",
            "scope_digest", name="uq_lifecycle_observations_exact_binding",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_lifecycle_observations_environment",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "connection_id"],
            [f"{SCHEMA}.source_objects.workspace_id", f"{SCHEMA}.source_objects.environment_id", f"{SCHEMA}.source_objects.source_object_id", f"{SCHEMA}.source_objects.connection_id"],
            name="fk_lifecycle_observations_source",
        ),
        sa.CheckConstraint(
            "kind IN ('provider_outage', 'permission_inaccessible', 'grant_uncertain', "
            "'grant_confirmed_revoked', 'scope_approval_superseded', 'scope_withdrawn', "
            "'move_within_scope', 'move_outside_scope', 'move_into_approved_scope', "
            "'trashed', 'deleted_metadata', 'filtered_absence', 'incomplete_scan', "
            "'qualified_deletion_evidence', 'retention_applicability_missing', "
            "'retention_expired', 'legal_hold_applied', 'legal_hold_released', "
            "'reappeared', 'identity_continuity_unproven')",
            name="ck_lifecycle_observations_kind",
        ),
        sa.CheckConstraint("scope_digest ~ '^[0-9a-f]{64}$'", name="ck_lifecycle_observations_scope_digest"),
        schema=SCHEMA,
    )

    op.create_table(
        "lifecycle_decisions",
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("observation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_reference", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_digest", sa.String(length=64), nullable=False),
        sa.Column("effect", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("authority_reference", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("authority_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("authority_kind", sa.Text(), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("audit_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("outbox_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("decision_id", name="pk_lifecycle_decisions"),
        sa.UniqueConstraint(
            "workspace_id", "environment_id", "source_object_id", "decision_id", "generation",
            name="uq_lifecycle_decisions_scope_generation",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id"],
            [f"{SCHEMA}.source_objects.workspace_id", f"{SCHEMA}.source_objects.environment_id", f"{SCHEMA}.source_objects.source_object_id"],
            name="fk_lifecycle_decisions_source",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "observation_id"],
            [f"{SCHEMA}.lifecycle_observations.workspace_id", f"{SCHEMA}.lifecycle_observations.environment_id", f"{SCHEMA}.lifecycle_observations.source_object_id", f"{SCHEMA}.lifecycle_observations.observation_id"],
            name="fk_lifecycle_decisions_observation",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "observation_id", "connection_id", "connection_version_id", "scope_reference", "scope_version_id", "scope_digest"],
            [f"{SCHEMA}.lifecycle_observations.workspace_id", f"{SCHEMA}.lifecycle_observations.environment_id", f"{SCHEMA}.lifecycle_observations.source_object_id", f"{SCHEMA}.lifecycle_observations.observation_id", f"{SCHEMA}.lifecycle_observations.connection_id", f"{SCHEMA}.lifecycle_observations.connection_version_id", f"{SCHEMA}.lifecycle_observations.scope_reference", f"{SCHEMA}.lifecycle_observations.scope_version_id", f"{SCHEMA}.lifecycle_observations.scope_digest"],
            name="fk_lifecycle_decisions_exact_observation_binding",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "audit_event_id"],
            [f"{SCHEMA}.audit_events.workspace_id", f"{SCHEMA}.audit_events.environment_id", f"{SCHEMA}.audit_events.audit_event_id"],
            name="fk_lifecycle_decisions_audit_event",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "outbox_event_id"],
            [f"{SCHEMA}.outbox_intents.workspace_id", f"{SCHEMA}.outbox_intents.environment_id", f"{SCHEMA}.outbox_intents.event_id"],
            name="fk_lifecycle_decisions_outbox_event",
        ),
        sa.CheckConstraint(
            "effect IN ('record_only', 'eligible', 'read_withdrawn', "
            "'disclosure_review_required', 'disclosure_held', "
            "'retention_applicability_unresolved', 'retention_ineligible', "
            "'authority_unavailable', 'identity_continuity_unproven')",
            name="ck_lifecycle_decisions_effect",
        ),
        sa.CheckConstraint(
            "status IN ('eligible', 'read_withdrawn', 'disclosure_review_required', "
            "'disclosure_held', 'retention_applicability_unresolved', 'retention_ineligible', "
            "'authority_unavailable', 'identity_continuity_unproven')",
            name="ck_lifecycle_decisions_status",
        ),
        sa.CheckConstraint("generation >= 1", name="ck_lifecycle_decisions_generation"),
        sa.CheckConstraint("scope_digest ~ '^[0-9a-f]{64}$'", name="ck_lifecycle_decisions_scope_digest"),
        sa.CheckConstraint(
            "authority_kind IN ('source_governance', 'retention_governance', "
            "'legal_compliance', 'provider_control_plane')",
            name="ck_lifecycle_decisions_authority_kind",
        ),
        schema=SCHEMA,
    )

    op.create_table(
        "current_lifecycle_authorities",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("current_decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("predecessor_decision_id", postgresql.UUID(as_uuid=True)),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id", "environment_id", "source_object_id",
            name="pk_current_lifecycle_authorities",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id"],
            [f"{SCHEMA}.source_objects.workspace_id", f"{SCHEMA}.source_objects.environment_id", f"{SCHEMA}.source_objects.source_object_id"],
            name="fk_current_lifecycle_authorities_source",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "current_decision_id", "generation"],
            [f"{SCHEMA}.lifecycle_decisions.workspace_id", f"{SCHEMA}.lifecycle_decisions.environment_id", f"{SCHEMA}.lifecycle_decisions.source_object_id", f"{SCHEMA}.lifecycle_decisions.decision_id", f"{SCHEMA}.lifecycle_decisions.generation"],
            name="fk_current_lifecycle_authorities_decision",
        ),
        sa.CheckConstraint("generation >= 1", name="ck_current_lifecycle_authorities_generation"),
        schema=SCHEMA,
    )

    op.create_index(
        "ix_lifecycle_observations_source_time",
        "lifecycle_observations",
        ["workspace_id", "environment_id", "source_object_id", "observed_at"],
        schema=SCHEMA,
    )
    for name in ("lifecycle_observations", "lifecycle_decisions", "current_lifecycle_authorities"):
        _tenant_table(name, mutable=name == "current_lifecycle_authorities")
        op.execute(
            f"COMMENT ON TABLE {SCHEMA}.{name} IS "
            "'classification=tenant_operational_state; provider_content=forbidden; "
            "lifecycle authority uses immutable references and expected-current CAS'"
        )
    _immutable("lifecycle_observations")
    _immutable("lifecycle_decisions")
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.guard_current_lifecycle_authority_step() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.generation <> 1 OR NEW.predecessor_decision_id IS NOT NULL THEN
                    RAISE EXCEPTION 'Lifecycle authority baseline is invalid.'
                        USING ERRCODE = '23514', CONSTRAINT = 'ck_current_lifecycle_authority_step';
                END IF;
            ELSIF NEW.generation <> OLD.generation + 1
               OR NEW.predecessor_decision_id IS DISTINCT FROM OLD.current_decision_id THEN
                RAISE EXCEPTION 'Lifecycle authority expected-current CAS failed.'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_current_lifecycle_authority_step';
            END IF;
            RETURN NEW;
        END; $function$
        """
    )
    op.execute(
        f"CREATE TRIGGER trg_current_lifecycle_authority_step BEFORE INSERT OR UPDATE "
        f"ON {SCHEMA}.current_lifecycle_authorities FOR EACH ROW EXECUTE FUNCTION "
        f"{SCHEMA}.guard_current_lifecycle_authority_step()"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.guard_current_lifecycle_authority_step() FROM PUBLIC")
    op.execute(f"REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {SCHEMA}.current_lifecycle_authorities FROM {runtime_role}")
    op.execute(f"REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {SCHEMA}.lifecycle_observations FROM {runtime_role}")
    op.execute(f"REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {SCHEMA}.lifecycle_decisions FROM {runtime_role}")
    del migration_role


def downgrade() -> None:
    op.drop_table("current_lifecycle_authorities", schema=SCHEMA)
    op.drop_table("lifecycle_decisions", schema=SCHEMA)
    op.drop_table("lifecycle_observations", schema=SCHEMA)
    op.execute(f"DROP FUNCTION {SCHEMA}.guard_current_lifecycle_authority_step()")
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_lifecycle_decisions_mutation()")
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_lifecycle_observations_mutation()")
    op.drop_constraint("uq_outbox_intents_tenant_event", "outbox_intents", schema=SCHEMA, type_="unique")
    op.drop_constraint("uq_audit_events_tenant_event", "audit_events", schema=SCHEMA, type_="unique")
    op.drop_constraint("uq_source_objects_lifecycle_connection", "source_objects", schema=SCHEMA, type_="unique")
