"""Add reader-first CRM relationship evidence v2 persistence."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings

revision = "20261223_91"
down_revision = "20261222_90"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def _tenant_table(name: str, *, mutable: bool = False) -> None:
    runtime, migration = _role("runtime_role"), _role("migration_role")
    table = f"{SCHEMA}.{name}"
    op.execute(f"DROP POLICY IF EXISTS pol_{name}_tenant_isolation ON {table}")
    op.execute(f"DROP POLICY IF EXISTS pol_{name}_migration_maintenance ON {table}")
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    tenant = ("workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
              "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid")
    op.execute(f"CREATE POLICY pol_{name}_tenant_isolation ON {table} FOR ALL TO {runtime} USING ({tenant}) WITH CHECK ({tenant})")
    op.execute(f"CREATE POLICY pol_{name}_migration_maintenance ON {table} FOR ALL TO {migration} USING (true) WITH CHECK (true)")
    grants = "SELECT, INSERT, UPDATE" if mutable else "SELECT, INSERT"
    op.execute(f"GRANT {grants} ON TABLE {table} TO {runtime}")
    op.execute(f"REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {table} FROM {runtime}")
    op.execute(f"DROP TRIGGER IF EXISTS trg_{name}_v2_writer_enabled ON {table}")
    op.execute(f"CREATE TRIGGER trg_{name}_v2_writer_enabled BEFORE INSERT OR UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.require_crm_relationship_v2_writes_enabled()")
    if not mutable:
        fn = f"reject_{name}_mutation"
        op.execute(f"DROP TRIGGER IF EXISTS trg_{name}_immutable ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.{fn}()")
        op.execute(f"CREATE FUNCTION {SCHEMA}.{fn}() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA} AS $$ BEGIN RAISE EXCEPTION 'CRM relationship history is immutable.' USING ERRCODE = '23514'; END $$")
        op.execute(f"CREATE TRIGGER trg_{name}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.{fn}()")
        op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.{fn}() FROM PUBLIC")


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB()
    def common() -> list[sa.Column]:
        return [sa.Column("workspace_id", uuid, nullable=False), sa.Column("environment_id", uuid, nullable=False)]

    op.create_table(
        "crm_relationship_v2_state",
        sa.Column("singleton", sa.Boolean(), primary_key=True, server_default=sa.true()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("singleton IS TRUE", name="ck_crm_rel_v2_state_singleton"),
        schema=SCHEMA, if_not_exists=True,
    )
    op.execute(f"REVOKE ALL ON TABLE {SCHEMA}.crm_relationship_v2_state FROM PUBLIC")
    op.execute(f"GRANT SELECT, UPDATE ON TABLE {SCHEMA}.crm_relationship_v2_state TO {_role('migration_role')}")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.crm_relationship_v2_writes_enabled()")
    op.execute(
        f"CREATE FUNCTION {SCHEMA}.crm_relationship_v2_writes_enabled() RETURNS boolean "
        f"LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, {SCHEMA} "
        f"AS $$ SELECT enabled FROM {SCHEMA}.crm_relationship_v2_state WHERE singleton $$"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.crm_relationship_v2_writes_enabled() FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SCHEMA}.crm_relationship_v2_writes_enabled() TO {_role('runtime_role')}")
    op.execute(f"CREATE OR REPLACE FUNCTION {SCHEMA}.require_crm_relationship_v2_writes_enabled() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA} AS $$ BEGIN IF NOT {SCHEMA}.crm_relationship_v2_writes_enabled() THEN RAISE EXCEPTION 'CRM relationship v2 writes are disabled.' USING ERRCODE = '55000'; END IF; RETURN NEW; END $$")
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.require_crm_relationship_v2_writes_enabled() FROM PUBLIC")
    op.execute(f"INSERT INTO {SCHEMA}.crm_relationship_v2_state(singleton, enabled) VALUES (true, false) ON CONFLICT (singleton) DO NOTHING")

    # Runtime roles can only resolve authorization generations through a
    # security-definer boundary. Lock the current row there so a concurrent
    # authority change serializes with each CRM v2 write transaction.
    op.execute(f"""
    CREATE FUNCTION {SCHEMA}.lock_current_authorization_generation(
        requested_workspace_id uuid,
        requested_environment_id uuid
    ) RETURNS bigint
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = pg_catalog, {SCHEMA}
    AS $$
    DECLARE resolved_generation bigint;
    BEGIN
        IF current_setting('spine.workspace_id', true) IS DISTINCT FROM requested_workspace_id::text
           OR current_setting('spine.environment_id', true) IS DISTINCT FROM requested_environment_id::text THEN
            RETURN NULL;
        END IF;
        SELECT generation INTO resolved_generation
        FROM {SCHEMA}.authorization_generations
        WHERE workspace_id = requested_workspace_id
          AND environment_id = requested_environment_id
        FOR SHARE;
        RETURN resolved_generation;
    END
    $$
    """)
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.lock_current_authorization_generation(uuid, uuid) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SCHEMA}.lock_current_authorization_generation(uuid, uuid) TO {_role('runtime_role')}")

    op.execute(f"""
    DO $block$
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_provider_connection_scope_id') THEN
        ALTER TABLE {SCHEMA}.provider_connection_versions
        ADD CONSTRAINT uq_provider_connection_scope_id
        UNIQUE (workspace_id, environment_id, connection_id, connection_version_id);
      END IF;
    END $block$;
    """)

    op.create_table(
        "crm_relationship_identities",
        sa.Column("relationship_key", sa.String(64), primary_key=True), *common(),
        sa.Column("natural_identity", jsonb, nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False, server_default="spine.crm-relationship-evidence:v2"),
        sa.Column("record_kind", sa.Text(), nullable=False, server_default="logical_relationship"),
        sa.UniqueConstraint("workspace_id", "environment_id", "relationship_key", name="uq_crm_rel_identity_scope_key"),
        sa.CheckConstraint("schema_version = 'spine.crm-relationship-evidence:v2' AND record_kind = 'logical_relationship'", name="ck_crm_rel_identity_dispatch"),
        sa.CheckConstraint("relationship_key ~ '^[0-9a-f]{64}$'", name="ck_crm_rel_identity_key"), schema=SCHEMA, if_not_exists=True)

    op.create_table(
        "crm_relationship_profiles",
        sa.Column("profile_id", uuid, nullable=False), *common(),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("profile_digest", sa.String(64), nullable=False),
        sa.Column("record", jsonb, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "environment_id", "profile_id", "profile_version", name="pk_crm_relationship_profiles"),
        sa.UniqueConstraint("workspace_id", "environment_id", "profile_id", "profile_version", "profile_digest", name="uq_crm_relationship_profiles_digest"),
        sa.CheckConstraint("profile_version > 0 AND profile_digest ~ '^[0-9a-f]{64}$'", name="ck_crm_relationship_profiles_values"),
        sa.CheckConstraint("record->>'schema_version' = 'bitrix24-crm-files-mapping-profile:v1' AND record->>'record_kind' = 'mapping_profile'", name="ck_crm_relationship_profiles_dispatch"), schema=SCHEMA, if_not_exists=True)

    op.create_table(
        "crm_relationship_authority_heads",
        *common(), sa.Column("relationship_key", sa.String(64), nullable=False),
        sa.Column("lifecycle_generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("withdrawn", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_event_id", uuid),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("workspace_id", "environment_id", "relationship_key", name="pk_crm_rel_authority_heads"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "relationship_key"], [f"{SCHEMA}.crm_relationship_identities.workspace_id", f"{SCHEMA}.crm_relationship_identities.environment_id", f"{SCHEMA}.crm_relationship_identities.relationship_key"], name="fk_crm_rel_authority_identity"),
        sa.CheckConstraint("lifecycle_generation >= 0", name="ck_crm_rel_authority_generation"),
        schema=SCHEMA, if_not_exists=True)

    op.create_table(
        "crm_relationship_claims",
        sa.Column("claim_id", uuid, primary_key=True), *common(),
        sa.Column("relationship_key", sa.String(64), nullable=False),
        sa.Column("source_object_id", uuid, nullable=False), sa.Column("source_revision_id", uuid, nullable=False),
        sa.Column("connection_id", uuid, nullable=False), sa.Column("connection_version_id", uuid, nullable=False),
        sa.Column("audit_event_id", uuid, nullable=False), sa.Column("outbox_event_id", uuid, nullable=False),
        sa.Column("mapping_profile_id", uuid, nullable=False), sa.Column("mapping_profile_version", sa.Integer(), nullable=False),
        sa.Column("verification_generation", sa.BigInteger(), nullable=False),
        sa.Column("authorization_generation", sa.BigInteger(), nullable=False),
        sa.Column("configuration_generation", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", uuid, nullable=False), sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("record", jsonb, nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "environment_id", "claim_id", name="uq_crm_rel_claim_scope_id"),
        sa.UniqueConstraint("workspace_id", "environment_id", "claim_id", "mapping_profile_id", "mapping_profile_version", name="uq_crm_rel_claim_profile_ref"),
        sa.UniqueConstraint("workspace_id", "environment_id", "idempotency_key", name="uq_crm_rel_claim_idempotency"),
        sa.UniqueConstraint("workspace_id", "environment_id", "relationship_key", "source_revision_id", name="uq_crm_rel_claim_revision"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "relationship_key"], [f"{SCHEMA}.crm_relationship_identities.workspace_id", f"{SCHEMA}.crm_relationship_identities.environment_id", f"{SCHEMA}.crm_relationship_identities.relationship_key"], name="fk_crm_rel_claim_identity"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "connection_id", "connection_version_id"], [f"{SCHEMA}.provider_connection_versions.workspace_id", f"{SCHEMA}.provider_connection_versions.environment_id", f"{SCHEMA}.provider_connection_versions.connection_id", f"{SCHEMA}.provider_connection_versions.connection_version_id"], name="fk_crm_rel_claim_connection_version"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "audit_event_id"], [f"{SCHEMA}.audit_events.workspace_id", f"{SCHEMA}.audit_events.environment_id", f"{SCHEMA}.audit_events.audit_event_id"], name="fk_crm_rel_claim_audit"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "outbox_event_id"], [f"{SCHEMA}.outbox_intents.workspace_id", f"{SCHEMA}.outbox_intents.environment_id", f"{SCHEMA}.outbox_intents.event_id"], name="fk_crm_rel_claim_outbox"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "mapping_profile_id", "mapping_profile_version"], [f"{SCHEMA}.crm_relationship_profiles.workspace_id", f"{SCHEMA}.crm_relationship_profiles.environment_id", f"{SCHEMA}.crm_relationship_profiles.profile_id", f"{SCHEMA}.crm_relationship_profiles.profile_version"], name="fk_crm_rel_claim_profile"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "source_object_id", "source_revision_id"], [f"{SCHEMA}.source_revisions.workspace_id", f"{SCHEMA}.source_revisions.environment_id", f"{SCHEMA}.source_revisions.source_object_id", f"{SCHEMA}.source_revisions.revision_id"], name="fk_crm_rel_claim_source_revision"),
        sa.CheckConstraint("verification_generation > 0 AND payload_digest ~ '^[0-9a-f]{64}$'", name="ck_crm_rel_claim_values"),
        sa.CheckConstraint("record->>'schema_version' = 'spine.crm-relationship-evidence:v2' AND record->>'record_kind' = 'claim'", name="ck_crm_rel_claim_dispatch"), schema=SCHEMA, if_not_exists=True)

    op.create_table(
        "crm_relationship_receipts",
        sa.Column("receipt_id", uuid, primary_key=True), *common(),
        sa.Column("claim_id", uuid, nullable=False), sa.Column("verification_generation", sa.BigInteger(), nullable=False),
        sa.Column("connection_id", uuid, nullable=False), sa.Column("connection_version_id", uuid, nullable=False),
        sa.Column("mapping_profile_id", uuid, nullable=False), sa.Column("mapping_profile_version", sa.Integer(), nullable=False),
        sa.Column("acting_human_id", uuid, nullable=False),
        sa.Column("authorization_generation", sa.BigInteger(), nullable=False), sa.Column("configuration_generation", sa.BigInteger(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False), sa.Column("idempotency_key", uuid, nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False), sa.Column("record", jsonb, nullable=False),
        sa.Column("audit_event_id", uuid, nullable=False), sa.Column("outbox_event_id", uuid, nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "environment_id", "receipt_id", name="uq_crm_rel_receipt_scope_id"),
        sa.UniqueConstraint("workspace_id", "environment_id", "claim_id", "verification_generation", name="uq_crm_rel_receipt_generation"),
        sa.UniqueConstraint("workspace_id", "environment_id", "idempotency_key", name="uq_crm_rel_receipt_idempotency"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "claim_id"], [f"{SCHEMA}.crm_relationship_claims.workspace_id", f"{SCHEMA}.crm_relationship_claims.environment_id", f"{SCHEMA}.crm_relationship_claims.claim_id"], name="fk_crm_rel_receipt_claim"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "mapping_profile_id", "mapping_profile_version"], [f"{SCHEMA}.crm_relationship_profiles.workspace_id", f"{SCHEMA}.crm_relationship_profiles.environment_id", f"{SCHEMA}.crm_relationship_profiles.profile_id", f"{SCHEMA}.crm_relationship_profiles.profile_version"], name="fk_crm_rel_receipt_profile"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "connection_id", "connection_version_id"], [f"{SCHEMA}.provider_connection_versions.workspace_id", f"{SCHEMA}.provider_connection_versions.environment_id", f"{SCHEMA}.provider_connection_versions.connection_id", f"{SCHEMA}.provider_connection_versions.connection_version_id"], name="fk_crm_rel_receipt_connection_version"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "audit_event_id"], [f"{SCHEMA}.audit_events.workspace_id", f"{SCHEMA}.audit_events.environment_id", f"{SCHEMA}.audit_events.audit_event_id"], name="fk_crm_rel_receipt_audit"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "outbox_event_id"], [f"{SCHEMA}.outbox_intents.workspace_id", f"{SCHEMA}.outbox_intents.environment_id", f"{SCHEMA}.outbox_intents.event_id"], name="fk_crm_rel_receipt_outbox"),
        sa.CheckConstraint("verification_generation > 0 AND outcome IN ('ELIGIBLE_VERIFIED','NOT_VERIFIED','INACCESSIBLE','STALE_CONTEXT','GOVERNANCE_WITHDRAWN','SOURCE_INELIGIBLE','AUTHORITY_UNAVAILABLE','PROVIDER_REVOCATION_UNQUALIFIED') AND payload_digest ~ '^[0-9a-f]{64}$'", name="ck_crm_rel_receipt_values"),
        sa.CheckConstraint("record->>'schema_version' = 'spine.crm-relationship-evidence:v2' AND record->>'record_kind' = 'observation_receipt'", name="ck_crm_rel_receipt_dispatch"), schema=SCHEMA, if_not_exists=True)

    op.create_table(
        "crm_relationship_lifecycle_events",
        sa.Column("event_id", uuid, primary_key=True), *common(),
        sa.Column("relationship_key", sa.String(64), nullable=False), sa.Column("claim_id", uuid),
        sa.Column("generation", sa.BigInteger(), nullable=False), sa.Column("action", sa.Text(), nullable=False),
        sa.Column("expected_generation", sa.BigInteger(), nullable=False), sa.Column("actor_id", uuid, nullable=False),
        sa.Column("authority_id", uuid, nullable=False), sa.Column("authority_version_id", uuid, nullable=False),
        sa.Column("authorization_generation", sa.BigInteger(), nullable=False),
        sa.Column("predecessor_event_id", uuid), sa.Column("reapproval_receipt_id", uuid),
        sa.Column("reapproval_eligibility_id", uuid),
        sa.Column("idempotency_key", uuid, nullable=False), sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("audit_event_id", uuid, nullable=False), sa.Column("outbox_event_id", uuid, nullable=False),
        sa.Column("record", jsonb, nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "environment_id", "event_id", name="uq_crm_rel_lifecycle_scope_id"),
        sa.UniqueConstraint("workspace_id", "environment_id", "relationship_key", "generation", name="uq_crm_rel_lifecycle_generation"),
        sa.UniqueConstraint("workspace_id", "environment_id", "idempotency_key", name="uq_crm_rel_lifecycle_idempotency"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "relationship_key"], [f"{SCHEMA}.crm_relationship_identities.workspace_id", f"{SCHEMA}.crm_relationship_identities.environment_id", f"{SCHEMA}.crm_relationship_identities.relationship_key"], name="fk_crm_rel_lifecycle_identity"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "claim_id"], [f"{SCHEMA}.crm_relationship_claims.workspace_id", f"{SCHEMA}.crm_relationship_claims.environment_id", f"{SCHEMA}.crm_relationship_claims.claim_id"], name="fk_crm_rel_lifecycle_claim"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "predecessor_event_id"], [f"{SCHEMA}.crm_relationship_lifecycle_events.workspace_id", f"{SCHEMA}.crm_relationship_lifecycle_events.environment_id", f"{SCHEMA}.crm_relationship_lifecycle_events.event_id"], name="fk_crm_rel_lifecycle_predecessor"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "reapproval_receipt_id"], [f"{SCHEMA}.crm_relationship_receipts.workspace_id", f"{SCHEMA}.crm_relationship_receipts.environment_id", f"{SCHEMA}.crm_relationship_receipts.receipt_id"], name="fk_crm_rel_lifecycle_receipt"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "audit_event_id"], [f"{SCHEMA}.audit_events.workspace_id", f"{SCHEMA}.audit_events.environment_id", f"{SCHEMA}.audit_events.audit_event_id"], name="fk_crm_rel_lifecycle_audit"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "outbox_event_id"], [f"{SCHEMA}.outbox_intents.workspace_id", f"{SCHEMA}.outbox_intents.environment_id", f"{SCHEMA}.outbox_intents.event_id"], name="fk_crm_rel_lifecycle_outbox"),
        sa.CheckConstraint("generation > 0 AND expected_generation >= 0 AND action IN ('withdraw','reapprove') AND payload_digest ~ '^[0-9a-f]{64}$'", name="ck_crm_rel_lifecycle_values"),
        sa.CheckConstraint("record->>'schema_version' = 'spine.crm-relationship-evidence:v2' AND record->>'record_kind' = 'lifecycle_event'", name="ck_crm_rel_lifecycle_dispatch"), schema=SCHEMA, if_not_exists=True)

    op.execute(f"""
    DO $block$
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_crm_rel_authority_event') THEN
        ALTER TABLE {SCHEMA}.crm_relationship_authority_heads
        ADD CONSTRAINT fk_crm_rel_authority_event
        FOREIGN KEY (workspace_id, environment_id, last_event_id)
        REFERENCES {SCHEMA}.crm_relationship_lifecycle_events (workspace_id, environment_id, event_id);
      END IF;
    END $block$;
    """)

    op.create_table(
        "crm_relationship_eligibility",
        sa.Column("eligibility_id", uuid, primary_key=True), *common(),
        sa.Column("claim_id", uuid, nullable=False),
        sa.Column("receipt_id", uuid, nullable=False),
        sa.Column("verification_generation", sa.BigInteger(), nullable=False),
        sa.Column("lifecycle_generation", sa.BigInteger(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("record", jsonb, nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "environment_id", "eligibility_id", name="uq_crm_rel_eligibility_scope_id"),
        sa.UniqueConstraint("workspace_id", "environment_id", "claim_id", "eligibility_id", name="uq_crm_rel_eligibility_claim_id"),
        sa.UniqueConstraint("workspace_id", "environment_id", "claim_id", "verification_generation", "lifecycle_generation", name="uq_crm_rel_eligibility_generation"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "claim_id"], [f"{SCHEMA}.crm_relationship_claims.workspace_id", f"{SCHEMA}.crm_relationship_claims.environment_id", f"{SCHEMA}.crm_relationship_claims.claim_id"], name="fk_crm_rel_eligibility_claim"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "receipt_id"], [f"{SCHEMA}.crm_relationship_receipts.workspace_id", f"{SCHEMA}.crm_relationship_receipts.environment_id", f"{SCHEMA}.crm_relationship_receipts.receipt_id"], name="fk_crm_rel_eligibility_receipt"),
        sa.CheckConstraint("verification_generation > 0 AND lifecycle_generation >= 0 AND outcome IN ('ELIGIBLE_VERIFIED','NOT_VERIFIED','INACCESSIBLE','STALE_CONTEXT','GOVERNANCE_WITHDRAWN','SOURCE_INELIGIBLE','AUTHORITY_UNAVAILABLE','PROVIDER_REVOCATION_UNQUALIFIED')", name="ck_crm_rel_eligibility_values"),
        sa.CheckConstraint("record->>'schema_version' = 'spine.crm-relationship-evidence:v2' AND record->>'record_kind' = 'eligibility'", name="ck_crm_rel_eligibility_dispatch"),
        schema=SCHEMA, if_not_exists=True)

    op.execute(f"""
    DO $block$
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_crm_rel_lifecycle_eligibility') THEN
        ALTER TABLE {SCHEMA}.crm_relationship_lifecycle_events
        ADD CONSTRAINT fk_crm_rel_lifecycle_eligibility
        FOREIGN KEY (workspace_id, environment_id, claim_id, reapproval_eligibility_id)
        REFERENCES {SCHEMA}.crm_relationship_eligibility (workspace_id, environment_id, claim_id, eligibility_id);
      END IF;
    END $block$;
    """)

    op.create_table(
        "crm_relationship_eligibility_heads",
        *common(), sa.Column("claim_id", uuid, nullable=False), sa.Column("verification_generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("lifecycle_generation", sa.BigInteger(), nullable=False, server_default="0"), sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("receipt_id", uuid), sa.Column("eligibility_id", uuid), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "environment_id", "claim_id", name="pk_crm_rel_eligibility_heads"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "claim_id"], [f"{SCHEMA}.crm_relationship_claims.workspace_id", f"{SCHEMA}.crm_relationship_claims.environment_id", f"{SCHEMA}.crm_relationship_claims.claim_id"], name="fk_crm_rel_head_claim"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "claim_id", "eligibility_id"], [f"{SCHEMA}.crm_relationship_eligibility.workspace_id", f"{SCHEMA}.crm_relationship_eligibility.environment_id", f"{SCHEMA}.crm_relationship_eligibility.claim_id", f"{SCHEMA}.crm_relationship_eligibility.eligibility_id"], name="fk_crm_rel_head_eligibility"),
        sa.CheckConstraint("verification_generation >= 0 AND lifecycle_generation >= 0", name="ck_crm_rel_head_generations"), schema=SCHEMA, if_not_exists=True)

    op.create_table(
        "crm_relationship_idempotency",
        sa.Column("workspace_id", uuid, nullable=False), sa.Column("environment_id", uuid, nullable=False),
        sa.Column("idempotency_key", uuid, nullable=False), sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("record_kind", sa.Text(), nullable=False), sa.Column("record_id", uuid, nullable=False),
        sa.Column("result", jsonb, nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "environment_id", "idempotency_key", name="pk_crm_rel_idempotency"),
        sa.CheckConstraint("payload_digest ~ '^[0-9a-f]{64}$' AND record_kind IN ('claim','observation_receipt','lifecycle_event')", name="ck_crm_rel_idempotency_values"), schema=SCHEMA, if_not_exists=True)

    op.execute(f"""
    DO $block$
    DECLARE item record;
    BEGIN
      FOR item IN SELECT * FROM (VALUES
        ('crm_relationship_claims','fk_crm_rel_claim_connection_version','FOREIGN KEY (workspace_id, environment_id, connection_id, connection_version_id) REFERENCES {SCHEMA}.provider_connection_versions (workspace_id, environment_id, connection_id, connection_version_id)'),
        ('crm_relationship_claims','fk_crm_rel_claim_audit','FOREIGN KEY (workspace_id, environment_id, audit_event_id) REFERENCES {SCHEMA}.audit_events (workspace_id, environment_id, audit_event_id)'),
        ('crm_relationship_claims','fk_crm_rel_claim_outbox','FOREIGN KEY (workspace_id, environment_id, outbox_event_id) REFERENCES {SCHEMA}.outbox_intents (workspace_id, environment_id, event_id)'),
        ('crm_relationship_claims','fk_crm_rel_claim_source_revision','FOREIGN KEY (workspace_id, environment_id, source_object_id, source_revision_id) REFERENCES {SCHEMA}.source_revisions (workspace_id, environment_id, source_object_id, revision_id)'),
        ('crm_relationship_receipts','fk_crm_rel_receipt_connection_version','FOREIGN KEY (workspace_id, environment_id, connection_id, connection_version_id) REFERENCES {SCHEMA}.provider_connection_versions (workspace_id, environment_id, connection_id, connection_version_id)'),
        ('crm_relationship_receipts','fk_crm_rel_receipt_audit','FOREIGN KEY (workspace_id, environment_id, audit_event_id) REFERENCES {SCHEMA}.audit_events (workspace_id, environment_id, audit_event_id)'),
        ('crm_relationship_receipts','fk_crm_rel_receipt_outbox','FOREIGN KEY (workspace_id, environment_id, outbox_event_id) REFERENCES {SCHEMA}.outbox_intents (workspace_id, environment_id, event_id)'),
        ('crm_relationship_lifecycle_events','fk_crm_rel_lifecycle_audit','FOREIGN KEY (workspace_id, environment_id, audit_event_id) REFERENCES {SCHEMA}.audit_events (workspace_id, environment_id, audit_event_id)'),
        ('crm_relationship_lifecycle_events','fk_crm_rel_lifecycle_outbox','FOREIGN KEY (workspace_id, environment_id, outbox_event_id) REFERENCES {SCHEMA}.outbox_intents (workspace_id, environment_id, event_id)')
      ) AS refs(table_name, constraint_name, definition)
      LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = item.constraint_name) THEN
          EXECUTE format('ALTER TABLE {SCHEMA}.%I ADD CONSTRAINT %I %s', item.table_name, item.constraint_name, item.definition);
        END IF;
      END LOOP;
    END
    $block$;
    """)

    for name in ("crm_relationship_identities", "crm_relationship_profiles", "crm_relationship_claims", "crm_relationship_receipts", "crm_relationship_eligibility", "crm_relationship_lifecycle_events", "crm_relationship_idempotency"):
        _tenant_table(name)
    _tenant_table("crm_relationship_eligibility_heads", mutable=True)
    _tenant_table("crm_relationship_authority_heads", mutable=True)


def downgrade() -> None:
    # Operational rollback disables v2 writers and retains all evidence for re-upgrade.
    # Keep the scoped authorization-lock helper so retained v2 readers remain usable.
    op.execute(f"UPDATE {SCHEMA}.crm_relationship_v2_state SET enabled = false WHERE singleton")
    for table, constraint in (
        ("crm_relationship_claims", "fk_crm_rel_claim_connection_version"),
        ("crm_relationship_claims", "fk_crm_rel_claim_audit"),
        ("crm_relationship_claims", "fk_crm_rel_claim_outbox"),
        ("crm_relationship_claims", "fk_crm_rel_claim_source_revision"),
        ("crm_relationship_receipts", "fk_crm_rel_receipt_connection_version"),
        ("crm_relationship_receipts", "fk_crm_rel_receipt_audit"),
        ("crm_relationship_receipts", "fk_crm_rel_receipt_outbox"),
        ("crm_relationship_lifecycle_events", "fk_crm_rel_lifecycle_audit"),
        ("crm_relationship_lifecycle_events", "fk_crm_rel_lifecycle_outbox"),
    ):
        op.execute(f"ALTER TABLE {SCHEMA}.{table} DROP CONSTRAINT IF EXISTS {constraint}")
    op.execute(f"ALTER TABLE {SCHEMA}.provider_connection_versions DROP CONSTRAINT IF EXISTS uq_provider_connection_scope_id")
