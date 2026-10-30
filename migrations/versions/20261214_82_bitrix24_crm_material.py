"""Persist exact Bitrix24 CRM activity-to-Disk material evidence."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261214_82"
down_revision = "20261213_81"
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
    function = f"reject_{table}_mutation"
    op.execute(f'DROP POLICY IF EXISTS "pol_{table}_tenant_isolation" ON {qualified}')
    op.execute(f'DROP POLICY IF EXISTS "pol_{table}_migration_maintenance" ON {qualified}')
    op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {qualified}")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.{function}()")
    ws_setting = "pg_catalog.current_setting('spine.workspace_id', true)"
    env_setting = "pg_catalog.current_setting('spine.environment_id', true)"
    uuid_pattern = "^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
    ws = f"CASE WHEN {ws_setting} OPERATOR(pg_catalog.~) '{uuid_pattern}' THEN CAST({ws_setting} AS pg_catalog.uuid) ELSE NULL END"
    env = f"CASE WHEN {env_setting} OPERATOR(pg_catalog.~) '{uuid_pattern}' THEN CAST({env_setting} AS pg_catalog.uuid) ELSE NULL END"
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(
        f'CREATE POLICY "pol_{table}_tenant_isolation" ON {qualified} '
        f"FOR ALL TO {runtime} USING (workspace_id = ({ws}) AND environment_id = ({env})) "
        f"WITH CHECK (workspace_id = ({ws}) AND environment_id = ({env}))"
    )
    op.execute(
        f'CREATE POLICY "pol_{table}_migration_maintenance" ON {qualified} '
        f"FOR ALL TO {migration} USING (true) WITH CHECK (true)"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE {qualified} TO {runtime}")
    op.execute(
        f"REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {qualified} FROM {runtime}"
    )
    op.execute(
        f"CREATE FUNCTION {SCHEMA}.{function}() RETURNS trigger LANGUAGE plpgsql "
        f"SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA} AS $function$ BEGIN "
        "RAISE EXCEPTION 'Bitrix24 CRM material evidence is immutable.' USING ERRCODE = '23514'; "
        "END; $function$"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {qualified} "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.{function}()"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.{function}() FROM PUBLIC")


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "bitrix24_crm_material_claims",
        sa.Column("claim_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("target_source_object_id", uuid, nullable=False),
        sa.Column("target_source_revision_id", uuid, nullable=False),
        sa.Column("disk_file_id", sa.Text(), nullable=False),
        sa.Column("disk_file_revision", sa.Text(), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("claim_id", name="pk_bitrix24_crm_material_claims"),
        sa.UniqueConstraint("workspace_id", "environment_id", "claim_id", name="uq_bitrix24_crm_material_claim_scope_id"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "target_source_object_id", "target_source_revision_id"],
            [f"{SCHEMA}.source_revisions.workspace_id", f"{SCHEMA}.source_revisions.environment_id", f"{SCHEMA}.source_revisions.source_object_id", f"{SCHEMA}.source_revisions.revision_id"],
            name="fk_bitrix24_crm_material_claim_target_revision",
        ),
        schema=SCHEMA,
        if_not_exists=True,
    )
    op.create_table(
        "bitrix24_crm_material_acceptances",
        sa.Column("acceptance_id", uuid, nullable=False),
        sa.Column("claim_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("target_source_object_id", uuid, nullable=False),
        sa.Column("target_source_revision_id", uuid, nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("acceptance_id", name="pk_bitrix24_crm_material_acceptances"),
        sa.UniqueConstraint("workspace_id", "environment_id", "claim_id", name="uq_bitrix24_crm_material_acceptance_claim"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "claim_id"],
            [f"{SCHEMA}.bitrix24_crm_material_claims.workspace_id", f"{SCHEMA}.bitrix24_crm_material_claims.environment_id", f"{SCHEMA}.bitrix24_crm_material_claims.claim_id"],
            name="fk_bitrix24_crm_material_acceptance_claim",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "target_source_object_id", "target_source_revision_id"],
            [f"{SCHEMA}.canonical_acceptances.workspace_id", f"{SCHEMA}.canonical_acceptances.environment_id", f"{SCHEMA}.canonical_acceptances.source_object_id", f"{SCHEMA}.canonical_acceptances.source_revision_id"],
            name="fk_bitrix24_crm_material_acceptance_target",
        ),
        schema=SCHEMA,
        if_not_exists=True,
    )
    op.create_table(
        "bitrix24_crm_material_revocations",
        sa.Column("revocation_id", uuid, nullable=False),
        sa.Column("claim_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("revocation_id", name="pk_bitrix24_crm_material_revocations"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "claim_id"],
            [f"{SCHEMA}.bitrix24_crm_material_claims.workspace_id", f"{SCHEMA}.bitrix24_crm_material_claims.environment_id", f"{SCHEMA}.bitrix24_crm_material_claims.claim_id"],
            name="fk_bitrix24_crm_material_revocation_claim",
        ),
        sa.CheckConstraint(
            "state IN ('relation_revoked', 'source_revoked', 'inaccessible')",
            name=op.f("ck_bitrix24_crm_material_revocation_state"),
        ),
        schema=SCHEMA,
        if_not_exists=True,
    )
    _tenant_immutable("bitrix24_crm_material_claims")
    _tenant_immutable("bitrix24_crm_material_acceptances")
    _tenant_immutable("bitrix24_crm_material_revocations")


def downgrade() -> None:
    for table in (
        "bitrix24_crm_material_revocations",
        "bitrix24_crm_material_acceptances",
        "bitrix24_crm_material_claims",
    ):
        qualified = f'{SCHEMA}."{table}"'
        op.execute(f'DROP POLICY IF EXISTS "pol_{table}_tenant_isolation" ON {qualified}')
        op.execute(f'DROP POLICY IF EXISTS "pol_{table}_migration_maintenance" ON {qualified}')
        op.drop_table(table, schema=SCHEMA)
        op.execute(f"DROP FUNCTION {SCHEMA}.reject_{table}_mutation()")
