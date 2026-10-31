"""Persist immutable Bitrix24 Disk scope policies and current heads."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings

revision = "20261221_89"
down_revision = "20261220_88"
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
    op.execute(f"ALTER TABLE {SCHEMA}.{name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {SCHEMA}.{name} FORCE ROW LEVEL SECURITY")
    predicate = (
        "workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid"
    )
    op.execute(
        f"CREATE POLICY pol_{name}_tenant ON {SCHEMA}.{name} FOR ALL TO {runtime} "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.execute(f"CREATE POLICY pol_{name}_migration ON {SCHEMA}.{name} FOR ALL TO {migration} USING (true) WITH CHECK (true)")
    op.execute(f"GRANT {'SELECT, INSERT, UPDATE' if mutable else 'SELECT, INSERT'} ON TABLE {SCHEMA}.{name} TO {runtime}")
    op.execute(f"REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {SCHEMA}.{name} FROM {runtime}")


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.add_column(
        "source_revision_provenance",
        sa.Column("bitrix24_disk_membership", postgresql.JSONB(), nullable=True),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_source_provenance_b24_membership_record",
        "source_revision_provenance",
        "bitrix24_disk_membership IS NULL OR jsonb_typeof(bitrix24_disk_membership) = 'object'",
        schema=SCHEMA,
    )
    op.create_unique_constraint(
        "uq_provider_connection_scope_version",
        "provider_connection_versions",
        ["workspace_id", "environment_id", "connection_id", "connection_version_id"],
        schema=SCHEMA,
    )
    for kind in ("content", "root"):
        versions = f"bitrix24_disk_{kind}_policy_versions"
        heads = f"current_bitrix24_disk_{kind}_policies"
        version_columns = [
            sa.Column("policy_id", uuid, nullable=False),
            sa.Column("version", sa.String(128), nullable=False),
            sa.Column("digest", sa.String(64), nullable=False),
            sa.Column("workspace_id", uuid, nullable=False),
            sa.Column("environment_id", uuid, nullable=False),
            sa.Column("connection_id", uuid, nullable=False),
            sa.Column("connection_version_id", uuid, nullable=False),
            sa.Column("resource_scope_reference", uuid, nullable=False),
        ]
        if kind == "root":
            version_columns.extend(
                [
                    sa.Column("content_policy_id", uuid, nullable=False),
                    sa.Column("content_policy_version", sa.String(128), nullable=False),
                    sa.Column("content_policy_digest", sa.String(64), nullable=False),
                ]
            )
        version_columns.extend(
            [
                sa.Column("record", postgresql.JSONB(), nullable=False),
                sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
            ]
        )
        version_constraints = [
            sa.PrimaryKeyConstraint("workspace_id", "environment_id", "policy_id", "version", name=f"pk_b24_disk_{kind}_policy_versions"),
            sa.UniqueConstraint("workspace_id", "environment_id", "policy_id", "version", "digest", name=f"uq_b24_disk_{kind}_policy_digest"),
            sa.ForeignKeyConstraint(["workspace_id", "environment_id"], [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"], name=f"fk_b24_disk_{kind}_policy_environment"),
            sa.ForeignKeyConstraint(["workspace_id", "environment_id", "connection_id", "connection_version_id"], [f"{SCHEMA}.provider_connection_versions.workspace_id", f"{SCHEMA}.provider_connection_versions.environment_id", f"{SCHEMA}.provider_connection_versions.connection_id", f"{SCHEMA}.provider_connection_versions.connection_version_id"], name=f"fk_b24_disk_{kind}_policy_connection"),
            sa.CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name=f"ck_b24_disk_{kind}_policy_digest"),
            sa.CheckConstraint("jsonb_typeof(record) = 'object'", name=f"ck_b24_disk_{kind}_policy_record"),
        ]
        if kind == "root":
            version_constraints.append(
                sa.ForeignKeyConstraint(
                    ["workspace_id", "environment_id", "content_policy_id", "content_policy_version", "content_policy_digest"],
                    [f"{SCHEMA}.bitrix24_disk_content_policy_versions.workspace_id", f"{SCHEMA}.bitrix24_disk_content_policy_versions.environment_id", f"{SCHEMA}.bitrix24_disk_content_policy_versions.policy_id", f"{SCHEMA}.bitrix24_disk_content_policy_versions.version", f"{SCHEMA}.bitrix24_disk_content_policy_versions.digest"],
                    name="fk_b24_disk_root_content_policy",
                )
            )
        op.create_table(
            versions,
            *version_columns,
            *version_constraints,
            schema=SCHEMA,
        )
        op.create_table(
            heads,
            sa.Column("workspace_id", uuid, nullable=False),
            sa.Column("environment_id", uuid, nullable=False),
            sa.Column("policy_id", uuid, nullable=False),
            sa.Column("current_version", sa.String(128), nullable=False),
            sa.Column("current_digest", sa.String(64), nullable=False),
            sa.Column("predecessor_version", sa.String(128)),
            sa.Column("predecessor_digest", sa.String(64)),
            sa.Column("generation", sa.Integer(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("workspace_id", "environment_id", "policy_id", name=f"pk_current_b24_disk_{kind}_policy"),
            sa.ForeignKeyConstraint(["workspace_id", "environment_id", "policy_id", "current_version", "current_digest"], [f"{SCHEMA}.{versions}.workspace_id", f"{SCHEMA}.{versions}.environment_id", f"{SCHEMA}.{versions}.policy_id", f"{SCHEMA}.{versions}.version", f"{SCHEMA}.{versions}.digest"], name=f"fk_current_b24_disk_{kind}_policy_version"),
            sa.ForeignKeyConstraint(["workspace_id", "environment_id"], [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"], name=f"fk_current_b24_disk_{kind}_policy_environment"),
            sa.CheckConstraint("generation >= 1", name=f"ck_current_b24_disk_{kind}_policy_generation"),
            sa.CheckConstraint("current_digest ~ '^[0-9a-f]{64}$'", name=f"ck_current_b24_disk_{kind}_policy_digest"),
            sa.CheckConstraint("(generation = 1 AND predecessor_version IS NULL AND predecessor_digest IS NULL) OR (generation > 1 AND predecessor_version IS NOT NULL AND predecessor_digest ~ '^[0-9a-f]{64}$')", name=f"ck_current_b24_disk_{kind}_policy_predecessor"),
            schema=SCHEMA,
        )
        _tenant_policy(versions, mutable=False)
        _tenant_policy(heads, mutable=True)
        op.execute(
            f"CREATE FUNCTION {SCHEMA}.reject_{kind}_policy_mutation() RETURNS trigger "
            "LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, spine "
            "AS $fn$ BEGIN RAISE EXCEPTION 'Policy versions are immutable.' "
            "USING ERRCODE = '23514'; END; $fn$"
        )
        op.execute(f"CREATE TRIGGER trg_{kind}_policy_immutable BEFORE UPDATE OR DELETE ON {SCHEMA}.{versions} FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_{kind}_policy_mutation()")
        op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.reject_{kind}_policy_mutation() FROM PUBLIC")
        op.execute(
            f"CREATE FUNCTION {SCHEMA}.guard_current_{kind}_policy_step() RETURNS trigger "
            "LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, spine "
            "AS $fn$ BEGIN IF TG_OP = 'INSERT' THEN "
            "IF NEW.generation <> 1 OR NEW.predecessor_version IS NOT NULL THEN "
            "RAISE EXCEPTION 'Policy baseline is invalid.' USING ERRCODE = '23514'; END IF; "
            "ELSIF NEW.generation <> OLD.generation + 1 OR "
            "NEW.predecessor_version IS DISTINCT FROM OLD.current_version OR "
            "NEW.predecessor_digest IS DISTINCT FROM OLD.current_digest THEN "
            "RAISE EXCEPTION 'Policy current-head CAS failed.' USING ERRCODE = '23514'; "
            "END IF; RETURN NEW; END; $fn$"
        )
        op.execute(f"CREATE TRIGGER trg_current_{kind}_policy_step BEFORE INSERT OR UPDATE ON {SCHEMA}.{heads} FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_current_{kind}_policy_step()")
        op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.guard_current_{kind}_policy_step() FROM PUBLIC")


def downgrade() -> None:
    for kind in ("root", "content"):
        op.drop_table(f"current_bitrix24_disk_{kind}_policies", schema=SCHEMA)
        op.drop_table(f"bitrix24_disk_{kind}_policy_versions", schema=SCHEMA)
        op.execute(f"DROP FUNCTION {SCHEMA}.guard_current_{kind}_policy_step()")
        op.execute(f"DROP FUNCTION {SCHEMA}.reject_{kind}_policy_mutation()")
    op.drop_constraint("uq_provider_connection_scope_version", "provider_connection_versions", schema=SCHEMA, type_="unique")
    op.drop_constraint("ck_source_provenance_b24_membership_record", "source_revision_provenance", schema=SCHEMA, type_="check")
    op.drop_column("source_revision_provenance", "bitrix24_disk_membership", schema=SCHEMA)
