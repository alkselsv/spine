"""Expand canonical readers for processing admission and acceptance v2."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261227_95"
down_revision = "20261226_94"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, attribute))


def upgrade() -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    uuid = postgresql.UUID(as_uuid=True)

    op.create_unique_constraint(
        "uq_source_receipt_associations_processing_binding",
        "source_revision_receipt_associations",
        ["association_id", "workspace_id", "environment_id", "source_object_id", "revision_id", "original_sha256", "byte_length"],
        schema=SCHEMA,
    )
    op.create_unique_constraint(
        "uq_trusted_content_evidence_processing_binding",
        "trusted_content_verification_evidence",
        ["evidence_id", "workspace_id", "environment_id", "source_object_id", "source_revision_id", "receipt_association_id", "original_sha256", "byte_length", "evidence_digest"],
        schema=SCHEMA,
    )
    op.create_unique_constraint(
        "uq_retention_applicabilities_processing_binding",
        "retention_applicabilities",
        ["workspace_id", "environment_id", "source_object_id", "applicability_id", "generation"],
        schema=SCHEMA,
    )
    op.create_table(
        "source_processing_admissions",
        sa.Column("processing_admission_id", uuid, nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("source_object_id", uuid, nullable=False),
        sa.Column("source_revision_id", uuid, nullable=False),
        sa.Column("selector_id", uuid, nullable=False),
        sa.Column("provider_connection_id", uuid, nullable=False),
        sa.Column("provider_connection_version_id", uuid, nullable=False),
        sa.Column("provider_approval_id", uuid, nullable=False),
        sa.Column("resource_scope_reference", uuid, nullable=False),
        sa.Column("authorization_generation", sa.Integer(), nullable=False),
        sa.Column("configuration_generation", sa.Integer(), nullable=False),
        sa.Column("grant_binding_id", uuid, nullable=False),
        sa.Column("grant_token_epoch", sa.BigInteger(), nullable=False),
        sa.Column("grant_security_fence", sa.BigInteger(), nullable=False),
        sa.Column("root_policy_id", uuid, nullable=False),
        sa.Column("root_policy_version", sa.String(128), nullable=False),
        sa.Column("root_policy_digest", sa.String(64), nullable=False),
        sa.Column("content_policy_id", uuid, nullable=False),
        sa.Column("content_policy_version", sa.String(128), nullable=False),
        sa.Column("content_policy_digest", sa.String(64), nullable=False),
        sa.Column("receipt_association_id", uuid, nullable=False),
        sa.Column("verified_sha256", sa.String(64), nullable=False),
        sa.Column("verified_byte_length", sa.Integer(), nullable=False),
        sa.Column("verifier_evidence_id", uuid, nullable=False),
        sa.Column("verifier_profile_version", sa.String(128), nullable=False),
        sa.Column("verifier_evidence_digest", sa.String(64), nullable=False),
        sa.Column("lifecycle_eligibility_reference", uuid, nullable=False),
        sa.Column("lifecycle_eligibility_generation", sa.Integer(), nullable=False),
        sa.Column("retention_applicability_reference", uuid, nullable=False),
        sa.Column("retention_applicability_generation", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("processor_capability_id", sa.String(128)),
        sa.Column("processor_capability_version", sa.String(128)),
        sa.Column("processor_profile_version", sa.String(128)),
        sa.Column("processor_qualification_reference", uuid),
        sa.Column("processor_qualification_digest", sa.String(64)),
        sa.Column("opaque_approval_id", uuid),
        sa.Column("opaque_approval_version", sa.String(128)),
        sa.Column("opaque_approval_digest", sa.String(64)),
        sa.Column("opaque_approved_purpose", sa.String(64)),
        sa.Column("opaque_security_controls_reference", uuid),
        sa.Column("idempotency_receipt_id", uuid, nullable=False),
        sa.Column("audit_event_id", uuid, nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("processing_admission_id", name="pk_source_processing_admissions"),
        sa.UniqueConstraint("workspace_id", "environment_id", "source_object_id", "source_revision_id", "processing_admission_id", name="uq_source_processing_admissions_acceptance_binding"),
        sa.UniqueConstraint("workspace_id", "environment_id", "source_revision_id", name="uq_source_processing_admissions_revision"),
        sa.UniqueConstraint("workspace_id", "environment_id", "idempotency_receipt_id", name="uq_source_processing_admissions_idempotency"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "source_object_id", "source_revision_id"], [f"{SCHEMA}.source_revisions.workspace_id", f"{SCHEMA}.source_revisions.environment_id", f"{SCHEMA}.source_revisions.source_object_id", f"{SCHEMA}.source_revisions.revision_id"], name="fk_source_processing_admissions_revision"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "selector_id"], [f"{SCHEMA}.trusted_canonical_acceptance_selector_versions.workspace_id", f"{SCHEMA}.trusted_canonical_acceptance_selector_versions.environment_id", f"{SCHEMA}.trusted_canonical_acceptance_selector_versions.selector_id"], name="fk_source_processing_admissions_selector"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "provider_connection_id", "provider_connection_version_id", "provider_approval_id"], [f"{SCHEMA}.provider_connection_versions.workspace_id", f"{SCHEMA}.provider_connection_versions.environment_id", f"{SCHEMA}.provider_connection_versions.connection_id", f"{SCHEMA}.provider_connection_versions.connection_version_id", f"{SCHEMA}.provider_connection_versions.approval_id"], name="fk_source_processing_admissions_connection"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "root_policy_id", "root_policy_version", "root_policy_digest"], [f"{SCHEMA}.bitrix24_disk_root_policy_versions.workspace_id", f"{SCHEMA}.bitrix24_disk_root_policy_versions.environment_id", f"{SCHEMA}.bitrix24_disk_root_policy_versions.policy_id", f"{SCHEMA}.bitrix24_disk_root_policy_versions.version", f"{SCHEMA}.bitrix24_disk_root_policy_versions.digest"], name="fk_source_processing_admissions_root_policy"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "content_policy_id", "content_policy_version", "content_policy_digest"], [f"{SCHEMA}.bitrix24_disk_content_policy_versions.workspace_id", f"{SCHEMA}.bitrix24_disk_content_policy_versions.environment_id", f"{SCHEMA}.bitrix24_disk_content_policy_versions.policy_id", f"{SCHEMA}.bitrix24_disk_content_policy_versions.version", f"{SCHEMA}.bitrix24_disk_content_policy_versions.digest"], name="fk_source_processing_admissions_content_policy"),
        sa.ForeignKeyConstraint(["receipt_association_id", "workspace_id", "environment_id", "source_object_id", "source_revision_id", "verified_sha256", "verified_byte_length"], [f"{SCHEMA}.source_revision_receipt_associations.association_id", f"{SCHEMA}.source_revision_receipt_associations.workspace_id", f"{SCHEMA}.source_revision_receipt_associations.environment_id", f"{SCHEMA}.source_revision_receipt_associations.source_object_id", f"{SCHEMA}.source_revision_receipt_associations.revision_id", f"{SCHEMA}.source_revision_receipt_associations.original_sha256", f"{SCHEMA}.source_revision_receipt_associations.byte_length"], name="fk_source_processing_admissions_exact_receipt"),
        sa.ForeignKeyConstraint(["verifier_evidence_id", "workspace_id", "environment_id", "source_object_id", "source_revision_id", "receipt_association_id", "verified_sha256", "verified_byte_length", "verifier_evidence_digest"], [f"{SCHEMA}.trusted_content_verification_evidence.evidence_id", f"{SCHEMA}.trusted_content_verification_evidence.workspace_id", f"{SCHEMA}.trusted_content_verification_evidence.environment_id", f"{SCHEMA}.trusted_content_verification_evidence.source_object_id", f"{SCHEMA}.trusted_content_verification_evidence.source_revision_id", f"{SCHEMA}.trusted_content_verification_evidence.receipt_association_id", f"{SCHEMA}.trusted_content_verification_evidence.original_sha256", f"{SCHEMA}.trusted_content_verification_evidence.byte_length", f"{SCHEMA}.trusted_content_verification_evidence.evidence_digest"], name="fk_source_processing_admissions_verifier"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "source_object_id", "lifecycle_eligibility_reference", "lifecycle_eligibility_generation"], [f"{SCHEMA}.lifecycle_decisions.workspace_id", f"{SCHEMA}.lifecycle_decisions.environment_id", f"{SCHEMA}.lifecycle_decisions.source_object_id", f"{SCHEMA}.lifecycle_decisions.decision_id", f"{SCHEMA}.lifecycle_decisions.generation"], name="fk_source_processing_admissions_lifecycle"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "source_object_id", "retention_applicability_reference", "retention_applicability_generation"], [f"{SCHEMA}.retention_applicabilities.workspace_id", f"{SCHEMA}.retention_applicabilities.environment_id", f"{SCHEMA}.retention_applicabilities.source_object_id", f"{SCHEMA}.retention_applicabilities.applicability_id", f"{SCHEMA}.retention_applicabilities.generation"], name="fk_source_processing_admissions_retention"),
        sa.ForeignKeyConstraint(["idempotency_receipt_id"], [f"{SCHEMA}.idempotency_receipts.receipt_id"], name="fk_source_processing_admissions_idempotency"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "audit_event_id"], [f"{SCHEMA}.audit_events.workspace_id", f"{SCHEMA}.audit_events.environment_id", f"{SCHEMA}.audit_events.audit_event_id"], name="fk_source_processing_admissions_audit", deferrable=True, initially="DEFERRED"),
        sa.CheckConstraint("schema_version = 'source-processing-admission:v1'", name=op.f("ck_source_processing_admissions_schema")),
        sa.CheckConstraint("authorization_generation >= 1 AND configuration_generation >= 1 AND grant_token_epoch >= 1 AND grant_security_fence >= 0 AND lifecycle_eligibility_generation >= 1 AND retention_applicability_generation >= 1 AND verified_byte_length >= 0", name=op.f("ck_source_processing_admissions_generations")),
        sa.CheckConstraint("verified_sha256 ~ '^[0-9a-f]{64}$' AND verifier_evidence_digest ~ '^[0-9a-f]{64}$' AND root_policy_digest ~ '^[0-9a-f]{64}$' AND content_policy_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_source_processing_admissions_digests")),
        sa.CheckConstraint("(mode = 'processor_required' AND processor_capability_id IS NOT NULL AND processor_capability_version IS NOT NULL AND processor_profile_version IS NOT NULL AND processor_qualification_reference IS NOT NULL AND processor_qualification_digest ~ '^[0-9a-f]{64}$' AND opaque_approval_id IS NULL AND opaque_approval_version IS NULL AND opaque_approval_digest IS NULL AND opaque_approved_purpose IS NULL AND opaque_security_controls_reference IS NULL) OR (mode = 'opaque_acceptance' AND processor_capability_id IS NULL AND processor_capability_version IS NULL AND processor_profile_version IS NULL AND processor_qualification_reference IS NULL AND processor_qualification_digest IS NULL AND opaque_approval_id IS NOT NULL AND opaque_approval_version IS NOT NULL AND opaque_approval_digest ~ '^[0-9a-f]{64}$' AND opaque_approved_purpose IS NOT NULL AND opaque_security_controls_reference IS NOT NULL)", name=op.f("ck_source_processing_admissions_mode")),
        schema=SCHEMA,
    )
    op.create_index("ix_source_processing_admissions_revision", "source_processing_admissions", ["workspace_id", "environment_id", "source_object_id", "source_revision_id"], schema=SCHEMA)
    op.execute(f"ALTER TABLE {SCHEMA}.source_processing_admissions ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {SCHEMA}.source_processing_admissions FORCE ROW LEVEL SECURITY")
    predicate = "workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid"
    op.execute(f"CREATE POLICY pol_source_processing_admissions_tenant_isolation ON {SCHEMA}.source_processing_admissions FOR ALL TO {runtime} USING ({predicate}) WITH CHECK ({predicate})")
    op.execute(f"CREATE POLICY pol_source_processing_admissions_migration_maintenance ON {SCHEMA}.source_processing_admissions FOR ALL TO {migration} USING (true) WITH CHECK (true)")
    op.execute(f"GRANT SELECT, INSERT ON TABLE {SCHEMA}.source_processing_admissions TO {runtime}")
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {SCHEMA}.source_processing_admissions FROM {runtime}")
    op.execute(f"CREATE FUNCTION {SCHEMA}.reject_source_processing_admission_mutation() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, spine AS $fn$ BEGIN RAISE EXCEPTION 'Source processing admissions are immutable.' USING ERRCODE = '23514'; END; $fn$")
    op.execute(f"CREATE TRIGGER trg_source_processing_admissions_immutable BEFORE UPDATE OR DELETE ON {SCHEMA}.source_processing_admissions FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_source_processing_admission_mutation()")

    op.add_column("canonical_acceptances", sa.Column("canonical_acceptance_schema_version", sa.Integer(), nullable=False, server_default="1"), schema=SCHEMA)
    op.add_column("canonical_acceptances", sa.Column("processing_admission_id", uuid), schema=SCHEMA)
    op.create_check_constraint(op.f("ck_canonical_acceptances_schema_processing"), "canonical_acceptances", "(canonical_acceptance_schema_version = 1 AND processing_admission_id IS NULL) OR (canonical_acceptance_schema_version = 2 AND processing_admission_id IS NOT NULL AND admission_decision_id IS NOT NULL AND access_policy_version_id IS NOT NULL)", schema=SCHEMA)
    op.create_foreign_key("fk_canonical_acceptances_processing_admission", "canonical_acceptances", "source_processing_admissions", ["workspace_id", "environment_id", "source_object_id", "source_revision_id", "processing_admission_id"], ["workspace_id", "environment_id", "source_object_id", "source_revision_id", "processing_admission_id"], source_schema=SCHEMA, referent_schema=SCHEMA)
    op.create_index("ix_canonical_acceptances_processing_admission", "canonical_acceptances", ["workspace_id", "environment_id", "processing_admission_id"], schema=SCHEMA)
    op.execute(f"CREATE FUNCTION {SCHEMA}.deny_canonical_acceptance_v2_write() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, spine AS $fn$ BEGIN IF NEW.canonical_acceptance_schema_version = 2 THEN RAISE EXCEPTION 'Canonical acceptance v2 writes are disabled during reader-first expansion.' USING ERRCODE = '55000'; END IF; RETURN NEW; END; $fn$")
    op.execute(f"CREATE TRIGGER trg_canonical_acceptance_v2_disabled BEFORE INSERT ON {SCHEMA}.canonical_acceptances FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.deny_canonical_acceptance_v2_write()")


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {SCHEMA}.canonical_acceptances WHERE canonical_acceptance_schema_version = 2)")).scalar():
        raise RuntimeError("cannot contract processing-admission schema while v2 history exists")
    op.execute(f"DROP TRIGGER trg_canonical_acceptance_v2_disabled ON {SCHEMA}.canonical_acceptances")
    op.execute(f"DROP FUNCTION {SCHEMA}.deny_canonical_acceptance_v2_write()")
    op.drop_index("ix_canonical_acceptances_processing_admission", table_name="canonical_acceptances", schema=SCHEMA)
    op.drop_constraint("fk_canonical_acceptances_processing_admission", "canonical_acceptances", schema=SCHEMA, type_="foreignkey")
    op.drop_constraint(op.f("ck_canonical_acceptances_schema_processing"), "canonical_acceptances", schema=SCHEMA, type_="check")
    op.drop_column("canonical_acceptances", "processing_admission_id", schema=SCHEMA)
    op.drop_column("canonical_acceptances", "canonical_acceptance_schema_version", schema=SCHEMA)
    op.drop_table("source_processing_admissions", schema=SCHEMA)
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_source_processing_admission_mutation()")
    op.drop_constraint("uq_trusted_content_evidence_processing_binding", "trusted_content_verification_evidence", schema=SCHEMA, type_="unique")
    op.drop_constraint("uq_retention_applicabilities_processing_binding", "retention_applicabilities", schema=SCHEMA, type_="unique")
    op.drop_constraint("uq_source_receipt_associations_processing_binding", "source_revision_receipt_associations", schema=SCHEMA, type_="unique")
