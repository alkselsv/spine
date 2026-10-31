"""Persist immutable canonical acceptance selectors and CAS heads."""

import sqlalchemy as sa
from alembic import op
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


def _tenant_policy(name: str, *, mutable: bool) -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    table = f"{SCHEMA}.{name}"
    predicate = (
        "workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid"
    )
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY pol_{name}_tenant_isolation ON {table} FOR ALL TO {runtime} "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.execute(
        f"CREATE POLICY pol_{name}_migration_maintenance ON {table} FOR ALL TO {migration} "
        "USING (true) WITH CHECK (true)"
    )
    op.execute(f"GRANT {'SELECT, INSERT, UPDATE' if mutable else 'SELECT, INSERT'} ON TABLE {table} TO {runtime}")
    op.execute(f"REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {table} FROM {runtime}")


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "trusted_canonical_acceptance_selector_versions",
        sa.Column("selector_id", uuid, nullable=False),
        sa.Column("selector_series_id", uuid, nullable=False),
        sa.Column("selector_version", sa.Integer(), nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("source_kind", sa.String(128), nullable=False),
        sa.Column("source_contract_id", sa.String(128), nullable=False),
        sa.Column("source_contract_version", sa.String(128), nullable=False),
        sa.Column("authority_kind", sa.String(32), nullable=False),
        sa.Column("approved_scope_reference", uuid, nullable=False),
        sa.Column("provider_kind", sa.String(32)),
        sa.Column("provider_connection_id", uuid),
        sa.Column("provider_connection_version_id", uuid),
        sa.Column("provider_approval_id", uuid),
        sa.Column("resource_scope_reference", uuid),
        sa.Column("upload_authority_reference", uuid),
        sa.Column("root_policy_id", uuid),
        sa.Column("root_policy_version", sa.String(128)),
        sa.Column("root_policy_digest", sa.String(64)),
        sa.Column("content_policy_id", uuid),
        sa.Column("content_policy_version", sa.String(128)),
        sa.Column("content_policy_digest", sa.String(64)),
        sa.Column("required_schema_version", sa.Integer(), nullable=False),
        sa.Column("approval_reference", uuid, nullable=False),
        sa.Column("predecessor_selector_id", uuid),
        sa.Column("audit_reference", uuid, nullable=False),
        sa.Column("idempotency_identity", sa.String(255), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "environment_id", "selector_id", name="pk_trusted_canonical_acceptance_selectors"),
        sa.UniqueConstraint("workspace_id", "environment_id", "selector_series_id", "selector_version", name="uq_trusted_canonical_acceptance_selector_version"),
        sa.UniqueConstraint("workspace_id", "environment_id", "selector_series_id", "selector_id", name="uq_trusted_canonical_acceptance_selector_series_id"),
        sa.UniqueConstraint("workspace_id", "environment_id", "idempotency_identity", name="uq_trusted_canonical_acceptance_selector_idempotency"),
        sa.UniqueConstraint("predecessor_selector_id", name="uq_trusted_canonical_acceptance_selector_predecessor"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id"], [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"], name="fk_trusted_canonical_acceptance_selector_environment"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "provider_connection_id", "provider_connection_version_id"], [f"{SCHEMA}.provider_connection_versions.workspace_id", f"{SCHEMA}.provider_connection_versions.environment_id", f"{SCHEMA}.provider_connection_versions.connection_id", f"{SCHEMA}.provider_connection_versions.connection_version_id"], name="fk_trusted_canonical_acceptance_selector_connection"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "root_policy_id", "root_policy_version", "root_policy_digest"], [f"{SCHEMA}.bitrix24_disk_root_policy_versions.workspace_id", f"{SCHEMA}.bitrix24_disk_root_policy_versions.environment_id", f"{SCHEMA}.bitrix24_disk_root_policy_versions.policy_id", f"{SCHEMA}.bitrix24_disk_root_policy_versions.version", f"{SCHEMA}.bitrix24_disk_root_policy_versions.digest"], name="fk_trusted_canonical_acceptance_selector_root_policy"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "content_policy_id", "content_policy_version", "content_policy_digest"], [f"{SCHEMA}.bitrix24_disk_content_policy_versions.workspace_id", f"{SCHEMA}.bitrix24_disk_content_policy_versions.environment_id", f"{SCHEMA}.bitrix24_disk_content_policy_versions.policy_id", f"{SCHEMA}.bitrix24_disk_content_policy_versions.version", f"{SCHEMA}.bitrix24_disk_content_policy_versions.digest"], name="fk_trusted_canonical_acceptance_selector_content_policy"),
        sa.CheckConstraint("selector_version >= 1 AND required_schema_version IN (1, 2)", name="ck_trusted_canonical_acceptance_selector_versions"),
        sa.CheckConstraint("authority_kind IN ('provider_connection', 'upload')", name="ck_trusted_canonical_acceptance_selector_authority"),
        sa.CheckConstraint("jsonb_typeof(record) = 'object'", name="ck_trusted_canonical_acceptance_selector_record"),
        sa.CheckConstraint("(selector_version = 1 AND predecessor_selector_id IS NULL) OR (selector_version > 1 AND predecessor_selector_id IS NOT NULL)", name="ck_trusted_canonical_acceptance_selector_history"),
        sa.CheckConstraint("(provider_connection_id IS NULL) = (provider_connection_version_id IS NULL) AND (provider_connection_id IS NULL) = (provider_approval_id IS NULL) AND (provider_connection_id IS NULL) = (resource_scope_reference IS NULL)", name="ck_trusted_canonical_acceptance_selector_provider_tuple"),
        sa.CheckConstraint("(root_policy_id IS NULL AND root_policy_version IS NULL AND root_policy_digest IS NULL AND content_policy_id IS NULL AND content_policy_version IS NULL AND content_policy_digest IS NULL) OR (root_policy_id IS NOT NULL AND root_policy_version IS NOT NULL AND root_policy_digest ~ '^[0-9a-f]{64}$' AND content_policy_id IS NOT NULL AND content_policy_version IS NOT NULL AND content_policy_digest ~ '^[0-9a-f]{64}$')", name="ck_trusted_canonical_acceptance_selector_policy_tuple"),
        schema=SCHEMA,
    )
    op.create_table(
        "current_trusted_canonical_acceptance_selectors",
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("selector_series_id", uuid, nullable=False),
        sa.Column("current_selector_id", uuid, nullable=False),
        sa.Column("cas_version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "environment_id", "selector_series_id", name="pk_current_trusted_canonical_acceptance_selector"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "selector_series_id", "current_selector_id"], [f"{SCHEMA}.trusted_canonical_acceptance_selector_versions.workspace_id", f"{SCHEMA}.trusted_canonical_acceptance_selector_versions.environment_id", f"{SCHEMA}.trusted_canonical_acceptance_selector_versions.selector_series_id", f"{SCHEMA}.trusted_canonical_acceptance_selector_versions.selector_id"], name="fk_current_trusted_canonical_acceptance_selector"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id"], [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"], name="fk_current_trusted_canonical_acceptance_selector_environment"),
        sa.CheckConstraint("cas_version >= 1", name="ck_current_trusted_canonical_acceptance_selector_cas"),
        schema=SCHEMA,
    )
    _tenant_policy("trusted_canonical_acceptance_selector_versions", mutable=False)
    _tenant_policy("current_trusted_canonical_acceptance_selectors", mutable=True)
    op.execute(
        f"CREATE FUNCTION {SCHEMA}.reject_trusted_canonical_acceptance_selector_mutation() "
        "RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, spine "
        "AS $fn$ BEGIN RAISE EXCEPTION 'Canonical acceptance selectors are immutable.' "
        "USING ERRCODE = '23514'; END; $fn$"
    )
    op.execute(
        f"CREATE TRIGGER trg_trusted_canonical_acceptance_selector_immutable "
        f"BEFORE UPDATE OR DELETE ON {SCHEMA}.trusted_canonical_acceptance_selector_versions "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_trusted_canonical_acceptance_selector_mutation()"
    )
    op.execute(
        f"CREATE FUNCTION {SCHEMA}.guard_current_trusted_canonical_acceptance_selector_step() "
        "RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, spine "
        "AS $fn$ DECLARE selected_version integer; selected_predecessor uuid; "
        "selected_series uuid; BEGIN SELECT selector_version, predecessor_selector_id, selector_series_id "
        f"INTO selected_version, selected_predecessor, selected_series FROM {SCHEMA}.trusted_canonical_acceptance_selector_versions "
        "WHERE workspace_id = NEW.workspace_id AND environment_id = NEW.environment_id "
        "AND selector_id = NEW.current_selector_id; "
        "IF NOT FOUND OR selected_series IS DISTINCT FROM NEW.selector_series_id THEN "
        "RAISE EXCEPTION 'Selector current head has no matching immutable record.' USING ERRCODE = '23514'; END IF; "
        "IF TG_OP = 'INSERT' THEN IF NEW.cas_version <> 1 OR selected_version <> 1 OR selected_predecessor IS NOT NULL THEN "
        "RAISE EXCEPTION 'Selector baseline is invalid.' USING ERRCODE = '23514'; END IF; "
        "ELSIF NEW.cas_version <> OLD.cas_version + 1 OR selected_version <> NEW.cas_version "
        "OR selected_predecessor IS DISTINCT FROM OLD.current_selector_id THEN "
        "RAISE EXCEPTION 'Selector current-head CAS failed.' USING ERRCODE = '23514'; "
        "END IF; RETURN NEW; END; $fn$"
    )
    op.execute(
        f"CREATE TRIGGER trg_current_trusted_canonical_acceptance_selector_step "
        f"BEFORE INSERT OR UPDATE ON {SCHEMA}.current_trusted_canonical_acceptance_selectors "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_current_trusted_canonical_acceptance_selector_step()"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.reject_trusted_canonical_acceptance_selector_mutation() FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.guard_current_trusted_canonical_acceptance_selector_step() FROM PUBLIC")


def downgrade() -> None:
    op.drop_table("current_trusted_canonical_acceptance_selectors", schema=SCHEMA)
    op.drop_table("trusted_canonical_acceptance_selector_versions", schema=SCHEMA)
    op.execute(f"DROP FUNCTION {SCHEMA}.guard_current_trusted_canonical_acceptance_selector_step()")
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_trusted_canonical_acceptance_selector_mutation()")
