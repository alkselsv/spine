"""Add immutable governed disclosure holds and exact release CAS."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings

revision = "20261223_91"
down_revision = "20261222_90"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, attribute))


def _tenant_table(name: str, *, mutable: bool = False) -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    op.execute(f"ALTER TABLE {SCHEMA}.{name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {SCHEMA}.{name} FORCE ROW LEVEL SECURITY")
    scope = (
        "workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid"
    )
    op.execute(
        f"CREATE POLICY pol_{name}_tenant_isolation ON {SCHEMA}.{name} "
        f"FOR ALL TO {runtime} USING ({scope}) WITH CHECK ({scope})"
    )
    op.execute(
        f"CREATE POLICY pol_{name}_migration_maintenance ON {SCHEMA}.{name} "
        f"FOR ALL TO {migration} USING (true) WITH CHECK (true)"
    )
    grants = "SELECT, INSERT, UPDATE" if mutable else "SELECT, INSERT"
    op.execute(f"GRANT {grants} ON TABLE {SCHEMA}.{name} TO {runtime}")
    op.execute(f"REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {SCHEMA}.{name} FROM {runtime}")


def _immutable(name: str) -> None:
    function = f"reject_{name}_mutation"
    op.execute(f"""
        CREATE FUNCTION {SCHEMA}.{function}() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ BEGIN
            RAISE EXCEPTION 'Disclosure hold history is immutable.'
                USING ERRCODE = '23514', CONSTRAINT = 'ck_{name}_immutable';
        END; $function$
        """)
    op.execute(
        f"CREATE TRIGGER trg_{name}_immutable BEFORE UPDATE OR DELETE ON "
        f"{SCHEMA}.{name} FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.{function}()"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.{function}() FROM PUBLIC")


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_unique_constraint(
        "uq_lifecycle_decisions_scope_id",
        "lifecycle_decisions",
        ["workspace_id", "environment_id", "source_object_id", "decision_id"],
        schema=SCHEMA,
    )
    op.create_table(
        "disclosure_holds",
        sa.Column("hold_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("source_object_id", uuid, nullable=False),
        sa.Column("review_decision_id", uuid, nullable=False),
        sa.Column("evidence_observation_id", uuid, nullable=False),
        sa.Column("source_revision_ids", postgresql.ARRAY(uuid), nullable=False),
        sa.Column("dependency_ids", postgresql.ARRAY(uuid), nullable=False),
        sa.Column("artifact_ids", postgresql.ARRAY(uuid), nullable=False),
        sa.Column("prohibited_effects", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("audit_event_id", uuid, nullable=False),
        sa.Column("outbox_event_id", uuid, nullable=False),
        sa.Column("supersedes_hold_id", uuid),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_until", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("hold_id", name="pk_disclosure_holds"),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "source_object_id",
            "hold_id",
            name="uq_disclosure_holds_scope_id",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "source_object_id",
            "supersedes_hold_id",
            name="uq_disclosure_holds_one_successor",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id"],
            [
                f"{SCHEMA}.source_objects.workspace_id",
                f"{SCHEMA}.source_objects.environment_id",
                f"{SCHEMA}.source_objects.source_object_id",
            ],
            name="fk_disclosure_holds_source",
        ),
        sa.ForeignKeyConstraint(
            [
                "workspace_id",
                "environment_id",
                "source_object_id",
                "review_decision_id",
            ],
            [
                f"{SCHEMA}.lifecycle_decisions.workspace_id",
                f"{SCHEMA}.lifecycle_decisions.environment_id",
                f"{SCHEMA}.lifecycle_decisions.source_object_id",
                f"{SCHEMA}.lifecycle_decisions.decision_id",
            ],
            name="fk_disclosure_holds_review_decision",
        ),
        sa.ForeignKeyConstraint(
            [
                "workspace_id",
                "environment_id",
                "source_object_id",
                "evidence_observation_id",
            ],
            [
                f"{SCHEMA}.lifecycle_observations.workspace_id",
                f"{SCHEMA}.lifecycle_observations.environment_id",
                f"{SCHEMA}.lifecycle_observations.source_object_id",
                f"{SCHEMA}.lifecycle_observations.observation_id",
            ],
            name="fk_disclosure_holds_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "audit_event_id"],
            [
                f"{SCHEMA}.audit_events.workspace_id",
                f"{SCHEMA}.audit_events.environment_id",
                f"{SCHEMA}.audit_events.audit_event_id",
            ],
            name="fk_disclosure_holds_audit_event",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "outbox_event_id"],
            [
                f"{SCHEMA}.outbox_intents.workspace_id",
                f"{SCHEMA}.outbox_intents.environment_id",
                f"{SCHEMA}.outbox_intents.event_id",
            ],
            name="fk_disclosure_holds_outbox_event",
        ),
        sa.ForeignKeyConstraint(
            [
                "workspace_id",
                "environment_id",
                "source_object_id",
                "supersedes_hold_id",
            ],
            [
                f"{SCHEMA}.disclosure_holds.workspace_id",
                f"{SCHEMA}.disclosure_holds.environment_id",
                f"{SCHEMA}.disclosure_holds.source_object_id",
                f"{SCHEMA}.disclosure_holds.hold_id",
            ],
            name="fk_disclosure_holds_superseded_hold",
        ),
        sa.CheckConstraint(
            "cardinality(source_revision_ids) > 0 AND cardinality(dependency_ids) > 0 "
            "AND cardinality(artifact_ids) > 0 AND cardinality(prohibited_effects) > 0",
            name="ck_disclosure_holds_exact_scope",
        ),
        sa.CheckConstraint(
            "prohibited_effects <@ ARRAY['retrieval','model_use','final_disclosure','derived_artifact_disclosure']::text[]",
            name="ck_disclosure_holds_effects",
        ),
        sa.CheckConstraint("effective_until > effective_from", name="ck_disclosure_holds_interval"),
        schema=SCHEMA,
    )
    op.create_table(
        "disclosure_hold_releases",
        sa.Column("release_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("source_object_id", uuid, nullable=False),
        sa.Column("hold_id", uuid, nullable=False),
        sa.Column("audit_event_id", uuid, nullable=False),
        sa.Column("outbox_event_id", uuid, nullable=False),
        sa.Column("superseded_by_hold_id", uuid),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("release_id", name="pk_disclosure_hold_releases"),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "source_object_id",
            "hold_id",
            name="uq_disclosure_hold_releases_hold",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "source_object_id",
            "hold_id",
            "release_id",
            name="uq_disclosure_hold_releases_scope_id",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "hold_id"],
            [
                f"{SCHEMA}.disclosure_holds.workspace_id",
                f"{SCHEMA}.disclosure_holds.environment_id",
                f"{SCHEMA}.disclosure_holds.source_object_id",
                f"{SCHEMA}.disclosure_holds.hold_id",
            ],
            name="fk_disclosure_hold_releases_hold",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "audit_event_id"],
            [
                f"{SCHEMA}.audit_events.workspace_id",
                f"{SCHEMA}.audit_events.environment_id",
                f"{SCHEMA}.audit_events.audit_event_id",
            ],
            name="fk_disclosure_hold_releases_audit_event",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "outbox_event_id"],
            [
                f"{SCHEMA}.outbox_intents.workspace_id",
                f"{SCHEMA}.outbox_intents.environment_id",
                f"{SCHEMA}.outbox_intents.event_id",
            ],
            name="fk_disclosure_hold_releases_outbox_event",
        ),
        sa.ForeignKeyConstraint(
            [
                "workspace_id",
                "environment_id",
                "source_object_id",
                "superseded_by_hold_id",
            ],
            [
                f"{SCHEMA}.disclosure_holds.workspace_id",
                f"{SCHEMA}.disclosure_holds.environment_id",
                f"{SCHEMA}.disclosure_holds.source_object_id",
                f"{SCHEMA}.disclosure_holds.hold_id",
            ],
            name="fk_disclosure_hold_releases_successor",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "disclosure_hold_source_revisions",
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("source_object_id", uuid, nullable=False),
        sa.Column("hold_id", uuid, nullable=False),
        sa.Column("source_revision_id", uuid, nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "environment_id",
            "source_object_id",
            "hold_id",
            "source_revision_id",
            name="pk_disclosure_hold_source_revisions",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "hold_id"],
            [
                f"{SCHEMA}.disclosure_holds.workspace_id",
                f"{SCHEMA}.disclosure_holds.environment_id",
                f"{SCHEMA}.disclosure_holds.source_object_id",
                f"{SCHEMA}.disclosure_holds.hold_id",
            ],
            name="fk_disclosure_hold_source_revisions_hold",
        ),
        sa.ForeignKeyConstraint(
            [
                "workspace_id",
                "environment_id",
                "source_object_id",
                "source_revision_id",
            ],
            [
                f"{SCHEMA}.source_revisions.workspace_id",
                f"{SCHEMA}.source_revisions.environment_id",
                f"{SCHEMA}.source_revisions.source_object_id",
                f"{SCHEMA}.source_revisions.revision_id",
            ],
            name="fk_disclosure_hold_source_revisions_revision",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "disclosure_hold_targets",
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("source_object_id", uuid, nullable=False),
        sa.Column("hold_id", uuid, nullable=False),
        sa.Column("source_revision_id", uuid, nullable=False),
        sa.Column("dependency_id", uuid, nullable=False),
        sa.Column("artifact_id", uuid, nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "environment_id",
            "source_object_id",
            "hold_id",
            "source_revision_id",
            "dependency_id",
            "artifact_id",
            name="pk_disclosure_hold_targets",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "hold_id"],
            [
                f"{SCHEMA}.disclosure_holds.workspace_id",
                f"{SCHEMA}.disclosure_holds.environment_id",
                f"{SCHEMA}.disclosure_holds.source_object_id",
                f"{SCHEMA}.disclosure_holds.hold_id",
            ],
            name="fk_disclosure_hold_targets_hold",
        ),
        sa.ForeignKeyConstraint(
            [
                "workspace_id",
                "environment_id",
                "source_object_id",
                "source_revision_id",
            ],
            [
                f"{SCHEMA}.source_revisions.workspace_id",
                f"{SCHEMA}.source_revisions.environment_id",
                f"{SCHEMA}.source_revisions.source_object_id",
                f"{SCHEMA}.source_revisions.revision_id",
            ],
            name="fk_disclosure_hold_targets_revision",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "current_disclosure_holds",
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("source_object_id", uuid, nullable=False),
        sa.Column("hold_id", uuid, nullable=False),
        sa.Column("current_release_id", uuid),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "environment_id",
            "source_object_id",
            "hold_id",
            name="pk_current_disclosure_holds",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "hold_id"],
            [
                f"{SCHEMA}.disclosure_holds.workspace_id",
                f"{SCHEMA}.disclosure_holds.environment_id",
                f"{SCHEMA}.disclosure_holds.source_object_id",
                f"{SCHEMA}.disclosure_holds.hold_id",
            ],
            name="fk_current_disclosure_holds_hold",
        ),
        sa.ForeignKeyConstraint(
            [
                "workspace_id",
                "environment_id",
                "source_object_id",
                "hold_id",
                "current_release_id",
            ],
            [
                f"{SCHEMA}.disclosure_hold_releases.workspace_id",
                f"{SCHEMA}.disclosure_hold_releases.environment_id",
                f"{SCHEMA}.disclosure_hold_releases.source_object_id",
                f"{SCHEMA}.disclosure_hold_releases.hold_id",
                f"{SCHEMA}.disclosure_hold_releases.release_id",
            ],
            name="fk_current_disclosure_holds_release",
        ),
        sa.CheckConstraint("state IN ('active','released')", name="ck_current_disclosure_holds_state"),
        sa.CheckConstraint(
            "(state = 'active' AND generation = 1 AND current_release_id IS NULL) OR "
            "(state = 'released' AND generation = 2 AND current_release_id IS NOT NULL)",
            name="ck_current_disclosure_holds_transition",
        ),
        schema=SCHEMA,
    )
    for name in (
        "disclosure_holds",
        "disclosure_hold_releases",
        "disclosure_hold_source_revisions",
        "disclosure_hold_targets",
        "current_disclosure_holds",
    ):
        _tenant_table(name, mutable=name == "current_disclosure_holds")
        op.execute(
            f"COMMENT ON TABLE {SCHEMA}.{name} IS "
            "'classification=tenant_operational_state; provider_content=forbidden; deny-only disclosure governance'"
        )
    _immutable("disclosure_holds")
    _immutable("disclosure_hold_releases")
    _immutable("disclosure_hold_source_revisions")
    _immutable("disclosure_hold_targets")
    op.execute(f"""
        CREATE FUNCTION {SCHEMA}.guard_disclosure_hold_target_insert() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ DECLARE target_is_declared boolean; BEGIN
            SELECT source_revision_ids @> ARRAY[NEW.source_revision_id]
               AND dependency_ids @> ARRAY[NEW.dependency_id]
               AND artifact_ids @> ARRAY[NEW.artifact_id]
              INTO target_is_declared
              FROM {SCHEMA}.disclosure_holds
             WHERE workspace_id = NEW.workspace_id
               AND environment_id = NEW.environment_id
               AND source_object_id = NEW.source_object_id
               AND hold_id = NEW.hold_id;
            IF target_is_declared IS DISTINCT FROM true OR EXISTS (
                SELECT 1 FROM {SCHEMA}.current_disclosure_holds
                 WHERE workspace_id = NEW.workspace_id
                   AND environment_id = NEW.environment_id
                   AND source_object_id = NEW.source_object_id
                   AND hold_id = NEW.hold_id
            ) THEN
                RAISE EXCEPTION 'Disclosure hold target is not part of the immutable declared set.'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_disclosure_hold_target_declared';
            END IF;
            RETURN NEW;
        END; $function$
        """)
    op.execute(
        f"CREATE TRIGGER trg_disclosure_hold_target_insert BEFORE INSERT ON "
        f"{SCHEMA}.disclosure_hold_targets FOR EACH ROW EXECUTE FUNCTION "
        f"{SCHEMA}.guard_disclosure_hold_target_insert()"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.guard_disclosure_hold_target_insert() FROM PUBLIC")
    op.execute(f"""
        CREATE FUNCTION {SCHEMA}.guard_current_disclosure_hold_step() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ DECLARE targets_complete boolean; BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.state <> 'active' OR NEW.generation <> 1 OR NEW.current_release_id IS NOT NULL THEN
                    RAISE EXCEPTION 'Disclosure hold baseline is invalid.'
                        USING ERRCODE = '23514', CONSTRAINT = 'ck_current_disclosure_hold_step';
                END IF;
                SELECT cardinality(hold.source_revision_ids)
                           * cardinality(hold.dependency_ids)
                           * cardinality(hold.artifact_ids)
                           = (
                               SELECT count(*)
                                 FROM {SCHEMA}.disclosure_hold_targets AS target
                                WHERE target.workspace_id = NEW.workspace_id
                                  AND target.environment_id = NEW.environment_id
                                  AND target.source_object_id = NEW.source_object_id
                                  AND target.hold_id = NEW.hold_id
                           )
                  INTO targets_complete
                  FROM {SCHEMA}.disclosure_holds AS hold
                 WHERE hold.workspace_id = NEW.workspace_id
                   AND hold.environment_id = NEW.environment_id
                   AND hold.source_object_id = NEW.source_object_id
                   AND hold.hold_id = NEW.hold_id;
                IF targets_complete IS DISTINCT FROM true THEN
                    RAISE EXCEPTION 'Disclosure hold target set is incomplete.'
                        USING ERRCODE = '23514', CONSTRAINT = 'ck_disclosure_hold_targets_complete';
                END IF;
            ELSIF OLD.state <> 'active' OR OLD.generation <> 1
               OR NEW.state <> 'released' OR NEW.generation <> 2
               OR NEW.current_release_id IS NULL THEN
                RAISE EXCEPTION 'Disclosure hold release CAS failed.'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_current_disclosure_hold_step';
            END IF;
            RETURN NEW;
        END; $function$
        """)
    op.execute(
        f"CREATE TRIGGER trg_current_disclosure_hold_step BEFORE INSERT OR UPDATE ON "
        f"{SCHEMA}.current_disclosure_holds FOR EACH ROW EXECUTE FUNCTION "
        f"{SCHEMA}.guard_current_disclosure_hold_step()"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.guard_current_disclosure_hold_step() FROM PUBLIC")


def downgrade() -> None:
    connection = op.get_bind()
    populated = connection.execute(
        sa.text(
            f"SELECT EXISTS (SELECT 1 FROM {SCHEMA}.disclosure_holds LIMIT 1) "
            f"OR EXISTS (SELECT 1 FROM {SCHEMA}.disclosure_hold_releases LIMIT 1)"
        )
    ).scalar_one()
    if populated:
        raise RuntimeError(
            "disclosure hold history is immutable; populated governance tables " "must be retained during rollback"
        )
    op.drop_table("current_disclosure_holds", schema=SCHEMA)
    op.drop_table("disclosure_hold_targets", schema=SCHEMA)
    op.drop_table("disclosure_hold_source_revisions", schema=SCHEMA)
    op.drop_table("disclosure_hold_releases", schema=SCHEMA)
    op.drop_table("disclosure_holds", schema=SCHEMA)
    op.execute(f"DROP FUNCTION {SCHEMA}.guard_current_disclosure_hold_step()")
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_disclosure_hold_releases_mutation()")
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_disclosure_hold_source_revisions_mutation()")
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_disclosure_hold_targets_mutation()")
    op.execute(f"DROP FUNCTION {SCHEMA}.guard_disclosure_hold_target_insert()")
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_disclosure_holds_mutation()")
    op.drop_constraint(
        "uq_lifecycle_decisions_scope_id",
        "lifecycle_decisions",
        schema=SCHEMA,
        type_="unique",
    )
