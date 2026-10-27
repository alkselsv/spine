"""Persist immutable register link claims and accepted exact-revision links."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings

revision = "20261201_69"
down_revision = "20261130_68"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def _tenant_immutable(table: str) -> None:
    runtime, migration = _role("runtime_role"), _role("migration_role")
    qualified = f'{SCHEMA}."{table}"'
    workspace_setting = "pg_catalog.current_setting('spine.workspace_id', true)"
    environment_setting = "pg_catalog.current_setting('spine.environment_id', true)"
    uuid_pattern = (
        "^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
    )
    workspace = f"CASE WHEN {workspace_setting} OPERATOR(pg_catalog.~) '{uuid_pattern}' THEN CAST({workspace_setting} AS pg_catalog.uuid) ELSE NULL END"
    environment = f"CASE WHEN {environment_setting} OPERATOR(pg_catalog.~) '{uuid_pattern}' THEN CAST({environment_setting} AS pg_catalog.uuid) ELSE NULL END"
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(
        f'CREATE POLICY "pol_{table}_tenant_isolation" ON {qualified} '
        f"FOR ALL TO {runtime} USING (workspace_id = ({workspace}) AND environment_id = ({environment})) "
        f"WITH CHECK (workspace_id = ({workspace}) AND environment_id = ({environment}))"
    )
    op.execute(
        f'CREATE POLICY "pol_{table}_migration_maintenance" ON {qualified} '
        f"FOR ALL TO {migration} USING (true) WITH CHECK (true)"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE {qualified} TO {runtime}")
    op.execute(
        f"REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {qualified} FROM {runtime}"
    )
    function = f"reject_{table}_mutation"
    op.execute(
        f"CREATE FUNCTION {SCHEMA}.{function}() RETURNS trigger LANGUAGE plpgsql "
        f"SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA} AS $function$ BEGIN "
        "RAISE EXCEPTION 'Register association evidence is immutable.' USING ERRCODE = '23514'; "
        "END; $function$"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {qualified} FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.{function}()"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.{function}() FROM PUBLIC")


def upgrade() -> None:
    op.create_table(
        "register_link_claims",
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "row_source_object_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "row_source_revision_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("provenance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("claim_id", name="pk_register_link_claims"),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "claim_id",
            name="uq_register_link_claims_scope_id",
        ),
        sa.ForeignKeyConstraint(
            [
                "workspace_id",
                "environment_id",
                "row_source_object_id",
                "row_source_revision_id",
            ],
            [
                f"{SCHEMA}.source_revisions.workspace_id",
                f"{SCHEMA}.source_revisions.environment_id",
                f"{SCHEMA}.source_revisions.source_object_id",
                f"{SCHEMA}.source_revisions.revision_id",
            ],
            name="fk_register_link_claims_row_revision",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "row_source_object_id", "provenance_id"],
            [
                f"{SCHEMA}.source_revision_provenance.workspace_id",
                f"{SCHEMA}.source_revision_provenance.environment_id",
                f"{SCHEMA}.source_revision_provenance.source_object_id",
                f"{SCHEMA}.source_revision_provenance.provenance_id",
            ],
            name="fk_register_link_claims_provenance",
        ),
        sa.CheckConstraint(
            "role IN ('request', 'meeting', 'client', 'project', 'historical_estimate', 'source_estimate', 'final_estimate', 'drive_material')",
            name=op.f("ck_register_link_claims_role"),
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "register_association_acceptances",
        sa.Column("acceptance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "row_source_object_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "row_source_revision_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "target_source_object_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "target_source_revision_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "acceptance_id", name="pk_register_association_acceptances"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "claim_id",
            name="uq_register_association_acceptances_claim",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "claim_id"],
            [
                f"{SCHEMA}.register_link_claims.workspace_id",
                f"{SCHEMA}.register_link_claims.environment_id",
                f"{SCHEMA}.register_link_claims.claim_id",
            ],
            name="fk_register_association_acceptances_claim",
        ),
        sa.ForeignKeyConstraint(
            [
                "workspace_id",
                "environment_id",
                "row_source_object_id",
                "row_source_revision_id",
            ],
            [
                f"{SCHEMA}.canonical_acceptances.workspace_id",
                f"{SCHEMA}.canonical_acceptances.environment_id",
                f"{SCHEMA}.canonical_acceptances.source_object_id",
                f"{SCHEMA}.canonical_acceptances.source_revision_id",
            ],
            name="fk_register_association_acceptances_row_accepted",
        ),
        sa.ForeignKeyConstraint(
            [
                "workspace_id",
                "environment_id",
                "target_source_object_id",
                "target_source_revision_id",
            ],
            [
                f"{SCHEMA}.canonical_acceptances.workspace_id",
                f"{SCHEMA}.canonical_acceptances.environment_id",
                f"{SCHEMA}.canonical_acceptances.source_object_id",
                f"{SCHEMA}.canonical_acceptances.source_revision_id",
            ],
            name="fk_register_association_acceptances_target_accepted",
        ),
        sa.CheckConstraint(
            "row_source_object_id <> target_source_object_id",
            name=op.f("ck_register_association_acceptances_distinct_sources"),
        ),
        sa.CheckConstraint(
            "role IN ('request', 'meeting', 'client', 'project', 'historical_estimate', 'source_estimate', 'final_estimate', 'drive_material')",
            name=op.f("ck_register_association_acceptances_role"),
        ),
        schema=SCHEMA,
    )
    for table in ("register_link_claims", "register_association_acceptances"):
        _tenant_immutable(table)


def downgrade() -> None:
    for table in ("register_association_acceptances", "register_link_claims"):
        op.drop_table(table, schema=SCHEMA)
        op.execute(f"DROP FUNCTION {SCHEMA}.reject_{table}_mutation()")
