"""Add reader-first CanonicalAcceptance v3 and tombstone writer selectors."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20270112_111"
down_revision = "20270111_110"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _canonical_acceptance_check() -> str:
    return (
        "(canonical_acceptance_schema_version = 1 AND processing_admission_id IS NULL "
        "AND revision_kind IS NULL AND tombstone_authorization_id IS NULL "
        "AND tombstone_authorization_digest IS NULL AND transition_id IS NULL) OR "
        "(canonical_acceptance_schema_version = 2 AND processing_admission_id IS NOT NULL "
        "AND admission_decision_id IS NOT NULL AND access_policy_version_id IS NOT NULL "
        "AND revision_kind IS NULL AND tombstone_authorization_id IS NULL "
        "AND tombstone_authorization_digest IS NULL AND transition_id IS NULL) OR "
        "(canonical_acceptance_schema_version = 3 AND revision_kind = 'tombstone' "
        "AND processing_admission_id IS NULL AND admission_decision_id IS NULL "
        "AND access_policy_version_id IS NULL AND tombstone_authorization_id IS NOT NULL "
        "AND tombstone_authorization_digest ~ '^[0-9a-f]{64}$' AND transition_id IS NOT NULL "
        "AND predecessor_revision_id IS NOT NULL AND predecessor_acceptance_id IS NOT NULL)"
    )


def _restore_retained_upgrade() -> bool:
    """Re-enable a retained v3 schema after a safe downgrade and older replay."""

    inspector = sa.inspect(op.get_bind())
    columns = {
        column["name"]
        for column in inspector.get_columns("canonical_acceptances", schema=SCHEMA)
    }
    if "tombstone_authorization_id" not in columns:
        return False
    if not inspector.has_table(
        "trusted_canonical_tombstone_selector_versions", schema=SCHEMA
    ):
        raise RuntimeError("retained canonical tombstone v3 schema is incomplete")
    op.execute(
        f"DROP TRIGGER IF EXISTS trg_canonical_acceptance_v3_disabled ON {SCHEMA}.canonical_acceptances"
    )
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.deny_canonical_acceptance_v3_write()")
    op.create_unique_constraint(
        "uq_tombstone_authorizations_acceptance_link",
        "tombstone_authorizations",
        [
            "workspace_id", "environment_id", "authorization_id",
            "source_object_id", "command_digest", "transition_id",
        ],
        schema=SCHEMA,
    )
    op.drop_constraint(
        op.f("ck_canonical_acceptances_schema_processing"),
        "canonical_acceptances",
        schema=SCHEMA,
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_canonical_acceptances_schema_processing"),
        "canonical_acceptances",
        _canonical_acceptance_check(),
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_canonical_acceptances_tombstone_authorization",
        "canonical_acceptances",
        "tombstone_authorizations",
        ["workspace_id", "environment_id", "tombstone_authorization_id", "source_object_id", "tombstone_authorization_digest", "transition_id"],
        ["workspace_id", "environment_id", "authorization_id", "source_object_id", "command_digest", "transition_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )
    return True


def _role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, attribute))


def _tenant_table(name: str, *, mutable: bool) -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    predicate = (
        "workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid"
    )
    op.execute(f"ALTER TABLE {SCHEMA}.{name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {SCHEMA}.{name} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY pol_{name}_tenant_isolation ON {SCHEMA}.{name} FOR ALL TO {runtime} "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.execute(
        f"CREATE POLICY pol_{name}_migration_maintenance ON {SCHEMA}.{name} FOR ALL TO {migration} "
        "USING (true) WITH CHECK (true)"
    )
    privileges = "SELECT, INSERT, UPDATE" if mutable else "SELECT, INSERT"
    op.execute(f"GRANT {privileges} ON TABLE {SCHEMA}.{name} TO {runtime}")
    op.execute(
        f"REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {SCHEMA}.{name} FROM {runtime}"
    )


def upgrade() -> None:
    if _restore_retained_upgrade():
        return
    uuid = postgresql.UUID(as_uuid=True)
    op.create_unique_constraint(
        "uq_tombstone_authorizations_acceptance_link",
        "tombstone_authorizations",
        [
            "workspace_id",
            "environment_id",
            "authorization_id",
            "source_object_id",
            "command_digest",
            "transition_id",
        ],
        schema=SCHEMA,
    )
    op.add_column("canonical_acceptances", sa.Column("revision_kind", sa.Text()), schema=SCHEMA)
    op.add_column(
        "canonical_acceptances", sa.Column("tombstone_authorization_id", uuid), schema=SCHEMA
    )
    op.add_column(
        "canonical_acceptances",
        sa.Column("tombstone_authorization_digest", sa.String(64)),
        schema=SCHEMA,
    )
    op.add_column("canonical_acceptances", sa.Column("transition_id", uuid), schema=SCHEMA)
    op.drop_constraint(
        op.f("ck_canonical_acceptances_schema_processing"),
        "canonical_acceptances",
        schema=SCHEMA,
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_canonical_acceptances_schema_processing"),
        "canonical_acceptances",
        _canonical_acceptance_check(),
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_canonical_acceptances_tombstone_authorization",
        "canonical_acceptances",
        "tombstone_authorizations",
        [
            "workspace_id",
            "environment_id",
            "tombstone_authorization_id",
            "source_object_id",
            "tombstone_authorization_digest",
            "transition_id",
        ],
        [
            "workspace_id",
            "environment_id",
            "authorization_id",
            "source_object_id",
            "command_digest",
            "transition_id",
        ],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )
    op.create_index(
        "ix_canonical_acceptances_tombstone_authorization",
        "canonical_acceptances",
        ["workspace_id", "environment_id", "tombstone_authorization_id"],
        schema=SCHEMA,
    )

    op.create_table(
        "trusted_canonical_tombstone_selector_versions",
        sa.Column("selector_id", uuid, nullable=False),
        sa.Column("selector_series_id", uuid, nullable=False),
        sa.Column("selector_version", sa.Integer(), nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("source_object_id", uuid, nullable=False),
        sa.Column("source_contract_id", sa.String(128), nullable=False),
        sa.Column("source_contract_version", sa.String(64), nullable=False),
        sa.Column("provider_kind", sa.String(64), nullable=False),
        sa.Column("connection_id", uuid, nullable=False),
        sa.Column("resource_scope_reference", uuid, nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("approval_reference", uuid, nullable=False),
        sa.Column("predecessor_selector_id", uuid),
        sa.Column("audit_reference", uuid, nullable=False),
        sa.Column("idempotency_identity", sa.String(255), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id", "environment_id", "selector_id",
            name="pk_trusted_canonical_tombstone_selectors",
        ),
        sa.UniqueConstraint(
            "workspace_id", "environment_id", "selector_series_id", "selector_version",
            name="uq_trusted_canonical_tombstone_selector_version",
        ),
        sa.UniqueConstraint(
            "workspace_id", "environment_id", "source_contract_id",
            "source_object_id", "source_contract_version", "provider_kind", "connection_id",
            "resource_scope_reference", "selector_version",
            name="uq_trusted_canonical_tombstone_selector_scope_version",
        ),
        sa.UniqueConstraint(
            "workspace_id", "environment_id", "selector_series_id", "selector_id",
            name="uq_trusted_canonical_tombstone_selector_series_id",
        ),
        sa.UniqueConstraint(
            "workspace_id", "environment_id", "idempotency_identity",
            name="uq_trusted_canonical_tombstone_selector_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id"],
            [
                f"{SCHEMA}.source_objects.workspace_id",
                f"{SCHEMA}.source_objects.environment_id",
                f"{SCHEMA}.source_objects.source_object_id",
            ],
            name="fk_trusted_canonical_tombstone_selector_source",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "audit_reference"],
            [
                f"{SCHEMA}.audit_events.workspace_id",
                f"{SCHEMA}.audit_events.environment_id",
                f"{SCHEMA}.audit_events.audit_event_id",
            ],
            name="fk_trusted_canonical_tombstone_selector_audit",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint(
            "selector_version >= 1 AND mode IN ('legacy_v1_allowed','v3_required','deny_new')",
            name="ck_trusted_canonical_tombstone_selector_mode",
        ),
        sa.CheckConstraint(
            "valid_until > valid_from AND created_at >= valid_from AND created_at < valid_until",
            name="ck_trusted_canonical_tombstone_selector_validity",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "current_trusted_canonical_tombstone_selectors",
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("selector_series_id", uuid, nullable=False),
        sa.Column("current_selector_id", uuid, nullable=False),
        sa.Column("cas_version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id", "environment_id", "selector_series_id",
            name="pk_current_trusted_canonical_tombstone_selector",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "selector_series_id", "current_selector_id"],
            [
                f"{SCHEMA}.trusted_canonical_tombstone_selector_versions.workspace_id",
                f"{SCHEMA}.trusted_canonical_tombstone_selector_versions.environment_id",
                f"{SCHEMA}.trusted_canonical_tombstone_selector_versions.selector_series_id",
                f"{SCHEMA}.trusted_canonical_tombstone_selector_versions.selector_id",
            ],
            name="fk_current_trusted_canonical_tombstone_selector",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_current_trusted_canonical_tombstone_selector_environment",
        ),
        sa.CheckConstraint("cas_version >= 1", name="ck_current_tombstone_selector_cas"),
        schema=SCHEMA,
    )
    _tenant_table("trusted_canonical_tombstone_selector_versions", mutable=False)
    _tenant_table("current_trusted_canonical_tombstone_selectors", mutable=True)
    op.execute(
        f"CREATE FUNCTION {SCHEMA}.guard_canonical_tombstone_selector_issuer() "
        "RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, spine "
        "AS $fn$ BEGIN IF COALESCE(current_setting('spine.acting_subject_id', true), '') = '' "
        "OR current_setting('spine.purpose', true) IS DISTINCT FROM 'canonical_tombstone_selector' "
        "OR current_setting('spine.operation', true) IS DISTINCT FROM 'canonical_tombstone_selector.publish' "
        "OR current_setting('spine.workspace_id', true) IS DISTINCT FROM NEW.workspace_id::text "
        "OR current_setting('spine.environment_id', true) IS DISTINCT FROM NEW.environment_id::text THEN "
        "RAISE EXCEPTION 'Tombstone selector publication requires an authorized interactive context.' "
        "USING ERRCODE = '42501'; END IF; RETURN NEW; END; $fn$"
    )
    op.execute(
        f"CREATE TRIGGER trg_canonical_tombstone_selector_issuer BEFORE INSERT ON "
        f"{SCHEMA}.trusted_canonical_tombstone_selector_versions FOR EACH ROW EXECUTE FUNCTION "
        f"{SCHEMA}.guard_canonical_tombstone_selector_issuer()"
    )
    op.execute(
        f"CREATE FUNCTION {SCHEMA}.reject_canonical_tombstone_selector_mutation() "
        "RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, spine "
        "AS $fn$ BEGIN RAISE EXCEPTION 'Canonical tombstone selectors are immutable.' "
        "USING ERRCODE = '23514'; END; $fn$"
    )
    op.execute(
        f"CREATE TRIGGER trg_canonical_tombstone_selectors_immutable BEFORE UPDATE OR DELETE ON "
        f"{SCHEMA}.trusted_canonical_tombstone_selector_versions FOR EACH ROW EXECUTE FUNCTION "
        f"{SCHEMA}.reject_canonical_tombstone_selector_mutation()"
    )
    op.execute(
        f"CREATE FUNCTION {SCHEMA}.guard_current_canonical_tombstone_selector_step() "
        "RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, spine "
        "AS $fn$ DECLARE selected_version integer; selected_predecessor uuid; selected_series uuid; "
        "BEGIN IF COALESCE(current_setting('spine.acting_subject_id', true), '') = '' "
        "OR current_setting('spine.purpose', true) IS DISTINCT FROM 'canonical_tombstone_selector' "
        "OR current_setting('spine.operation', true) IS DISTINCT FROM 'canonical_tombstone_selector.publish' "
        "OR current_setting('spine.workspace_id', true) IS DISTINCT FROM NEW.workspace_id::text "
        "OR current_setting('spine.environment_id', true) IS DISTINCT FROM NEW.environment_id::text THEN "
        "RAISE EXCEPTION 'Tombstone selector publication requires an authorized interactive context.' "
        "USING ERRCODE = '42501'; END IF; SELECT selector_version, predecessor_selector_id, selector_series_id "
        f"INTO selected_version, selected_predecessor, selected_series FROM {SCHEMA}.trusted_canonical_tombstone_selector_versions "
        "WHERE workspace_id = NEW.workspace_id AND environment_id = NEW.environment_id "
        "AND selector_id = NEW.current_selector_id; IF NOT FOUND OR selected_series IS DISTINCT FROM NEW.selector_series_id THEN "
        "RAISE EXCEPTION 'Tombstone selector head has no immutable record.' USING ERRCODE = '23514'; END IF; "
        "IF TG_OP = 'INSERT' THEN IF NEW.cas_version <> 1 OR selected_version <> 1 OR selected_predecessor IS NOT NULL THEN "
        "RAISE EXCEPTION 'Tombstone selector baseline is invalid.' USING ERRCODE = '23514'; END IF; "
        "ELSIF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id OR NEW.environment_id IS DISTINCT FROM OLD.environment_id "
        "OR NEW.selector_series_id IS DISTINCT FROM OLD.selector_series_id OR NEW.cas_version <> OLD.cas_version + 1 "
        "OR selected_version <> NEW.cas_version OR selected_predecessor IS DISTINCT FROM OLD.current_selector_id THEN "
        "RAISE EXCEPTION 'Tombstone selector current-head CAS failed.' USING ERRCODE = '23514'; END IF; "
        "RETURN NEW; END; $fn$"
    )
    op.execute(
        f"CREATE TRIGGER trg_current_canonical_tombstone_selector_step BEFORE INSERT OR UPDATE ON "
        f"{SCHEMA}.current_trusted_canonical_tombstone_selectors FOR EACH ROW EXECUTE FUNCTION "
        f"{SCHEMA}.guard_current_canonical_tombstone_selector_step()"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.guard_canonical_tombstone_selector_issuer() FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.guard_current_canonical_tombstone_selector_step() FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.reject_canonical_tombstone_selector_mutation() FROM PUBLIC")


def downgrade() -> None:
    """Safe rollback: disable new v3 writes while retaining readers and history."""

    op.drop_constraint(
        "fk_canonical_acceptances_tombstone_authorization",
        "canonical_acceptances",
        schema=SCHEMA,
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_tombstone_authorizations_acceptance_link",
        "tombstone_authorizations",
        schema=SCHEMA,
        type_="unique",
    )
    op.execute(
        f"CREATE OR REPLACE FUNCTION {SCHEMA}.deny_canonical_acceptance_v3_write() "
        "RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, spine "
        "AS $fn$ BEGIN IF NEW.canonical_acceptance_schema_version = 3 THEN "
        "RAISE EXCEPTION 'Canonical acceptance v3 writes are disabled by rollback.' "
        "USING ERRCODE = '55000'; END IF; RETURN NEW; END; $fn$"
    )
    op.execute(
        f"DROP TRIGGER IF EXISTS trg_canonical_acceptance_v3_disabled ON {SCHEMA}.canonical_acceptances"
    )
    op.execute(
        f"CREATE TRIGGER trg_canonical_acceptance_v3_disabled BEFORE INSERT ON "
        f"{SCHEMA}.canonical_acceptances FOR EACH ROW EXECUTE FUNCTION "
        f"{SCHEMA}.deny_canonical_acceptance_v3_write()"
    )
