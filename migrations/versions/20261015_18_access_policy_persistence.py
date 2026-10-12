"""Persist immutable, tenant-scoped Access Policy activation history."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261015_18"
down_revision = "20261014_17"
branch_labels = None
depends_on = None
SCHEMA = "spine"
POLICY_TABLES = (
    "access_policy_versions",
    "access_policy_grants",
    "policy_proposals",
    "policy_approvals",
    "policy_activations",
    "current_access_policies",
)
_ID = "^[a-z][a-z0-9_.:-]{0,63}$"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def _uuid_setting(name: str) -> str:
    setting = f"pg_catalog.current_setting('spine.{name}', true)"
    pattern = "^[0-9A-Fa-f-]{36}$"
    return f"CASE WHEN {setting} OPERATOR(pg_catalog.~) '{pattern}' THEN CAST({setting} AS pg_catalog.uuid) ELSE NULL END"


def _tenant(table: str, *, mutable: bool = False) -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    qualified = f'{SCHEMA}."{table}"'
    scope = f"workspace_id = ({_uuid_setting('workspace_id')}) AND environment_id = ({_uuid_setting('environment_id')})"
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(f'CREATE POLICY "pol_{table}_tenant_isolation" ON {qualified} FOR ALL TO {runtime} USING ({scope}) WITH CHECK ({scope})')
    op.execute(f'CREATE POLICY "pol_{table}_migration_maintenance" ON {qualified} FOR ALL TO {migration} USING (true) WITH CHECK (true)')
    privileges = "SELECT, INSERT, UPDATE" if mutable else "SELECT, INSERT"
    op.execute(f"GRANT {privileges} ON TABLE {qualified} TO {runtime}")
    op.execute(f"REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {qualified} FROM {runtime}")
    if not mutable:
        op.execute(f"REVOKE UPDATE ON TABLE {qualified} FROM {runtime}")


def _immutable_history() -> None:
    runtime = _role("runtime_role")
    op.execute(f"""
        CREATE FUNCTION {SCHEMA}.reject_access_policy_history_mutation() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ BEGIN
            RAISE EXCEPTION 'Access Policy history is immutable.'
                USING ERRCODE = '23514', CONSTRAINT = 'ck_access_policy_history_immutable';
        END; $function$
    """)
    for table in POLICY_TABLES[:-1]:
        op.execute(
            f'CREATE TRIGGER "trg_{table}_immutable" BEFORE UPDATE OR DELETE ON '
            f'{SCHEMA}."{table}" FOR EACH ROW EXECUTE FUNCTION '
            f"{SCHEMA}.reject_access_policy_history_mutation()"
        )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.reject_access_policy_history_mutation() FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SCHEMA}.reject_access_policy_history_mutation() TO {runtime}")


def _current_policy_cas() -> None:
    runtime = _role("runtime_role")
    op.execute(f"""
        CREATE FUNCTION {SCHEMA}.enforce_current_access_policy_cas() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.policy_generation <> 1 OR NEW.cas_version <> 1 THEN
                    RAISE EXCEPTION 'Initial policy head must start at generation one.'
                        USING ERRCODE = '40001', CONSTRAINT = 'ck_current_access_policies_initial_cas';
                END IF;
            ELSIF ROW(NEW.workspace_id, NEW.environment_id, NEW.source_object_id)
                    IS DISTINCT FROM ROW(OLD.workspace_id, OLD.environment_id, OLD.source_object_id)
               OR NEW.policy_generation <> OLD.policy_generation + 1
               OR NEW.cas_version <> OLD.cas_version + 1
               OR NEW.access_policy_version_id = OLD.access_policy_version_id THEN
                RAISE EXCEPTION 'Current policy compare-and-swap failed.'
                    USING ERRCODE = '40001', CONSTRAINT = 'ck_current_access_policies_cas';
            END IF;
            RETURN NEW;
        END; $function$
    """)
    op.execute(f"CREATE TRIGGER trg_current_access_policies_cas BEFORE INSERT OR UPDATE ON {SCHEMA}.current_access_policies FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.enforce_current_access_policy_cas()")
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.enforce_current_access_policy_cas() FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SCHEMA}.enforce_current_access_policy_cas() TO {runtime}")


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    digest = sa.String(64)
    op.create_table(
        "access_policy_versions",
        sa.Column("access_policy_version_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("source_object_id", uuid, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("predecessor_policy_version_id", uuid),
        sa.Column("document_schema_version", sa.Text(), nullable=False),
        sa.Column("document_hash", digest, nullable=False),
        sa.Column("origin_kind", sa.Text(), nullable=False),
        sa.Column("origin_reference", uuid, nullable=False),
        sa.Column("origin_version", sa.Integer(), nullable=False),
        sa.Column("origin_hash", digest, nullable=False),
        sa.Column("created_by_canonical_human_id", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("access_policy_version_id", name="pk_access_policy_versions"),
        sa.UniqueConstraint("workspace_id", "environment_id", "source_object_id", "access_policy_version_id", name="uq_access_policy_versions_scope_source_id"),
        sa.UniqueConstraint("workspace_id", "environment_id", "source_object_id", "version", name="uq_access_policy_versions_source_version"),
        sa.UniqueConstraint("workspace_id", "environment_id", "source_object_id", "document_hash", name="uq_access_policy_versions_source_hash"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "source_object_id"], [f"{SCHEMA}.source_objects.workspace_id", f"{SCHEMA}.source_objects.environment_id", f"{SCHEMA}.source_objects.source_object_id"], name="fk_access_policy_versions_source"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "source_object_id", "predecessor_policy_version_id"], [f"{SCHEMA}.access_policy_versions.workspace_id", f"{SCHEMA}.access_policy_versions.environment_id", f"{SCHEMA}.access_policy_versions.source_object_id", f"{SCHEMA}.access_policy_versions.access_policy_version_id"], name="fk_access_policy_versions_predecessor"),
        sa.CheckConstraint("version > 0 AND origin_version > 0", name="ck_access_policy_versions_positive_versions"),
        sa.CheckConstraint("(version = 1 AND predecessor_policy_version_id IS NULL) OR (version > 1 AND predecessor_policy_version_id IS NOT NULL)", name="ck_access_policy_versions_chain"),
        sa.CheckConstraint("document_hash ~ '^[0-9a-f]{64}$' AND origin_hash ~ '^[0-9a-f]{64}$'", name="ck_access_policy_versions_hashes"),
        sa.CheckConstraint("document_schema_version ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'", name="ck_access_policy_versions_schema"),
        sa.CheckConstraint("origin_kind IN ('eligible_template','approved_source_configuration','approved_proposal')", name="ck_access_policy_versions_origin"),
        schema=SCHEMA,
    )
    op.create_index("ix_access_policy_versions_source", "access_policy_versions", ["workspace_id", "environment_id", "source_object_id", "version"], schema=SCHEMA)

    op.create_table(
        "access_policy_grants",
        sa.Column("access_grant_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("source_object_id", uuid, nullable=False),
        sa.Column("access_policy_version_id", uuid, nullable=False),
        sa.Column("principal_kind", sa.Text(), nullable=False),
        sa.Column("principal_id", uuid, nullable=False),
        sa.Column("permission", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text()),
        sa.Column("operation", sa.Text()),
        sa.Column("valid_from", sa.DateTime(timezone=True)),
        sa.Column("valid_until", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("access_grant_id", name="pk_access_policy_grants"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "source_object_id", "access_policy_version_id"], [f"{SCHEMA}.access_policy_versions.workspace_id", f"{SCHEMA}.access_policy_versions.environment_id", f"{SCHEMA}.access_policy_versions.source_object_id", f"{SCHEMA}.access_policy_versions.access_policy_version_id"], name="fk_access_policy_grants_policy"),
        sa.CheckConstraint("(principal_kind IN ('human','team') AND permission = 'read_content' AND purpose IS NULL AND operation IS NULL AND valid_from IS NULL AND valid_until IS NULL) OR (principal_kind = 'service' AND permission = 'process_content' AND purpose IS NOT NULL AND operation IS NOT NULL)", name="ck_access_policy_grants_authority_shape"),
        sa.CheckConstraint("(valid_from IS NULL AND valid_until IS NULL) OR (valid_from IS NOT NULL AND valid_until IS NOT NULL AND valid_until > valid_from)", name="ck_access_policy_grants_validity"),
        sa.CheckConstraint(f"(purpose IS NULL OR purpose ~ '{_ID}') AND (operation IS NULL OR operation ~ '{_ID}')", name="ck_access_policy_grants_service_identifiers"),
        schema=SCHEMA,
    )
    op.create_index("uq_access_policy_grants_normalized", "access_policy_grants", ["workspace_id", "environment_id", "access_policy_version_id", "principal_kind", "principal_id", "permission", "purpose", "operation", "valid_from", "valid_until"], unique=True, schema=SCHEMA, postgresql_nulls_not_distinct=True)
    op.create_index("ix_access_policy_grants_principal", "access_policy_grants", ["workspace_id", "environment_id", "principal_kind", "principal_id"], schema=SCHEMA)

    op.create_table(
        "policy_proposals",
        sa.Column("policy_proposal_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("source_object_id", uuid, nullable=False),
        sa.Column("proposal_version", sa.Integer(), nullable=False),
        sa.Column("predecessor_proposal_id", uuid),
        sa.Column("proposed_policy_version_id", uuid, nullable=False),
        sa.Column("proposed_policy_hash", digest, nullable=False),
        sa.Column("requester_canonical_human_id", uuid, nullable=False),
        sa.Column("expected_policy_version_id", uuid),
        sa.Column("expected_policy_generation", sa.BigInteger(), nullable=False),
        sa.Column("benefit_classification", sa.Text(), nullable=False),
        sa.Column("benefit_schema_version", sa.Text(), nullable=False),
        sa.Column("membership_generation", sa.BigInteger(), nullable=False),
        sa.Column("authority_generation", sa.BigInteger(), nullable=False),
        sa.Column("service_authority_generation", sa.BigInteger(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("policy_proposal_id", name="pk_policy_proposals"),
        sa.UniqueConstraint("workspace_id", "environment_id", "source_object_id", "policy_proposal_id", name="uq_policy_proposals_scope_source_id"),
        sa.UniqueConstraint("workspace_id", "environment_id", "source_object_id", "policy_proposal_id", "proposal_version", "proposed_policy_hash", name="uq_policy_proposals_exact"),
        sa.UniqueConstraint("workspace_id", "environment_id", "source_object_id", "proposal_version", name="uq_policy_proposals_source_version"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "source_object_id", "proposed_policy_version_id"], [f"{SCHEMA}.access_policy_versions.workspace_id", f"{SCHEMA}.access_policy_versions.environment_id", f"{SCHEMA}.access_policy_versions.source_object_id", f"{SCHEMA}.access_policy_versions.access_policy_version_id"], name="fk_policy_proposals_policy"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "source_object_id", "predecessor_proposal_id"], [f"{SCHEMA}.policy_proposals.workspace_id", f"{SCHEMA}.policy_proposals.environment_id", f"{SCHEMA}.policy_proposals.source_object_id", f"{SCHEMA}.policy_proposals.policy_proposal_id"], name="fk_policy_proposals_predecessor"),
        sa.CheckConstraint("expected_policy_generation >= 0 AND membership_generation > 0 AND authority_generation > 0 AND service_authority_generation > 0", name="ck_policy_proposals_generations"),
        sa.CheckConstraint("benefit_classification IN ('self_benefiting','other_only','revocation')", name="ck_policy_proposals_benefit"),
        schema=SCHEMA,
    )
    op.create_index("ix_policy_proposals_baseline", "policy_proposals", ["workspace_id", "environment_id", "source_object_id", "expected_policy_generation"], schema=SCHEMA)

    op.create_table(
        "policy_approvals",
        sa.Column("policy_approval_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("source_object_id", uuid, nullable=False),
        sa.Column("policy_proposal_id", uuid, nullable=False),
        sa.Column("proposal_version", sa.Integer(), nullable=False),
        sa.Column("proposal_hash", digest, nullable=False),
        sa.Column("approver_canonical_human_id", uuid, nullable=False),
        sa.Column("identity_proof_reference", uuid, nullable=False),
        sa.Column("authority_snapshot_reference", uuid, nullable=False),
        sa.Column("membership_generation", sa.BigInteger(), nullable=False),
        sa.Column("authority_generation", sa.BigInteger(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("policy_approval_id", name="pk_policy_approvals"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "source_object_id", "policy_proposal_id", "proposal_version", "proposal_hash"], [f"{SCHEMA}.policy_proposals.workspace_id", f"{SCHEMA}.policy_proposals.environment_id", f"{SCHEMA}.policy_proposals.source_object_id", f"{SCHEMA}.policy_proposals.policy_proposal_id", f"{SCHEMA}.policy_proposals.proposal_version", f"{SCHEMA}.policy_proposals.proposed_policy_hash"], name="fk_policy_approvals_exact_proposal"),
        sa.UniqueConstraint("workspace_id", "environment_id", "policy_proposal_id", "approver_canonical_human_id", "decision", name="uq_policy_approvals_proposal_approver_decision"),
        sa.CheckConstraint("decision IN ('approved','rejected')", name="ck_policy_approvals_decision"),
        sa.CheckConstraint(f"reason_code ~ '{_ID}'", name="ck_policy_approvals_reason"),
        schema=SCHEMA,
    )
    op.create_index("ix_policy_approvals_proposal", "policy_approvals", ["workspace_id", "environment_id", "policy_proposal_id", "decided_at"], schema=SCHEMA)

    op.create_table(
        "policy_activations",
        sa.Column("policy_activation_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("source_object_id", uuid, nullable=False),
        sa.Column("predecessor_policy_version_id", uuid),
        sa.Column("new_policy_version_id", uuid, nullable=False),
        sa.Column("policy_generation", sa.BigInteger(), nullable=False),
        sa.Column("policy_proposal_id", uuid),
        sa.Column("policy_approval_id", uuid),
        sa.Column("initial_eligibility_decision_id", uuid),
        sa.Column("requester_canonical_human_id", uuid, nullable=False),
        sa.Column("activator_canonical_human_id", uuid, nullable=False),
        sa.Column("membership_generation", sa.BigInteger(), nullable=False),
        sa.Column("authority_generation", sa.BigInteger(), nullable=False),
        sa.Column("service_authority_generation", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_receipt_id", uuid, nullable=False),
        sa.Column("audit_event_id", uuid, nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("policy_activation_id", name="pk_policy_activations"),
        sa.UniqueConstraint("workspace_id", "environment_id", "source_object_id", "policy_activation_id", "new_policy_version_id", "policy_generation", name="uq_policy_activations_head_tuple"),
        sa.UniqueConstraint("workspace_id", "environment_id", "source_object_id", "new_policy_version_id", name="uq_policy_activations_source_policy"),
        sa.UniqueConstraint("workspace_id", "environment_id", "source_object_id", "policy_generation", name="uq_policy_activations_source_generation"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "source_object_id", "new_policy_version_id"], [f"{SCHEMA}.access_policy_versions.workspace_id", f"{SCHEMA}.access_policy_versions.environment_id", f"{SCHEMA}.access_policy_versions.source_object_id", f"{SCHEMA}.access_policy_versions.access_policy_version_id"], name="fk_policy_activations_new_policy"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "source_object_id", "predecessor_policy_version_id"], [f"{SCHEMA}.access_policy_versions.workspace_id", f"{SCHEMA}.access_policy_versions.environment_id", f"{SCHEMA}.access_policy_versions.source_object_id", f"{SCHEMA}.access_policy_versions.access_policy_version_id"], name="fk_policy_activations_predecessor"),
        sa.ForeignKeyConstraint(["policy_approval_id"], [f"{SCHEMA}.policy_approvals.policy_approval_id"], name="fk_policy_activations_approval"),
        sa.ForeignKeyConstraint(["idempotency_receipt_id"], [f"{SCHEMA}.idempotency_receipts.receipt_id"], name="fk_policy_activations_idempotency"),
        sa.ForeignKeyConstraint(["audit_event_id"], [f"{SCHEMA}.audit_events.audit_event_id"], name="fk_policy_activations_audit", deferrable=True, initially="DEFERRED"),
        sa.CheckConstraint("policy_generation > 0 AND membership_generation > 0 AND authority_generation > 0 AND service_authority_generation > 0", name="ck_policy_activations_generations"),
        sa.CheckConstraint("(policy_generation = 1 AND predecessor_policy_version_id IS NULL AND initial_eligibility_decision_id IS NOT NULL) OR (policy_generation > 1 AND predecessor_policy_version_id IS NOT NULL AND policy_proposal_id IS NOT NULL AND initial_eligibility_decision_id IS NULL)", name="ck_policy_activations_evidence_shape"),
        schema=SCHEMA,
    )
    op.create_index("ix_policy_activations_source", "policy_activations", ["workspace_id", "environment_id", "source_object_id", "policy_generation"], schema=SCHEMA)

    op.create_table(
        "current_access_policies",
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("source_object_id", uuid, nullable=False),
        sa.Column("access_policy_version_id", uuid, nullable=False),
        sa.Column("policy_activation_id", uuid, nullable=False),
        sa.Column("policy_generation", sa.BigInteger(), nullable=False),
        sa.Column("cas_version", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("source_object_id", name="pk_current_access_policies"),
        sa.UniqueConstraint("workspace_id", "environment_id", "source_object_id", name="uq_current_access_policies_scope_source"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "source_object_id"], [f"{SCHEMA}.source_objects.workspace_id", f"{SCHEMA}.source_objects.environment_id", f"{SCHEMA}.source_objects.source_object_id"], name="fk_current_access_policies_source"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "source_object_id", "policy_activation_id", "access_policy_version_id", "policy_generation"], [f"{SCHEMA}.policy_activations.workspace_id", f"{SCHEMA}.policy_activations.environment_id", f"{SCHEMA}.policy_activations.source_object_id", f"{SCHEMA}.policy_activations.policy_activation_id", f"{SCHEMA}.policy_activations.new_policy_version_id", f"{SCHEMA}.policy_activations.policy_generation"], name="fk_current_access_policies_activation"),
        sa.CheckConstraint("policy_generation > 0 AND cas_version > 0", name="ck_current_access_policies_positive_versions"),
        schema=SCHEMA,
    )
    op.create_index("ix_current_access_policies_policy_generation", "current_access_policies", ["workspace_id", "environment_id", "access_policy_version_id", "policy_generation"], schema=SCHEMA)

    for table in POLICY_TABLES[:-1]:
        _tenant(table)
    _tenant("current_access_policies", mutable=True)
    _immutable_history()
    _current_policy_cas()


def downgrade() -> None:
    runtime = _role("runtime_role")
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.enforce_current_access_policy_cas() FROM {runtime}")
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.reject_access_policy_history_mutation() FROM {runtime}")
    for table in reversed(POLICY_TABLES):
        op.drop_table(table, schema=SCHEMA)
    op.execute(f"DROP FUNCTION {SCHEMA}.enforce_current_access_policy_cas()")
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_access_policy_history_mutation()")
