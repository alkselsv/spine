"""Persist canonical source acceptance and current-head transitions."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261018_21"
down_revision = "20261017_20"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def _uuid_setting(name: str) -> str:
    setting = f"pg_catalog.current_setting('spine.{name}', true)"
    pattern = "^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
    return f"CASE WHEN {setting} OPERATOR(pg_catalog.~) '{pattern}' THEN CAST({setting} AS pg_catalog.uuid) ELSE NULL END"


def _tenant(table: str, *, mutable: bool) -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    qualified = f'{SCHEMA}."{table}"'
    predicate = (
        f"workspace_id = ({_uuid_setting('workspace_id')}) AND "
        f"environment_id = ({_uuid_setting('environment_id')})"
    )
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(
        f'CREATE POLICY "pol_{table}_tenant_isolation" ON {qualified} '
        f"FOR ALL TO {runtime} USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.execute(
        f'CREATE POLICY "pol_{table}_migration_maintenance" ON {qualified} '
        f"FOR ALL TO {migration} USING (true) WITH CHECK (true)"
    )
    grants = "SELECT, INSERT, UPDATE" if mutable else "SELECT, INSERT"
    op.execute(f"GRANT {grants} ON TABLE {qualified} TO {runtime}")
    op.execute(
        f"REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {qualified} FROM {runtime}"
    )


def _immutable(table: str) -> None:
    qualified = f'{SCHEMA}."{table}"'
    function = f"reject_{table}_mutation"
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.{function}() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ BEGIN
            RAISE EXCEPTION 'Canonical acceptance rows are immutable.'
                USING ERRCODE = '23514', CONSTRAINT = 'ck_{table}_immutable';
        END; $function$
        """
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON "
        f"{qualified} FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.{function}()"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.{function}() FROM PUBLIC")


def upgrade() -> None:
    # The lineage trigger must read immutable evidence that the runtime role
    # intentionally cannot SELECT.  Its fixed search_path and value-only
    # comparisons keep the elevated read narrow and non-disclosing.
    op.execute(
        f"ALTER FUNCTION {SCHEMA}.validate_source_receipt_association() "
        "SECURITY DEFINER"
    )
    op.create_unique_constraint(
        "uq_source_revision_provenance_scope_id",
        "source_revision_provenance",
        ["workspace_id", "environment_id", "source_object_id", "provenance_id"],
        schema=SCHEMA,
    )
    op.create_table(
        "canonical_generation_counters",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("current_generation", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "environment_id", name="pk_canonical_generation_counters"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_canonical_generation_counters_environment",
        ),
        sa.CheckConstraint("current_generation >= 0", name="ck_canonical_generation_nonnegative"),
        schema=SCHEMA,
    )
    op.create_table(
        "canonical_acceptances",
        sa.Column("canonical_acceptance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("predecessor_revision_id", postgresql.UUID(as_uuid=True)),
        sa.Column("predecessor_acceptance_id", postgresql.UUID(as_uuid=True)),
        sa.Column("accepted_generation", sa.Integer(), nullable=False),
        sa.Column("ordering", sa.Text(), nullable=False),
        sa.Column("ordering_scheme", sa.Text()),
        sa.Column("ordering_token", sa.Text()),
        sa.Column("admission_decision_id", postgresql.UUID(as_uuid=True)),
        sa.Column("access_policy_version_id", postgresql.UUID(as_uuid=True)),
        sa.Column("provenance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_receipt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("audit_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("outbox_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("canonical_acceptance_id", name="pk_canonical_acceptances"),
        sa.UniqueConstraint("workspace_id", "environment_id", "accepted_generation", name="uq_canonical_acceptances_generation"),
        sa.UniqueConstraint("workspace_id", "environment_id", "source_object_id", "source_revision_id", name="uq_canonical_acceptances_revision"),
        sa.UniqueConstraint("workspace_id", "environment_id", "canonical_acceptance_id", name="uq_canonical_acceptances_scope_id"),
        sa.UniqueConstraint("idempotency_receipt_id", name="uq_canonical_acceptances_idempotency_receipt"),
        sa.UniqueConstraint(
            "workspace_id", "environment_id", "source_object_id",
            "canonical_acceptance_id", "source_revision_id", "accepted_generation",
            name="uq_canonical_acceptances_head_tuple",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id"],
            [f"{SCHEMA}.source_objects.workspace_id", f"{SCHEMA}.source_objects.environment_id", f"{SCHEMA}.source_objects.source_object_id"],
            name="fk_canonical_acceptances_source",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "source_revision_id"],
            [f"{SCHEMA}.source_revisions.workspace_id", f"{SCHEMA}.source_revisions.environment_id", f"{SCHEMA}.source_revisions.source_object_id", f"{SCHEMA}.source_revisions.revision_id"],
            name="fk_canonical_acceptances_revision",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "provenance_id"],
            [f"{SCHEMA}.source_revision_provenance.workspace_id", f"{SCHEMA}.source_revision_provenance.environment_id", f"{SCHEMA}.source_revision_provenance.source_object_id", f"{SCHEMA}.source_revision_provenance.provenance_id"],
            name="fk_canonical_acceptances_provenance",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_revision_id", "admission_decision_id"],
            [f"{SCHEMA}.admission_decisions.workspace_id", f"{SCHEMA}.admission_decisions.environment_id", f"{SCHEMA}.admission_decisions.source_revision_id", f"{SCHEMA}.admission_decisions.admission_decision_id"],
            name="fk_canonical_acceptances_admission",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "access_policy_version_id"],
            [f"{SCHEMA}.access_policy_versions.workspace_id", f"{SCHEMA}.access_policy_versions.environment_id", f"{SCHEMA}.access_policy_versions.source_object_id", f"{SCHEMA}.access_policy_versions.access_policy_version_id"],
            name="fk_canonical_acceptances_policy",
        ),
        sa.ForeignKeyConstraint(
            ["idempotency_receipt_id"],
            [f"{SCHEMA}.idempotency_receipts.receipt_id"],
            name="fk_canonical_acceptances_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["audit_event_id"],
            [f"{SCHEMA}.audit_events.audit_event_id"],
            name="fk_canonical_acceptances_audit",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["outbox_event_id"],
            [f"{SCHEMA}.outbox_intents.event_id"],
            name="fk_canonical_acceptances_outbox",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint("accepted_generation >= 1", name="ck_canonical_acceptances_generation"),
        sa.CheckConstraint("ordering IN ('trusted', 'expected_current')", name="ck_canonical_acceptances_ordering"),
        sa.CheckConstraint("(ordering_scheme IS NULL) = (ordering_token IS NULL)", name="ck_canonical_acceptances_order_pair"),
        sa.CheckConstraint("(ordering = 'trusted' AND ordering_scheme IS NOT NULL) OR (ordering = 'expected_current' AND ordering_scheme IS NULL)", name="ck_canonical_acceptances_ordering_shape"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_canonical_acceptances_source_generation",
        "canonical_acceptances",
        ["workspace_id", "environment_id", "source_object_id", "accepted_generation"],
        schema=SCHEMA,
    )
    op.create_table(
        "current_source_heads",
        sa.Column("source_object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("current_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("current_revision_kind", sa.Text(), nullable=False),
        sa.Column("acceptance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("accepted_generation", sa.Integer(), nullable=False),
        sa.Column("cas_version", sa.Integer(), nullable=False),
        sa.Column("ordering_scheme", sa.Text()),
        sa.Column("ordering_token", sa.Text()),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("source_object_id", name="pk_current_source_heads"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "current_revision_id"],
            [f"{SCHEMA}.source_revisions.workspace_id", f"{SCHEMA}.source_revisions.environment_id", f"{SCHEMA}.source_revisions.source_object_id", f"{SCHEMA}.source_revisions.revision_id"],
            name="fk_current_source_heads_revision",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "acceptance_id"],
            [f"{SCHEMA}.canonical_acceptances.workspace_id", f"{SCHEMA}.canonical_acceptances.environment_id", f"{SCHEMA}.canonical_acceptances.canonical_acceptance_id"],
            name="fk_current_source_heads_acceptance",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "acceptance_id", "current_revision_id", "accepted_generation"],
            [f"{SCHEMA}.canonical_acceptances.workspace_id", f"{SCHEMA}.canonical_acceptances.environment_id", f"{SCHEMA}.canonical_acceptances.source_object_id", f"{SCHEMA}.canonical_acceptances.canonical_acceptance_id", f"{SCHEMA}.canonical_acceptances.source_revision_id", f"{SCHEMA}.canonical_acceptances.accepted_generation"],
            name="fk_current_source_heads_acceptance_tuple",
        ),
        sa.CheckConstraint("current_revision_kind IN ('content', 'tombstone')", name="ck_current_source_heads_kind"),
        sa.CheckConstraint("accepted_generation >= 1 AND cas_version >= 1", name="ck_current_source_heads_versions"),
        sa.CheckConstraint("(ordering_scheme IS NULL) = (ordering_token IS NULL)", name="ck_current_source_heads_order_pair"),
        schema=SCHEMA,
    )
    for table in ("canonical_generation_counters", "canonical_acceptances", "current_source_heads"):
        _tenant(table, mutable=table != "canonical_acceptances")
    _immutable("canonical_acceptances")
    op.execute(
        f"CREATE CONSTRAINT TRIGGER trg_canonical_acceptances_receipt_scope "
        f"AFTER INSERT OR UPDATE OF workspace_id, environment_id, idempotency_receipt_id "
        f"ON {SCHEMA}.canonical_acceptances DEFERRABLE INITIALLY DEFERRED "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.validate_issue7_receipt_scope()"
    )
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.guard_canonical_generation_step() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ BEGIN
            IF NEW.current_generation <> OLD.current_generation + 1 THEN
                RAISE EXCEPTION 'Canonical generation must increase by one.'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_canonical_generation_step';
            END IF;
            RETURN NEW;
        END; $function$
        """
    )
    op.execute(
        f"CREATE TRIGGER trg_canonical_generation_step BEFORE UPDATE ON "
        f"{SCHEMA}.canonical_generation_counters FOR EACH ROW EXECUTE FUNCTION "
        f"{SCHEMA}.guard_canonical_generation_step()"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.guard_canonical_generation_step() FROM PUBLIC")
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.guard_current_source_head_step() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ BEGIN
            IF NEW.cas_version <> OLD.cas_version + 1
               OR NEW.accepted_generation <= OLD.accepted_generation
               OR NEW.source_object_id <> OLD.source_object_id THEN
                RAISE EXCEPTION 'Current source head transition is not monotonic.'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_current_source_heads_step';
            END IF;
            RETURN NEW;
        END; $function$
        """
    )
    op.execute(
        f"CREATE TRIGGER trg_current_source_head_step BEFORE UPDATE ON "
        f"{SCHEMA}.current_source_heads FOR EACH ROW EXECUTE FUNCTION "
        f"{SCHEMA}.guard_current_source_head_step()"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.guard_current_source_head_step() FROM PUBLIC")
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.guard_canonical_acceptance_predecessor() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ DECLARE current_revision uuid; current_acceptance uuid;
        BEGIN
            SELECT current_revision_id, acceptance_id
              INTO current_revision, current_acceptance
              FROM {SCHEMA}.current_source_heads
             WHERE source_object_id = NEW.source_object_id
               AND workspace_id = NEW.workspace_id
               AND environment_id = NEW.environment_id
             FOR KEY SHARE;
            IF NOT FOUND THEN
                IF NEW.predecessor_revision_id IS NOT NULL OR NEW.predecessor_acceptance_id IS NOT NULL THEN
                    RAISE EXCEPTION 'Canonical predecessor is not empty for a first acceptance.'
                        USING ERRCODE = '23514', CONSTRAINT = 'ck_canonical_acceptances_predecessor';
                END IF;
            ELSIF NEW.predecessor_revision_id IS DISTINCT FROM current_revision
               OR NEW.predecessor_acceptance_id IS DISTINCT FROM current_acceptance THEN
                RAISE EXCEPTION 'Canonical predecessor does not match current source head.'
                    USING ERRCODE = '40001', CONSTRAINT = 'ck_canonical_acceptances_predecessor';
            END IF;
            RETURN NEW;
        END; $function$
        """)
    op.execute(
        f"CREATE TRIGGER trg_canonical_acceptances_predecessor BEFORE INSERT ON "
        f"{SCHEMA}.canonical_acceptances FOR EACH ROW EXECUTE FUNCTION "
        f"{SCHEMA}.guard_canonical_acceptance_predecessor()"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.guard_canonical_acceptance_predecessor() FROM PUBLIC"
    )


def downgrade() -> None:
    runtime = _role("runtime_role")
    op.drop_constraint(
        "fk_canonical_acceptances_outbox",
        "canonical_acceptances",
        schema=SCHEMA,
        type_="foreignkey",
    )
    for table in (
        "current_source_heads",
        "canonical_acceptances",
        "canonical_generation_counters",
    ):
        qualified = f'{SCHEMA}."{table}"'
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {qualified} FROM {runtime}")
        op.drop_table(table, schema=SCHEMA)
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_canonical_acceptances_mutation()")
    op.execute(f"DROP FUNCTION {SCHEMA}.guard_canonical_acceptance_predecessor()")
    op.execute(f"DROP FUNCTION {SCHEMA}.guard_current_source_head_step()")
    op.execute(f"DROP FUNCTION {SCHEMA}.guard_canonical_generation_step()")
    op.execute(
        f"ALTER FUNCTION {SCHEMA}.validate_source_receipt_association() "
        "SECURITY INVOKER"
    )
    op.drop_constraint(
        "uq_source_revision_provenance_scope_id",
        "source_revision_provenance",
        schema=SCHEMA,
        type_="unique",
    )
