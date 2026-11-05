"""Persist qualified deletion evidence and trusted tombstone authorization."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings

revision = "20270111_110"
down_revision = "20270110_109"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, attribute))


def _tenant_table(name: str, *, mutable: bool = False) -> None:
    runtime_role = _role("runtime_role")
    migration_role = _role("migration_role")
    op.execute(f"ALTER TABLE {SCHEMA}.{name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {SCHEMA}.{name} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY pol_{name}_tenant_isolation ON {SCHEMA}.{name} "
        f"FOR ALL TO {runtime_role} "
        "USING (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid) "
        "WITH CHECK (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid)"
    )
    op.execute(
        f"CREATE POLICY pol_{name}_migration_maintenance ON {SCHEMA}.{name} "
        f"FOR ALL TO {migration_role} USING (true) WITH CHECK (true)"
    )
    grants = "SELECT, INSERT, UPDATE" if mutable else "SELECT, INSERT"
    op.execute(f"GRANT {grants} ON TABLE {SCHEMA}.{name} TO {runtime_role}")
    op.execute(
        f"REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE "
        f"{SCHEMA}.{name} FROM {runtime_role}"
    )


def _immutable(name: str) -> None:
    function = f"reject_{name}_mutation"
    op.execute(f"""
        CREATE FUNCTION {SCHEMA}.{function}() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ BEGIN
            RAISE EXCEPTION 'Deletion authority records are immutable.'
                USING ERRCODE = '23514', CONSTRAINT = 'ck_{name}_immutable';
        END; $function$
        """)
    op.execute(
        f"CREATE TRIGGER trg_{name}_immutable BEFORE UPDATE OR DELETE ON "
        f"{SCHEMA}.{name} FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.{function}()"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.{function}() FROM PUBLIC")


def _current_guard(name: str, current_id: str) -> None:
    function = f"guard_{name}_step"
    op.execute(f"""
        CREATE FUNCTION {SCHEMA}.{function}() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.generation <> 1 THEN
                    RAISE EXCEPTION 'Deletion authority baseline is invalid.'
                        USING ERRCODE = '23514', CONSTRAINT = 'ck_{name}_step';
                END IF;
            ELSIF NEW.generation <> OLD.generation + 1
               OR NEW.{current_id} IS NOT DISTINCT FROM OLD.{current_id} THEN
                RAISE EXCEPTION 'Deletion authority generation CAS failed.'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_{name}_step';
            END IF;
            RETURN NEW;
        END; $function$
        """)
    op.execute(
        f"CREATE TRIGGER trg_{name}_step BEFORE INSERT OR UPDATE ON {SCHEMA}.{name} "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.{function}()"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.{function}() FROM PUBLIC")


def upgrade() -> None:
    runtime_role = _role("runtime_role")
    op.create_unique_constraint(
        "uq_qualification_authority_tenant_principal",
        "qualification_authority_bindings",
        [
            "workspace_id",
            "environment_id",
            "binding_id",
            "version",
            "principal_id",
        ],
        schema=SCHEMA,
    )
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.resolve_current_deletion_qualification_authority(
            requested_principal_id uuid,
            requested_workspace_id uuid,
            requested_environment_id uuid,
            requested_purpose text,
            requested_operation text
        ) RETURNS TABLE(binding_id uuid, version bigint)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
            WITH current_authority AS (
                SELECT authority.binding_id, authority.version, authority.status,
                       authority.purpose, authority.operation, authority.expires_at
                  FROM {SCHEMA}.qualification_authority_bindings AS authority
                 WHERE authority.workspace_id = requested_workspace_id
                   AND authority.environment_id = requested_environment_id
                   AND authority.principal_kind = 'service'
                   AND authority.principal_id = requested_principal_id
                   AND authority.purpose = requested_purpose
                   AND authority.operation = requested_operation
                 ORDER BY authority.version DESC
                 LIMIT 1
            )
            SELECT authority.binding_id, authority.version
              FROM current_authority AS authority
             WHERE authority.status = 'active'
               AND authority.expires_at > transaction_timestamp()
               AND current_setting('spine.workspace_id', true)
                   = requested_workspace_id::text
               AND current_setting('spine.environment_id', true)
                   = requested_environment_id::text
            ;
        $function$
        """
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}."
        "resolve_current_deletion_qualification_authority"
        "(uuid,uuid,uuid,text,text) FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {SCHEMA}."
        "resolve_current_deletion_qualification_authority"
        f"(uuid,uuid,uuid,text,text) TO {runtime_role}"
    )
    op.create_table(
        "qualified_deletion_evidence",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "connection_version_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "resource_scope_reference", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "scope_approval_version_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("scope_approval_digest", sa.String(64), nullable=False),
        sa.Column(
            "qualifier_service_principal_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "qualification_authority_reference",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("qualification_authority_generation", sa.Integer(), nullable=False),
        sa.Column("observation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_id", sa.String(128), nullable=False),
        sa.Column("profile_version", sa.String(64), nullable=False),
        sa.Column("evidence_digest", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(64), nullable=False),
        sa.Column("fresh_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "environment_id",
            "evidence_id",
            name="pk_qualified_deletion_evidence",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "evidence_id",
            "source_object_id",
            "evidence_digest",
            name="uq_qualified_deletion_evidence_exact",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id"],
            [
                f"{SCHEMA}.source_objects.workspace_id",
                f"{SCHEMA}.source_objects.environment_id",
                f"{SCHEMA}.source_objects.source_object_id",
            ],
            name="fk_qualified_deletion_evidence_source",
        ),
        sa.ForeignKeyConstraint(
            [
                "workspace_id",
                "environment_id",
                "qualification_authority_reference",
                "qualification_authority_generation",
                "qualifier_service_principal_id",
            ],
            [
                f"{SCHEMA}.qualification_authority_bindings.workspace_id",
                f"{SCHEMA}.qualification_authority_bindings.environment_id",
                f"{SCHEMA}.qualification_authority_bindings.binding_id",
                f"{SCHEMA}.qualification_authority_bindings.version",
                f"{SCHEMA}.qualification_authority_bindings.principal_id",
            ],
            name="fk_qualified_deletion_evidence_qualification_authority",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "source_deletion_policy_approvals",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("policy_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("policy_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("policy_digest", sa.String(64), nullable=False),
        sa.Column("source_contract_id", sa.String(128), nullable=False),
        sa.Column("source_contract_version", sa.String(64), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "resource_scope_reference", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("approver_human_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("authority_generation", sa.Integer(), nullable=False),
        sa.Column("audit_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "environment_id",
            "approval_id",
            name="pk_source_deletion_policy_approvals",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "approval_id",
            "policy_version_id",
            "policy_digest",
            name="uq_source_deletion_policy_approvals_exact",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "source_contract_id",
            "source_contract_version",
            "connection_id",
            "resource_scope_reference",
            "authority_generation",
            name="uq_source_deletion_policy_approval_generation",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "audit_event_id"],
            [
                f"{SCHEMA}.audit_events.workspace_id",
                f"{SCHEMA}.audit_events.environment_id",
                f"{SCHEMA}.audit_events.audit_event_id",
            ],
            name="fk_source_deletion_policy_approval_audit",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint(
            "authority_generation >= 1",
            name="ck_source_deletion_policy_approval_generation",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "current_source_deletion_policy_approvals",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_contract_id", sa.String(128), nullable=False),
        sa.Column("source_contract_version", sa.String(64), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "resource_scope_reference", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("current_approval_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "environment_id",
            "source_contract_id",
            "source_contract_version",
            "connection_id",
            "resource_scope_reference",
            name="pk_current_source_deletion_policy_approvals",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "current_approval_id"],
            [
                f"{SCHEMA}.source_deletion_policy_approvals.workspace_id",
                f"{SCHEMA}.source_deletion_policy_approvals.environment_id",
                f"{SCHEMA}.source_deletion_policy_approvals.approval_id",
            ],
            name="fk_current_source_deletion_policy_approval",
        ),
        sa.CheckConstraint(
            "generation >= 1",
            name="ck_current_source_deletion_policy_approval_generation",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "tombstone_authorization_issuers",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("issuer_authority_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "service_principal_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "authorized_by_human_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("authority_generation", sa.Integer(), nullable=False),
        sa.Column("authority_digest", sa.String(64), nullable=False),
        sa.Column("audit_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "environment_id",
            "issuer_authority_id",
            name="pk_tombstone_authorization_issuers",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "issuer_authority_id",
            "service_principal_id",
            "authority_generation",
            name="uq_tombstone_authorization_issuers_exact",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "authority_generation",
            name="uq_tombstone_authorization_issuer_generation",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "audit_event_id"],
            [
                f"{SCHEMA}.audit_events.workspace_id",
                f"{SCHEMA}.audit_events.environment_id",
                f"{SCHEMA}.audit_events.audit_event_id",
            ],
            name="fk_tombstone_authorization_issuer_audit",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint(
            "authority_generation >= 1",
            name="ck_tombstone_authorization_issuer_generation",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "current_tombstone_authorization_issuers",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "current_issuer_authority_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "environment_id",
            name="pk_current_tombstone_authorization_issuers",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "current_issuer_authority_id"],
            [
                f"{SCHEMA}.tombstone_authorization_issuers.workspace_id",
                f"{SCHEMA}.tombstone_authorization_issuers.environment_id",
                f"{SCHEMA}.tombstone_authorization_issuers.issuer_authority_id",
            ],
            name="fk_current_tombstone_authorization_issuer",
        ),
        sa.CheckConstraint(
            "generation >= 1",
            name="ck_current_tombstone_authorization_issuer_generation",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "tombstone_authorizations",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("authorization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "qualified_evidence_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("qualified_evidence_digest", sa.String(64), nullable=False),
        sa.Column(
            "deletion_policy_approval_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "deletion_policy_version_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("deletion_policy_digest", sa.String(64), nullable=False),
        sa.Column("issuer_authority_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "issuer_service_principal_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("issuer_authority_generation", sa.Integer(), nullable=False),
        sa.Column(
            "expected_current_revision_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "expected_current_acceptance_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("expected_current_cas_version", sa.Integer(), nullable=False),
        sa.Column(
            "tombstone_revision_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("transition_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("command_digest", sa.String(64), nullable=False),
        sa.Column("audit_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "environment_id",
            "authorization_id",
            name="pk_tombstone_authorizations",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "idempotency_key",
            name="uq_tombstone_authorizations_command",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "source_object_id",
            "expected_current_revision_id",
            "expected_current_acceptance_id",
            "expected_current_cas_version",
            name="uq_tombstone_authorizations_expected_head",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "source_object_id",
            "transition_id",
            name="uq_tombstone_authorizations_transition",
        ),
        sa.ForeignKeyConstraint(
            [
                "workspace_id",
                "environment_id",
                "qualified_evidence_id",
                "source_object_id",
                "qualified_evidence_digest",
            ],
            [
                f"{SCHEMA}.qualified_deletion_evidence.workspace_id",
                f"{SCHEMA}.qualified_deletion_evidence.environment_id",
                f"{SCHEMA}.qualified_deletion_evidence.evidence_id",
                f"{SCHEMA}.qualified_deletion_evidence.source_object_id",
                f"{SCHEMA}.qualified_deletion_evidence.evidence_digest",
            ],
            name="fk_tombstone_authorizations_evidence",
        ),
        sa.ForeignKeyConstraint(
            [
                "workspace_id",
                "environment_id",
                "deletion_policy_approval_id",
                "deletion_policy_version_id",
                "deletion_policy_digest",
            ],
            [
                f"{SCHEMA}.source_deletion_policy_approvals.workspace_id",
                f"{SCHEMA}.source_deletion_policy_approvals.environment_id",
                f"{SCHEMA}.source_deletion_policy_approvals.approval_id",
                f"{SCHEMA}.source_deletion_policy_approvals.policy_version_id",
                f"{SCHEMA}.source_deletion_policy_approvals.policy_digest",
            ],
            name="fk_tombstone_authorizations_policy_approval",
        ),
        sa.ForeignKeyConstraint(
            [
                "workspace_id",
                "environment_id",
                "issuer_authority_id",
                "issuer_service_principal_id",
                "issuer_authority_generation",
            ],
            [
                f"{SCHEMA}.tombstone_authorization_issuers.workspace_id",
                f"{SCHEMA}.tombstone_authorization_issuers.environment_id",
                f"{SCHEMA}.tombstone_authorization_issuers.issuer_authority_id",
                f"{SCHEMA}.tombstone_authorization_issuers.service_principal_id",
                f"{SCHEMA}.tombstone_authorization_issuers.authority_generation",
            ],
            name="fk_tombstone_authorizations_issuer",
        ),
        sa.ForeignKeyConstraint(
            [
                "workspace_id",
                "environment_id",
                "expected_current_acceptance_id",
                "source_object_id",
                "expected_current_revision_id",
            ],
            [
                f"{SCHEMA}.canonical_acceptances.workspace_id",
                f"{SCHEMA}.canonical_acceptances.environment_id",
                f"{SCHEMA}.canonical_acceptances.canonical_acceptance_id",
                f"{SCHEMA}.canonical_acceptances.source_object_id",
                f"{SCHEMA}.canonical_acceptances.source_revision_id",
            ],
            name="fk_tombstone_authorizations_expected_acceptance",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "audit_event_id"],
            [
                f"{SCHEMA}.audit_events.workspace_id",
                f"{SCHEMA}.audit_events.environment_id",
                f"{SCHEMA}.audit_events.audit_event_id",
            ],
            name="fk_tombstone_authorizations_audit",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint(
            "expected_current_cas_version >= 1", name="ck_tombstone_authorizations_cas"
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "tombstone_authorization_decisions",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("authorization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("disposition", sa.String(16), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("audit_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "environment_id",
            "decision_id",
            name="pk_tombstone_authorization_decisions",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "authorization_id",
            "decision_id",
            "generation",
            name="uq_tombstone_authorization_decisions_generation",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "authorization_id"],
            [
                f"{SCHEMA}.tombstone_authorizations.workspace_id",
                f"{SCHEMA}.tombstone_authorizations.environment_id",
                f"{SCHEMA}.tombstone_authorizations.authorization_id",
            ],
            name="fk_tombstone_authorization_decisions_authorization",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "audit_event_id"],
            [
                f"{SCHEMA}.audit_events.workspace_id",
                f"{SCHEMA}.audit_events.environment_id",
                f"{SCHEMA}.audit_events.audit_event_id",
            ],
            name="fk_tombstone_authorization_decisions_audit",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint(
            "disposition IN ('issued', 'revoked', 'superseded')",
            name="ck_tombstone_authorization_decisions_disposition",
        ),
        sa.CheckConstraint(
            "generation >= 1", name="ck_tombstone_authorization_decisions_generation"
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "current_tombstone_authorization_decisions",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("authorization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("current_decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "environment_id",
            "authorization_id",
            name="pk_current_tombstone_authorization_decisions",
        ),
        sa.ForeignKeyConstraint(
            [
                "workspace_id",
                "environment_id",
                "authorization_id",
                "current_decision_id",
                "generation",
            ],
            [
                f"{SCHEMA}.tombstone_authorization_decisions.workspace_id",
                f"{SCHEMA}.tombstone_authorization_decisions.environment_id",
                f"{SCHEMA}.tombstone_authorization_decisions.authorization_id",
                f"{SCHEMA}.tombstone_authorization_decisions.decision_id",
                f"{SCHEMA}.tombstone_authorization_decisions.generation",
            ],
            name="fk_current_tombstone_authorization_decision",
        ),
        sa.CheckConstraint(
            "generation >= 1",
            name="ck_current_tombstone_authorization_decision_generation",
        ),
        schema=SCHEMA,
    )

    immutable = (
        "qualified_deletion_evidence",
        "source_deletion_policy_approvals",
        "tombstone_authorization_issuers",
        "tombstone_authorizations",
        "tombstone_authorization_decisions",
    )
    current = (
        "current_source_deletion_policy_approvals",
        "current_tombstone_authorization_issuers",
        "current_tombstone_authorization_decisions",
    )
    for name in (*immutable, *current):
        _tenant_table(name, mutable=name in current)
        op.execute(
            f"COMMENT ON TABLE {SCHEMA}.{name} IS "
            "'classification=tenant_operational_state; provider_content=forbidden; "
            "qualified deletion authority uses immutable references only'"
        )
    for name in immutable:
        _immutable(name)
    _current_guard("current_source_deletion_policy_approvals", "current_approval_id")
    _current_guard(
        "current_tombstone_authorization_issuers", "current_issuer_authority_id"
    )
    _current_guard("current_tombstone_authorization_decisions", "current_decision_id")


def downgrade() -> None:
    for name in (
        "current_tombstone_authorization_decisions",
        "current_tombstone_authorization_issuers",
        "current_source_deletion_policy_approvals",
    ):
        op.drop_table(name, schema=SCHEMA)
        op.execute(f"DROP FUNCTION {SCHEMA}.guard_{name}_step()")
    for name in (
        "tombstone_authorization_decisions",
        "tombstone_authorizations",
        "tombstone_authorization_issuers",
        "source_deletion_policy_approvals",
        "qualified_deletion_evidence",
    ):
        op.drop_table(name, schema=SCHEMA)
        op.execute(f"DROP FUNCTION {SCHEMA}.reject_{name}_mutation()")
    op.execute(
        f"DROP FUNCTION {SCHEMA}.resolve_current_deletion_qualification_authority"
        "(uuid,uuid,uuid,text,text)"
    )
    op.drop_constraint(
        "uq_qualification_authority_tenant_principal",
        "qualification_authority_bindings",
        schema=SCHEMA,
        type_="unique",
    )
