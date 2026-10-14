"""Persist PostgreSQL-owned projection activation, pointers, and drift."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261022_26"
down_revision = "20261021_25"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def _tenant(table: str, *, allow_update: bool = False) -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    qualified = f'{SCHEMA}."{table}"'
    predicate = (
        "workspace_id = (CASE WHEN pg_catalog.current_setting('spine.workspace_id', true) "
        "OPERATOR(pg_catalog.~) '^[0-9A-Fa-f-]{36}$' THEN "
        "CAST(pg_catalog.current_setting('spine.workspace_id', true) AS pg_catalog.uuid) ELSE NULL END) "
        "AND environment_id = (CASE WHEN pg_catalog.current_setting('spine.environment_id', true) "
        "OPERATOR(pg_catalog.~) '^[0-9A-Fa-f-]{36}$' THEN "
        "CAST(pg_catalog.current_setting('spine.environment_id', true) AS pg_catalog.uuid) ELSE NULL END)"
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
    privileges = "SELECT, INSERT, UPDATE" if allow_update else "SELECT, INSERT"
    op.execute(f"GRANT {privileges} ON TABLE {qualified} TO {runtime}")
    op.execute(f"REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {qualified} FROM {runtime}")


def _immutable(table: str) -> None:
    qualified = f'{SCHEMA}."{table}"'
    function = f"reject_{table}_mutation"
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.{function}() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ BEGIN
            RAISE EXCEPTION 'Projection publication records are immutable.'
                USING ERRCODE = '23514', CONSTRAINT = 'ck_{table}_immutable';
        END; $function$
        """
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON "
        f"{qualified} FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.{function}()"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.{function}() FROM PUBLIC")


def _receipt_scope_guard(table: str) -> None:
    op.execute(
        f"CREATE CONSTRAINT TRIGGER trg_{table}_receipt_scope "
        f"AFTER INSERT OR UPDATE OF workspace_id, environment_id, idempotency_receipt_id "
        f"ON {SCHEMA}.\"{table}\" DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        f"EXECUTE FUNCTION {SCHEMA}.validate_issue7_receipt_scope()"
    )


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    digest = sa.String(64)

    op.add_column(
        "projection_runs",
        sa.Column("predecessor_snapshot_id", uuid),
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_projection_runs_predecessor_snapshot",
        "projection_runs",
        "projection_snapshots",
        ["workspace_id", "environment_id", "predecessor_snapshot_id"],
        ["workspace_id", "environment_id", "projection_snapshot_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )

    op.create_table(
        "projection_activations",
        sa.Column("projection_activation_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("projection_kind", sa.Text(), nullable=False),
        sa.Column("projection_snapshot_id", uuid, nullable=False),
        sa.Column("predecessor_snapshot_id", uuid),
        sa.Column("activation_generation", sa.Integer(), nullable=False),
        sa.Column("activation_kind", sa.Text(), nullable=False),
        sa.Column("command_identity", sa.Text(), nullable=False),
        sa.Column("command_digest", digest, nullable=False),
        sa.Column("idempotency_receipt_id", uuid, nullable=False),
        sa.Column("audit_event_id", uuid, nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("projection_activation_id", name="pk_projection_activations"),
        sa.UniqueConstraint("workspace_id", "environment_id", "projection_activation_id", name="uq_projection_activations_scope_id"),
        sa.UniqueConstraint("workspace_id", "environment_id", "projection_kind", "activation_generation", name="uq_projection_activations_generation"),
        sa.UniqueConstraint("workspace_id", "environment_id", "projection_kind", "projection_snapshot_id", name="uq_projection_activations_snapshot"),
        sa.UniqueConstraint("workspace_id", "environment_id", "projection_kind", "projection_activation_id", "projection_snapshot_id", "activation_generation", name="uq_projection_activations_pointer_target"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "projection_snapshot_id"],
            [f"{SCHEMA}.projection_snapshots.workspace_id", f"{SCHEMA}.projection_snapshots.environment_id", f"{SCHEMA}.projection_snapshots.projection_snapshot_id"],
            name="fk_projection_activations_snapshot",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "predecessor_snapshot_id"],
            [f"{SCHEMA}.projection_snapshots.workspace_id", f"{SCHEMA}.projection_snapshots.environment_id", f"{SCHEMA}.projection_snapshots.projection_snapshot_id"],
            name="fk_projection_activations_predecessor",
        ),
        sa.ForeignKeyConstraint(["idempotency_receipt_id"], [f"{SCHEMA}.idempotency_receipts.receipt_id"], name="fk_projection_activations_idempotency"),
        sa.ForeignKeyConstraint(["audit_event_id"], [f"{SCHEMA}.audit_events.audit_event_id"], name="fk_projection_activations_audit"),
        sa.CheckConstraint("activation_generation >= 1", name="ck_projection_activations_generation"),
        sa.CheckConstraint("activation_kind IN ('publish', 'rollback')", name="ck_projection_activations_kind"),
        sa.CheckConstraint("command_digest ~ '^[0-9a-f]{64}$'", name="ck_projection_activations_digest"),
        schema=SCHEMA,
    )
    op.create_table(
        "active_projections",
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("projection_kind", sa.Text(), nullable=False),
        sa.Column("projection_snapshot_id", uuid, nullable=False),
        sa.Column("projection_activation_id", uuid, nullable=False),
        sa.Column("activation_generation", sa.Integer(), nullable=False),
        sa.Column("cas_version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "environment_id", "projection_kind", name="pk_active_projections"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "projection_kind", "projection_activation_id", "projection_snapshot_id", "activation_generation"],
            [f"{SCHEMA}.projection_activations.workspace_id", f"{SCHEMA}.projection_activations.environment_id", f"{SCHEMA}.projection_activations.projection_kind", f"{SCHEMA}.projection_activations.projection_activation_id", f"{SCHEMA}.projection_activations.projection_snapshot_id", f"{SCHEMA}.projection_activations.activation_generation"],
            name="fk_active_projections_activation",
        ),
        sa.CheckConstraint("activation_generation >= 1", name="ck_active_projections_generation"),
        sa.CheckConstraint("cas_version >= 1", name="ck_active_projections_cas"),
        schema=SCHEMA,
    )
    op.create_table(
        "projection_drifts",
        sa.Column("projection_drift_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("projection_kind", sa.Text(), nullable=False),
        sa.Column("projection_snapshot_id", uuid, nullable=False),
        sa.Column("projection_activation_id", uuid, nullable=False),
        sa.Column("activation_generation", sa.Integer(), nullable=False),
        sa.Column("canonical_acceptance_id", uuid, nullable=False),
        sa.Column("source_object_id", uuid, nullable=False),
        sa.Column("source_revision_id", uuid, nullable=False),
        sa.Column("accepted_generation", sa.Integer(), nullable=False),
        sa.Column("detected_by", sa.Text(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("projection_drift_id", name="pk_projection_drifts"),
        sa.UniqueConstraint("workspace_id", "environment_id", "projection_drift_id", name="uq_projection_drifts_scope_id"),
        sa.UniqueConstraint("workspace_id", "environment_id", "projection_kind", "activation_generation", "canonical_acceptance_id", name="uq_projection_drifts_activation_acceptance"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "projection_kind", "projection_activation_id", "projection_snapshot_id", "activation_generation"],
            [f"{SCHEMA}.projection_activations.workspace_id", f"{SCHEMA}.projection_activations.environment_id", f"{SCHEMA}.projection_activations.projection_kind", f"{SCHEMA}.projection_activations.projection_activation_id", f"{SCHEMA}.projection_activations.projection_snapshot_id", f"{SCHEMA}.projection_activations.activation_generation"],
            name="fk_projection_drifts_activation",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "canonical_acceptance_id", "source_revision_id", "accepted_generation"],
            [f"{SCHEMA}.canonical_acceptances.workspace_id", f"{SCHEMA}.canonical_acceptances.environment_id", f"{SCHEMA}.canonical_acceptances.source_object_id", f"{SCHEMA}.canonical_acceptances.canonical_acceptance_id", f"{SCHEMA}.canonical_acceptances.source_revision_id", f"{SCHEMA}.canonical_acceptances.accepted_generation"],
            name="fk_projection_drifts_acceptance",
        ),
        sa.CheckConstraint("activation_generation >= 1", name="ck_projection_drifts_activation_generation"),
        sa.CheckConstraint("accepted_generation >= 1", name="ck_projection_drifts_accepted_generation"),
        sa.CheckConstraint("detected_by IN ('activation', 'canonical_acceptance')", name="ck_projection_drifts_detected_by"),
        schema=SCHEMA,
    )

    for table, mutable in (
        ("projection_activations", False),
        ("active_projections", True),
        ("projection_drifts", False),
    ):
        _tenant(table, allow_update=mutable)
    _receipt_scope_guard("projection_activations")
    _immutable("projection_activations")
    _immutable("projection_drifts")
    op.create_index("ix_projection_activations_kind_generation", "projection_activations", ["workspace_id", "environment_id", "projection_kind", "activation_generation"], schema=SCHEMA)
    op.create_index("ix_active_projections_snapshot_generation", "active_projections", ["workspace_id", "environment_id", "projection_snapshot_id", "activation_generation"], schema=SCHEMA)
    op.create_index("ix_projection_drifts_source", "projection_drifts", ["workspace_id", "environment_id", "source_object_id", "accepted_generation"], schema=SCHEMA)

    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.guard_projection_activation_generation() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE previous integer;
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM {SCHEMA}.projection_snapshots
                WHERE workspace_id = NEW.workspace_id AND environment_id = NEW.environment_id
                  AND projection_snapshot_id = NEW.projection_snapshot_id
                  AND snapshot_state = 'finalized'
            ) THEN
                RAISE EXCEPTION 'Projection activation requires a finalized snapshot.' USING ERRCODE = '23514';
            END IF;
            SELECT COALESCE(MAX(activation_generation), 0) INTO previous
              FROM {SCHEMA}.projection_activations
             WHERE workspace_id = NEW.workspace_id AND environment_id = NEW.environment_id
               AND projection_kind = NEW.projection_kind;
            IF NEW.activation_generation <> previous + 1 THEN
                RAISE EXCEPTION 'Projection activation generation must be monotonic.' USING ERRCODE = '40001';
            END IF;
            RETURN NEW;
        END; $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.guard_projection_activation_generation() FROM PUBLIC")
    op.execute(f"CREATE TRIGGER trg_projection_activations_generation BEFORE INSERT ON {SCHEMA}.projection_activations FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_projection_activation_generation()")
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.guard_projection_drift_boundary() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE boundary_generation integer;
        DECLARE boundary_scope text;
        BEGIN
            SELECT boundary.accepted_through_generation, boundary.scope_version
              INTO boundary_generation, boundary_scope
              FROM {SCHEMA}.projection_snapshots AS snapshot
              JOIN {SCHEMA}.canonical_boundaries AS boundary
                ON boundary.workspace_id = snapshot.workspace_id
               AND boundary.environment_id = snapshot.environment_id
               AND boundary.boundary_id = snapshot.canonical_boundary_id
             WHERE snapshot.workspace_id = NEW.workspace_id
               AND snapshot.environment_id = NEW.environment_id
               AND snapshot.projection_snapshot_id = NEW.projection_snapshot_id;
            IF boundary_generation IS NULL OR boundary_scope <> 'r1'
               OR NEW.accepted_generation <= boundary_generation THEN
                RAISE EXCEPTION 'Projection drift must be beyond its canonical boundary.'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END; $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.guard_projection_drift_boundary() FROM PUBLIC")
    op.execute(
        f"CREATE TRIGGER trg_projection_drifts_boundary BEFORE INSERT ON {SCHEMA}.projection_drifts "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_projection_drift_boundary()"
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER trg_projection_activations_generation ON {SCHEMA}.projection_activations")
    op.execute(f"DROP FUNCTION {SCHEMA}.guard_projection_activation_generation()")
    op.execute(f"DROP TRIGGER trg_projection_drifts_boundary ON {SCHEMA}.projection_drifts")
    op.execute(f"DROP FUNCTION {SCHEMA}.guard_projection_drift_boundary()")
    for table in ("projection_drifts", "active_projections", "projection_activations"):
        if table != "active_projections":
            op.execute(f"DROP TRIGGER trg_{table}_immutable ON {SCHEMA}.\"{table}\"")
            op.execute(f"DROP FUNCTION {SCHEMA}.reject_{table}_mutation()")
        op.drop_table(table, schema=SCHEMA)
    op.drop_constraint("fk_projection_runs_predecessor_snapshot", "projection_runs", schema=SCHEMA, type_="foreignkey")
    op.drop_column("projection_runs", "predecessor_snapshot_id", schema=SCHEMA)
