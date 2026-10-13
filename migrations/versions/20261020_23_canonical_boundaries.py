"""Persist server-selected canonical boundaries and exact membership."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261020_23"
down_revision = "20261019_22"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def _tenant(table: str) -> None:
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
    op.execute(f"GRANT SELECT, INSERT ON TABLE {qualified} TO {runtime}")
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {qualified} FROM {runtime}")


def _immutable(table: str, message: str) -> None:
    qualified = f'{SCHEMA}."{table}"'
    function = f"reject_{table}_mutation"
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.{function}() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ BEGIN
            RAISE EXCEPTION '{message}'
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
    op.create_table(
        "canonical_boundaries",
        sa.Column("boundary_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("projection_kind", sa.Text(), nullable=False),
        sa.Column("scope_version", sa.Text(), nullable=False),
        sa.Column("accepted_through_generation", sa.Integer(), nullable=False),
        sa.Column("boundary_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("manifest_digest", sa.String(64), nullable=False),
        sa.Column("idempotency_receipt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("boundary_id", name="pk_canonical_boundaries"),
        sa.UniqueConstraint(
            "workspace_id", "environment_id", "boundary_id",
            name="uq_canonical_boundaries_scope_id",
        ),
        sa.UniqueConstraint(
            "idempotency_receipt_id", name="uq_canonical_boundaries_idempotency_receipt"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_canonical_boundaries_environment",
        ),
        sa.ForeignKeyConstraint(
            ["idempotency_receipt_id"],
            [f"{SCHEMA}.idempotency_receipts.receipt_id"],
            name="fk_canonical_boundaries_idempotency",
        ),
        sa.CheckConstraint(
            "accepted_through_generation >= 0",
            name="ck_canonical_boundaries_generation",
        ),
        sa.CheckConstraint(
            "projection_kind ~ '^[a-z][a-z0-9_.:-]{0,63}$'",
            name="ck_canonical_boundaries_projection_kind",
        ),
        sa.CheckConstraint(
            "scope_version ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'",
            name="ck_canonical_boundaries_scope_version",
        ),
        sa.CheckConstraint(
            "manifest_digest ~ '^[0-9a-f]{64}$'",
            name="ck_canonical_boundaries_digest",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "canonical_boundary_members",
        sa.Column("boundary_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("acceptance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_kind", sa.Text(), nullable=False),
        sa.Column("accepted_generation", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint(
            "boundary_id", "source_object_id", name="pk_canonical_boundary_members"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "boundary_id"],
            [
                f"{SCHEMA}.canonical_boundaries.workspace_id",
                f"{SCHEMA}.canonical_boundaries.environment_id",
                f"{SCHEMA}.canonical_boundaries.boundary_id",
            ],
            name="fk_canonical_boundary_members_boundary",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id"],
            [
                f"{SCHEMA}.source_objects.workspace_id",
                f"{SCHEMA}.source_objects.environment_id",
                f"{SCHEMA}.source_objects.source_object_id",
            ],
            name="fk_canonical_boundary_members_source",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "source_revision_id"],
            [
                f"{SCHEMA}.source_revisions.workspace_id",
                f"{SCHEMA}.source_revisions.environment_id",
                f"{SCHEMA}.source_revisions.source_object_id",
                f"{SCHEMA}.source_revisions.revision_id",
            ],
            name="fk_canonical_boundary_members_revision",
        ),
        sa.ForeignKeyConstraint(
            [
                "workspace_id", "environment_id", "source_object_id", "acceptance_id",
                "source_revision_id", "accepted_generation",
            ],
            [
                f"{SCHEMA}.canonical_acceptances.workspace_id",
                f"{SCHEMA}.canonical_acceptances.environment_id",
                f"{SCHEMA}.canonical_acceptances.source_object_id",
                f"{SCHEMA}.canonical_acceptances.canonical_acceptance_id",
                f"{SCHEMA}.canonical_acceptances.source_revision_id",
                f"{SCHEMA}.canonical_acceptances.accepted_generation",
            ],
            name="fk_canonical_boundary_members_acceptance",
        ),
        sa.CheckConstraint(
            "revision_kind IN ('content', 'tombstone')",
            name="ck_canonical_boundary_members_kind",
        ),
        sa.CheckConstraint(
            "accepted_generation >= 1",
            name="ck_canonical_boundary_members_generation",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_canonical_boundary_members_scope_source",
        "canonical_boundary_members",
        ["workspace_id", "environment_id", "source_object_id"],
        schema=SCHEMA,
    )
    for table in ("canonical_boundaries", "canonical_boundary_members"):
        _tenant(table)
    _immutable("canonical_boundaries", "Canonical boundaries are immutable.")
    _immutable("canonical_boundary_members", "Canonical boundary members are immutable.")
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.guard_canonical_boundary_member_insert() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ DECLARE boundary_xid xid; BEGIN
            SELECT xmin INTO boundary_xid
              FROM {SCHEMA}.canonical_boundaries
             WHERE boundary_id = NEW.boundary_id
               AND workspace_id = NEW.workspace_id
               AND environment_id = NEW.environment_id;
            IF boundary_xid IS NULL OR boundary_xid::text <> pg_catalog.txid_current()::text THEN
                RAISE EXCEPTION 'Boundary membership can only be created with its boundary.'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_canonical_boundary_members_same_transaction';
            END IF;
            RETURN NEW;
        END; $function$
        """
    )
    op.execute(
        f"CREATE TRIGGER trg_canonical_boundary_member_insert BEFORE INSERT ON "
        f"{SCHEMA}.canonical_boundary_members FOR EACH ROW EXECUTE FUNCTION "
        f"{SCHEMA}.guard_canonical_boundary_member_insert()"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.guard_canonical_boundary_member_insert() FROM PUBLIC"
    )


def downgrade() -> None:
    runtime = _role("runtime_role")
    for table in ("canonical_boundary_members", "canonical_boundaries"):
        qualified = f'{SCHEMA}."{table}"'
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {qualified} FROM {runtime}")
    op.execute(
        f"DROP TRIGGER trg_canonical_boundary_member_insert ON "
        f"{SCHEMA}.canonical_boundary_members"
    )
    op.execute(
        f"DROP TRIGGER trg_canonical_boundaries_immutable ON "
        f"{SCHEMA}.canonical_boundaries"
    )
    op.execute(
        f"DROP TRIGGER trg_canonical_boundary_members_immutable ON "
        f"{SCHEMA}.canonical_boundary_members"
    )
    op.execute(
        f"DROP FUNCTION {SCHEMA}.guard_canonical_boundary_member_insert()"
    )
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_canonical_boundary_members_mutation()")
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_canonical_boundaries_mutation()")
    op.drop_table("canonical_boundary_members", schema=SCHEMA)
    op.drop_table("canonical_boundaries", schema=SCHEMA)
