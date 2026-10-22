"""Bind isolated qualification targets to external secret and database-role references."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261120_57"
down_revision = "20261119_56"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, attribute))


def upgrade() -> None:
    runtime_role = _role("runtime_role")
    migration_role = _role("migration_role")
    op.create_table(
        "qualification_target_bindings",
        sa.Column("binding_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("binding_series_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("binding_version", sa.BigInteger(), nullable=False),
        sa.Column("predecessor_binding_id", postgresql.UUID(as_uuid=True)),
        sa.Column("authority_binding_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("authority_binding_version", sa.BigInteger(), nullable=False),
        sa.Column("qualification_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target", sa.String(length=16), nullable=False),
        sa.Column("target_reference", sa.String(length=255), nullable=False),
        sa.Column("target_scope_reference", sa.String(length=255), nullable=False),
        sa.Column("principal_kind", sa.String(length=16), nullable=False),
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.String(length=64), nullable=True),
        sa.Column("operation", sa.String(length=64), nullable=True),
        sa.Column("alias_reference", sa.String(length=255), nullable=False),
        sa.Column("secret_provider_reference", sa.String(length=255), nullable=False),
        sa.Column("secret_reference", sa.String(length=255), nullable=False),
        sa.Column("database_role_reference", sa.String(length=255), nullable=False),
        sa.Column("target_role", sa.String(length=32), nullable=False),
        sa.Column("database_access", sa.String(length=16), nullable=False),
        sa.Column(
            "access_policy_references",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
        ),
        sa.Column("binding_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("binding_id", name="pk_qualification_target_bindings"),
        sa.UniqueConstraint(
            "binding_series_id",
            "binding_version",
            name="uq_qualification_target_series_version",
        ),
        sa.UniqueConstraint(
            "predecessor_binding_id",
            name="uq_qualification_target_predecessor",
        ),
        sa.ForeignKeyConstraint(
            ["predecessor_binding_id"],
            [f"{SCHEMA}.qualification_target_bindings.binding_id"],
            name="fk_qualification_target_predecessor",
        ),
        sa.ForeignKeyConstraint(
            ["authority_binding_id", "authority_binding_version"],
            [
                f"{SCHEMA}.qualification_authority_bindings.binding_id",
                f"{SCHEMA}.qualification_authority_bindings.version",
            ],
            name="fk_qualification_target_authority",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_qualification_target_environment",
        ),
        sa.UniqueConstraint("binding_digest", name="uq_qualification_target_digest"),
        sa.CheckConstraint(
            "authority_binding_version > 0",
            name=op.f("ck_qualification_target_authority_version"),
        ),
        sa.CheckConstraint(
            "(binding_version = 1 AND predecessor_binding_id IS NULL "
            "AND binding_id = binding_series_id) OR "
            "(binding_version > 1 AND predecessor_binding_id IS NOT NULL "
            "AND binding_id <> binding_series_id)",
            name=op.f("ck_qualification_target_history"),
        ),
        sa.CheckConstraint(
            "target IN ('source', 'restore')",
            name=op.f("ck_qualification_target_kind"),
        ),
        sa.CheckConstraint(
            "principal_kind IN ('human', 'service')",
            name=op.f("ck_qualification_target_principal_kind"),
        ),
        sa.CheckConstraint(
            "(principal_kind = 'human' AND purpose IS NULL AND operation IS NULL) OR "
            "(principal_kind = 'service' AND purpose IS NOT NULL AND operation IS NOT NULL)",
            name=op.f("ck_qualification_target_principal_scope"),
        ),
        sa.CheckConstraint(
            "(target = 'source' AND target_role = 'source_operator' "
            "AND database_access = 'write') OR "
            "(target = 'restore' AND target_role = 'restore_operator' "
            "AND database_access = 'write') OR "
            "(target = 'restore' AND target_role = 'verifier' "
            "AND database_access = 'read_only' AND principal_kind = 'service')",
            name=op.f("ck_qualification_target_role_scope"),
        ),
        sa.CheckConstraint(
            "cardinality(access_policy_references) > 0 "
            "AND array_position(access_policy_references, NULL) IS NULL",
            name=op.f("ck_qualification_target_policy_references"),
        ),
        sa.CheckConstraint(
            "binding_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_qualification_target_digest"),
        ),
        schema=SCHEMA,
    )
    op.execute(
        f"COMMENT ON TABLE {SCHEMA}.qualification_target_bindings IS "
        "'classification=tenant_authorization_reference; runtime_table_access=none; "
        "secret_values=forbidden; scope=qualification_only'"
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.qualification_target_bindings ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.qualification_target_bindings FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        f"CREATE POLICY pol_qualification_target_bindings_tenant_isolation "
        f"ON {SCHEMA}.qualification_target_bindings FOR ALL TO {runtime_role} "
        "USING (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid) "
        "WITH CHECK (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid)"
    )
    op.execute(
        f"CREATE POLICY pol_qualification_target_bindings_migration_maintenance "
        f"ON {SCHEMA}.qualification_target_bindings FOR ALL TO {migration_role} "
        "USING (true) WITH CHECK (true)"
    )
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.enforce_qualification_target_separation()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE
            predecessor {SCHEMA}.qualification_target_bindings%ROWTYPE;
        BEGIN
            IF cardinality(NEW.access_policy_references) <> (
                SELECT count(DISTINCT policy_id)
                  FROM unnest(NEW.access_policy_references) AS policy_id
            ) THEN
                RAISE EXCEPTION 'qualification target policy references are duplicated'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_qualification_target_policy_references';
            END IF;

            IF EXISTS (
                SELECT 1
                  FROM unnest(NEW.access_policy_references) AS required(policy_id)
                 WHERE NOT EXISTS (
                     SELECT 1
                       FROM {SCHEMA}.access_policy_versions AS policy
                      WHERE policy.access_policy_version_id = required.policy_id
                        AND policy.workspace_id = NEW.workspace_id
                        AND policy.environment_id = NEW.environment_id
                 )
            ) THEN
                RAISE EXCEPTION 'qualification target policy reference is not approved'
                    USING ERRCODE = '23503',
                          CONSTRAINT = 'fk_qualification_target_policy_reference';
            END IF;

            IF NEW.binding_version > 1 THEN
                SELECT * INTO predecessor
                  FROM {SCHEMA}.qualification_target_bindings
                 WHERE binding_id = NEW.predecessor_binding_id;
                IF NOT FOUND
                   OR predecessor.binding_series_id <> NEW.binding_series_id
                   OR predecessor.binding_version + 1 <> NEW.binding_version
                   OR predecessor.target <> NEW.target
                   OR predecessor.target_role <> NEW.target_role
                   OR predecessor.authority_binding_id <> NEW.authority_binding_id
                   OR predecessor.authority_binding_version >= NEW.authority_binding_version THEN
                    RAISE EXCEPTION 'qualification target successor is invalid'
                        USING ERRCODE = '23514',
                              CONSTRAINT = 'ck_qualification_target_successor';
                END IF;
            END IF;

            PERFORM pg_advisory_xact_lock(hashtextextended(reference_key, 0))
              FROM unnest(ARRAY[
                  'principal:' || NEW.principal_id::text,
                  'alias:' || NEW.alias_reference,
                  'secret:' || NEW.secret_provider_reference || ':' || NEW.secret_reference,
                  'database-role:' || NEW.database_role_reference,
                  'target:' || NEW.target_reference,
                  'scope:' || NEW.target_scope_reference
              ]) AS reference_key
             ORDER BY reference_key;

            IF EXISTS (
                SELECT 1
                  FROM {SCHEMA}.qualification_target_bindings AS existing
                 WHERE (
                     existing.target <> NEW.target
                     AND (
                         existing.principal_id = NEW.principal_id
                         OR existing.alias_reference = NEW.alias_reference
                         OR (
                             existing.secret_provider_reference = NEW.secret_provider_reference
                             AND existing.secret_reference = NEW.secret_reference
                         )
                         OR existing.database_role_reference = NEW.database_role_reference
                         OR existing.target_reference = NEW.target_reference
                         OR existing.target_scope_reference = NEW.target_scope_reference
                     )
                 ) OR (
                     existing.target = NEW.target
                     AND existing.target_role <> NEW.target_role
                     AND (
                         existing.principal_id = NEW.principal_id
                         OR existing.alias_reference = NEW.alias_reference
                         OR (
                             existing.secret_provider_reference = NEW.secret_provider_reference
                             AND existing.secret_reference = NEW.secret_reference
                         )
                         OR existing.database_role_reference = NEW.database_role_reference
                         OR existing.target_scope_reference = NEW.target_scope_reference
                     )
                 ) OR (
                     existing.target = NEW.target
                     AND existing.target_role = NEW.target_role
                     AND existing.binding_series_id <> NEW.binding_series_id
                     AND (
                         existing.principal_id = NEW.principal_id
                         OR existing.alias_reference = NEW.alias_reference
                         OR (
                             existing.secret_provider_reference = NEW.secret_provider_reference
                             AND existing.secret_reference = NEW.secret_reference
                         )
                         OR existing.database_role_reference = NEW.database_role_reference
                         OR existing.target_scope_reference = NEW.target_scope_reference
                     )
                 )
            ) THEN
                RAISE EXCEPTION 'qualification target separation conflict'
                    USING ERRCODE = '23505',
                          CONSTRAINT = 'uq_qualification_target_separation';
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.enforce_qualification_target_separation() "
        "FROM PUBLIC"
    )
    op.execute(
        f"CREATE TRIGGER trg_qualification_target_separation "
        f"BEFORE INSERT ON {SCHEMA}.qualification_target_bindings FOR EACH ROW "
        f"EXECUTE FUNCTION {SCHEMA}.enforce_qualification_target_separation()"
    )
    op.execute(
        f"CREATE TRIGGER trg_qualification_target_immutable "
        f"BEFORE UPDATE OR DELETE ON {SCHEMA}.qualification_target_bindings FOR EACH ROW "
        f"EXECUTE FUNCTION {SCHEMA}.reject_authorization_history_mutation()"
    )
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.resolve_current_qualification_target_binding(
            requested_binding_id uuid,
            requested_binding_digest text,
            requested_workspace_id uuid,
            requested_environment_id uuid
        ) RETURNS TABLE (
            binding_id uuid,
            binding_series_id uuid,
            binding_version bigint,
            predecessor_binding_id uuid,
            authority_binding_id uuid,
            authority_binding_version bigint,
            qualification_id uuid,
            workspace_id uuid,
            environment_id uuid,
            target text,
            target_reference text,
            target_scope_reference text,
            principal_kind text,
            principal_id uuid,
            purpose text,
            operation text,
            alias_reference text,
            secret_provider_reference text,
            secret_reference text,
            database_role_reference text,
            target_role text,
            database_access text,
            access_policy_references uuid[]
        )
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
            SELECT target_binding.binding_id,
                   target_binding.binding_series_id,
                   target_binding.binding_version,
                   target_binding.predecessor_binding_id,
                   target_binding.authority_binding_id,
                   target_binding.authority_binding_version,
                   target_binding.qualification_id,
                   target_binding.workspace_id,
                   target_binding.environment_id,
                   target_binding.target::text,
                   target_binding.target_reference::text,
                   target_binding.target_scope_reference::text,
                   target_binding.principal_kind::text,
                   target_binding.principal_id,
                   target_binding.purpose::text,
                   target_binding.operation::text,
                   target_binding.alias_reference::text,
                   target_binding.secret_provider_reference::text,
                   target_binding.secret_reference::text,
                   target_binding.database_role_reference::text,
                   target_binding.target_role::text,
                   target_binding.database_access::text,
                   target_binding.access_policy_references
              FROM {SCHEMA}.qualification_target_bindings AS target_binding
              JOIN {SCHEMA}.qualification_authority_bindings AS authority
                ON authority.binding_id = target_binding.authority_binding_id
               AND authority.version = target_binding.authority_binding_version
               AND authority.qualification_id = target_binding.qualification_id
               AND authority.workspace_id = target_binding.workspace_id
               AND authority.environment_id = target_binding.environment_id
               AND authority.principal_kind = target_binding.principal_kind
               AND authority.principal_id = target_binding.principal_id
               AND authority.purpose IS NOT DISTINCT FROM target_binding.purpose
               AND authority.operation IS NOT DISTINCT FROM target_binding.operation
              JOIN {SCHEMA}.authorization_generations AS generation
                ON generation.workspace_id = target_binding.workspace_id
               AND generation.environment_id = target_binding.environment_id
             WHERE target_binding.binding_id = requested_binding_id
               AND target_binding.binding_digest = requested_binding_digest
               AND target_binding.workspace_id = requested_workspace_id
               AND target_binding.environment_id = requested_environment_id
               AND authority.status = 'active'
               AND transaction_timestamp() < authority.expires_at
               AND authority.version = (
                    SELECT max(current_authority.version)
                      FROM {SCHEMA}.qualification_authority_bindings AS current_authority
                     WHERE current_authority.binding_id = authority.binding_id
               )
               AND NOT EXISTS (
                    SELECT 1
                      FROM unnest(target_binding.access_policy_references) AS required(policy_id)
                     WHERE NOT EXISTS (
                         SELECT 1
                           FROM {SCHEMA}.access_policy_versions AS policy
                          WHERE policy.access_policy_version_id = required.policy_id
                            AND policy.workspace_id = target_binding.workspace_id
                            AND policy.environment_id = target_binding.environment_id
                     )
               )
               AND current_setting('spine.workspace_id', true) = requested_workspace_id::text
               AND current_setting('spine.environment_id', true) = requested_environment_id::text;
        $function$
        """
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.resolve_current_qualification_target_binding"
        "(uuid,text,uuid,uuid) FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {SCHEMA}.resolve_current_qualification_target_binding"
        f"(uuid,text,uuid,uuid) TO {runtime_role}"
    )


def downgrade() -> None:
    runtime_role = _role("runtime_role")
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION {SCHEMA}.resolve_current_qualification_target_binding"
        f"(uuid,text,uuid,uuid) FROM {runtime_role}"
    )
    op.execute(
        f"DROP FUNCTION {SCHEMA}.resolve_current_qualification_target_binding"
        "(uuid,text,uuid,uuid)"
    )
    op.execute(
        f"DROP TRIGGER trg_qualification_target_immutable "
        f"ON {SCHEMA}.qualification_target_bindings"
    )
    op.execute(
        f"DROP TRIGGER trg_qualification_target_separation "
        f"ON {SCHEMA}.qualification_target_bindings"
    )
    op.execute(
        f"DROP FUNCTION {SCHEMA}.enforce_qualification_target_separation()"
    )
    op.drop_table("qualification_target_bindings", schema=SCHEMA)
