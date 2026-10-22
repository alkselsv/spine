"""Add bounded qualification authority with database-time hard expiry."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261119_56"
down_revision = "20261118_55"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, attribute))


def _runtime_role() -> str:
    return _role("runtime_role")


def upgrade() -> None:
    runtime_role = _runtime_role()
    migration_role = _role("migration_role")
    op.create_table(
        "qualification_authority_bindings",
        sa.Column("binding_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("qualification_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("principal_kind", sa.String(length=16), nullable=False),
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.String(length=64), nullable=True),
        sa.Column("operation", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("change_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recorded_by", sa.Text(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint(
            "binding_id", "version", name="pk_qualification_authority_bindings"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_qualification_binding_environment",
        ),
        sa.UniqueConstraint(
            "change_id", name="uq_qualification_binding_change_id"
        ),
        sa.CheckConstraint(
            "version > 0", name=op.f("ck_qualification_binding_version_positive")
        ),
        sa.CheckConstraint(
            "principal_kind IN ('human', 'service')",
            name=op.f("ck_qualification_binding_principal_kind"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled', 'revoked')",
            name=op.f("ck_qualification_binding_status"),
        ),
        sa.CheckConstraint(
            "(principal_kind = 'human' AND purpose IS NULL AND operation IS NULL) "
            "OR (principal_kind = 'service' AND purpose IS NOT NULL AND operation IS NOT NULL)",
            name=op.f("ck_qualification_binding_scope_shape"),
        ),
        sa.CheckConstraint(
            "char_length(recorded_by) BETWEEN 1 AND 255",
            name=op.f("ck_qualification_binding_recorded_by_length"),
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_qualification_authority_current_principal",
        "qualification_authority_bindings",
        [
            "workspace_id",
            "environment_id",
            "principal_kind",
            "principal_id",
            "purpose",
            "operation",
            sa.text("version DESC"),
        ],
        schema=SCHEMA,
    )
    op.execute(
        f"COMMENT ON TABLE {SCHEMA}.qualification_authority_bindings IS "
        "'classification=tenant_authorization; runtime_table_access=none; "
        "scope=qualification_only'"
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.qualification_authority_bindings ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.qualification_authority_bindings FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        f"CREATE POLICY pol_qualification_authority_bindings_tenant_isolation "
        f"ON {SCHEMA}.qualification_authority_bindings FOR ALL TO {runtime_role} "
        "USING (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid) "
        "WITH CHECK (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid)"
    )
    op.execute(
        f"CREATE POLICY pol_qualification_authority_bindings_migration_maintenance "
        f"ON {SCHEMA}.qualification_authority_bindings FOR ALL TO {migration_role} "
        "USING (true) WITH CHECK (true)"
    )

    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.validate_qualification_authority_binding()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE
            first_record {SCHEMA}.qualification_authority_bindings%ROWTYPE;
            latest_status text;
        BEGIN
            PERFORM pg_catalog.pg_advisory_xact_lock(
                pg_catalog.hashtextextended(
                    NEW.workspace_id::text || ':' || NEW.environment_id::text || ':' ||
                    NEW.principal_kind || ':' || NEW.principal_id::text || ':' ||
                    COALESCE(NEW.purpose, '') || ':' || COALESCE(NEW.operation, ''),
                    0
                )
            );
            SELECT * INTO first_record
              FROM {SCHEMA}.qualification_authority_bindings
             WHERE binding_id = NEW.binding_id
             ORDER BY version
             LIMIT 1;
            IF FOUND THEN
                SELECT status INTO latest_status
                  FROM {SCHEMA}.qualification_authority_bindings
                 WHERE binding_id = NEW.binding_id
                 ORDER BY version DESC
                 LIMIT 1;
                IF NEW.version != (
                    SELECT max(version) + 1
                    FROM {SCHEMA}.qualification_authority_bindings
                    WHERE binding_id = NEW.binding_id
                ) OR NEW.qualification_id != first_record.qualification_id
                   OR NEW.workspace_id != first_record.workspace_id
                   OR NEW.environment_id != first_record.environment_id
                   OR NEW.principal_kind != first_record.principal_kind
                   OR NEW.principal_id != first_record.principal_id
                   OR NEW.purpose IS DISTINCT FROM first_record.purpose
                   OR NEW.operation IS DISTINCT FROM first_record.operation
                   OR NEW.expires_at != first_record.expires_at THEN
                    RAISE EXCEPTION 'Qualification binding scope and deadline are immutable.'
                        USING ERRCODE = '23514',
                              CONSTRAINT = 'ck_qualification_authority_binding_immutable';
                END IF;
                IF latest_status = 'revoked' THEN
                    RAISE EXCEPTION 'Revoked qualification authority cannot be reactivated.'
                        USING ERRCODE = '23514',
                              CONSTRAINT = 'ck_qualification_authority_revocation_terminal';
                END IF;
            ELSIF NEW.version != 1 THEN
                RAISE EXCEPTION 'Qualification binding history must start at version 1.'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_qualification_authority_binding_initial_version';
            END IF;

            IF EXISTS (
                SELECT 1 FROM {SCHEMA}.qualification_authority_bindings AS existing
                 WHERE existing.workspace_id = NEW.workspace_id
                   AND existing.environment_id = NEW.environment_id
                   AND existing.principal_kind = NEW.principal_kind
                   AND existing.principal_id = NEW.principal_id
                   AND existing.purpose IS NOT DISTINCT FROM NEW.purpose
                   AND existing.operation IS NOT DISTINCT FROM NEW.operation
                   AND existing.binding_id != NEW.binding_id
            ) THEN
                RAISE EXCEPTION 'Qualification principal scope already has a binding.'
                    USING ERRCODE = '23505',
                          CONSTRAINT = 'uq_qualification_authority_principal_scope';
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(
        f"CREATE TRIGGER trg_qualification_authority_validate "
        f"BEFORE INSERT ON {SCHEMA}.qualification_authority_bindings FOR EACH ROW "
        f"EXECUTE FUNCTION {SCHEMA}.validate_qualification_authority_binding()"
    )
    op.execute(
        f"CREATE TRIGGER trg_qualification_authority_immutable "
        f"BEFORE UPDATE OR DELETE ON {SCHEMA}.qualification_authority_bindings FOR EACH ROW "
        f"EXECUTE FUNCTION {SCHEMA}.reject_authorization_history_mutation()"
    )
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.advance_qualification_authorization_generation()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE
            changed_rows integer;
        BEGIN
            UPDATE {SCHEMA}.authorization_generations
               SET generation = generation + 1,
                   updated_at = transaction_timestamp()
             WHERE workspace_id = NEW.workspace_id
               AND environment_id = NEW.environment_id;
            GET DIAGNOSTICS changed_rows = ROW_COUNT;
            IF changed_rows != 1 THEN
                RAISE EXCEPTION 'Qualification authority generation is unavailable.'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_qualification_authority_generation_required';
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(
        f"CREATE TRIGGER trg_qualification_authority_advance_generation "
        f"AFTER INSERT ON {SCHEMA}.qualification_authority_bindings FOR EACH ROW "
        f"EXECUTE FUNCTION {SCHEMA}.advance_qualification_authorization_generation()"
    )

    _create_resolvers(runtime_role)

    for function in (
        "validate_qualification_authority_binding()",
        "advance_qualification_authorization_generation()",
        "resolve_current_qualification_human_authority(uuid,uuid,uuid)",
        "resolve_current_human_authority(uuid,uuid,uuid)",
        "resolve_current_service_authority(uuid,uuid,uuid,text,text)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.{function} FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {SCHEMA}.resolve_current_qualification_human_authority"
        f"(uuid,uuid,uuid) TO {runtime_role}"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {SCHEMA}.resolve_current_human_authority"
        f"(uuid,uuid,uuid) TO {runtime_role}"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {SCHEMA}.resolve_current_service_authority"
        f"(uuid,uuid,uuid,text,text) TO {runtime_role}"
    )
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION {SCHEMA}.resolve_current_authorization_generation"
        f"(uuid,uuid) FROM {runtime_role}"
    )


def _create_resolvers(runtime_role: str) -> None:
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.resolve_current_qualification_human_authority(
            requested_principal_id uuid,
            requested_workspace_id uuid,
            requested_environment_id uuid
        ) RETURNS bigint
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
            WITH current_qualification AS (
                SELECT status, expires_at
                  FROM {SCHEMA}.qualification_authority_bindings
                 WHERE workspace_id = requested_workspace_id
                   AND environment_id = requested_environment_id
                   AND principal_kind = 'human'
                   AND principal_id = requested_principal_id
                 ORDER BY version DESC LIMIT 1
            )
            SELECT generation.generation
              FROM {SCHEMA}.authorization_generations AS generation
             WHERE generation.workspace_id = requested_workspace_id
               AND generation.environment_id = requested_environment_id
               AND (
                    NOT EXISTS (SELECT 1 FROM current_qualification)
                    OR EXISTS (
                        SELECT 1 FROM current_qualification
                         WHERE status = 'active'
                           AND transaction_timestamp() < expires_at
                    )
               )
               AND current_setting('spine.workspace_id', true) = requested_workspace_id::text
               AND current_setting('spine.environment_id', true) = requested_environment_id::text;
        $function$
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.resolve_current_human_authority(
            requested_principal_id uuid,
            requested_workspace_id uuid,
            requested_environment_id uuid
        ) RETURNS bigint
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
            WITH current_identity AS (
                SELECT status FROM {SCHEMA}.canonical_human_identity_states
                 WHERE identity_id = requested_principal_id
                 ORDER BY version DESC LIMIT 1
            ),
            current_workspace AS (
                SELECT status FROM {SCHEMA}.workspace_memberships
                 WHERE workspace_id = requested_workspace_id
                   AND canonical_human_identity_id = requested_principal_id
                 ORDER BY version DESC LIMIT 1
            ),
            current_environment AS (
                SELECT status FROM {SCHEMA}.environment_memberships
                 WHERE workspace_id = requested_workspace_id
                   AND environment_id = requested_environment_id
                   AND canonical_human_identity_id = requested_principal_id
                 ORDER BY version DESC LIMIT 1
            ),
            current_role_binding AS (
                SELECT status FROM {SCHEMA}.environment_role_bindings
                 WHERE workspace_id = requested_workspace_id
                   AND environment_id = requested_environment_id
                   AND canonical_human_identity_id = requested_principal_id
                   AND role = 'administrator'
                 ORDER BY version DESC LIMIT 1
            ),
            current_qualification AS (
                SELECT status, expires_at
                  FROM {SCHEMA}.qualification_authority_bindings
                 WHERE workspace_id = requested_workspace_id
                   AND environment_id = requested_environment_id
                   AND principal_kind = 'human'
                   AND principal_id = requested_principal_id
                 ORDER BY version DESC LIMIT 1
            )
            SELECT generation.generation
              FROM current_identity AS identity
              CROSS JOIN current_workspace AS workspace_membership
              CROSS JOIN current_environment AS environment_membership
              CROSS JOIN current_role_binding AS role_binding
              JOIN {SCHEMA}.authorization_generations AS generation
                ON generation.workspace_id = requested_workspace_id
               AND generation.environment_id = requested_environment_id
              JOIN {SCHEMA}.environments AS environment
                ON environment.workspace_id = requested_workspace_id
               AND environment.id = requested_environment_id
             WHERE identity.status = 'active'
               AND workspace_membership.status = 'active'
               AND environment_membership.status = 'active'
               AND role_binding.status = 'active'
               AND (
                    NOT EXISTS (SELECT 1 FROM current_qualification)
                    OR EXISTS (
                        SELECT 1 FROM current_qualification
                         WHERE status = 'active'
                           AND transaction_timestamp() < expires_at
                    )
               )
               AND current_setting('spine.workspace_id', true) = requested_workspace_id::text
               AND current_setting('spine.environment_id', true) = requested_environment_id::text;
        $function$
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.resolve_current_service_authority(
            requested_principal_id uuid,
            requested_workspace_id uuid,
            requested_environment_id uuid,
            requested_purpose text,
            requested_operation text
        ) RETURNS bigint
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
            WITH any_qualification AS (
                SELECT 1
                  FROM {SCHEMA}.qualification_authority_bindings
                 WHERE workspace_id = requested_workspace_id
                   AND environment_id = requested_environment_id
                   AND principal_kind = 'service'
                   AND principal_id = requested_principal_id
                 LIMIT 1
            ), current_qualification AS (
                SELECT status, purpose, operation, expires_at
                  FROM {SCHEMA}.qualification_authority_bindings
                 WHERE workspace_id = requested_workspace_id
                   AND environment_id = requested_environment_id
                   AND principal_kind = 'service'
                   AND principal_id = requested_principal_id
                   AND purpose = requested_purpose
                   AND operation = requested_operation
                 ORDER BY version DESC LIMIT 1
            )
            SELECT generation.generation
              FROM {SCHEMA}.authorization_generations AS generation
              JOIN {SCHEMA}.environments AS environment
                ON environment.workspace_id = generation.workspace_id
               AND environment.id = generation.environment_id
             WHERE generation.workspace_id = requested_workspace_id
               AND generation.environment_id = requested_environment_id
               AND (
                    NOT EXISTS (SELECT 1 FROM any_qualification)
                    OR EXISTS (
                        SELECT 1 FROM current_qualification
                         WHERE status = 'active'
                           AND purpose = requested_purpose
                           AND operation = requested_operation
                           AND transaction_timestamp() < expires_at
                    )
               )
               AND current_setting('spine.workspace_id', true) = requested_workspace_id::text
               AND current_setting('spine.environment_id', true) = requested_environment_id::text;
        $function$
        """
    )

    op.execute(
        f"REVOKE EXECUTE ON FUNCTION {SCHEMA}.resolve_current_authorization_snapshot"
        f"(text,text,uuid,uuid) FROM {runtime_role}"
    )
    op.execute(
        f"DROP FUNCTION {SCHEMA}.resolve_current_authorization_snapshot(text,text,uuid,uuid)"
    )
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.resolve_current_authorization_snapshot(
            requested_issuer text,
            requested_subject text,
            requested_workspace_id uuid,
            requested_environment_id uuid
        ) RETURNS TABLE (
            acting_subject_id uuid,
            authorization_generation bigint,
            effective_team_ids uuid[]
        )
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
            WITH current_alias AS (
                SELECT canonical_human_identity_id, status
                  FROM {SCHEMA}.authentication_alias_bindings
                 WHERE issuer = requested_issuer AND subject = requested_subject
                 ORDER BY version DESC LIMIT 1
            ),
            current_environment AS (
                SELECT membership.effective_team_ids
                  FROM {SCHEMA}.environment_memberships AS membership
                  JOIN current_alias AS alias
                    ON alias.canonical_human_identity_id = membership.canonical_human_identity_id
                 WHERE membership.workspace_id = requested_workspace_id
                   AND membership.environment_id = requested_environment_id
                 ORDER BY membership.version DESC LIMIT 1
            )
            SELECT alias.canonical_human_identity_id,
                   {SCHEMA}.resolve_current_human_authority(
                       alias.canonical_human_identity_id,
                       requested_workspace_id,
                       requested_environment_id
                   ),
                   environment_membership.effective_team_ids
              FROM current_alias AS alias
              CROSS JOIN current_environment AS environment_membership
             WHERE alias.status = 'active'
               AND {SCHEMA}.resolve_current_human_authority(
                       alias.canonical_human_identity_id,
                       requested_workspace_id,
                       requested_environment_id
                   ) IS NOT NULL;
        $function$
        """
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.resolve_current_authorization_snapshot"
        "(text,text,uuid,uuid) FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {SCHEMA}.resolve_current_authorization_snapshot"
        f"(text,text,uuid,uuid) TO {runtime_role}"
    )


def downgrade() -> None:
    runtime_role = _runtime_role()
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION {SCHEMA}.resolve_current_authorization_snapshot"
        f"(text,text,uuid,uuid) FROM {runtime_role}"
    )
    op.execute(
        f"DROP FUNCTION {SCHEMA}.resolve_current_authorization_snapshot(text,text,uuid,uuid)"
    )
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION {SCHEMA}.resolve_current_service_authority"
        f"(uuid,uuid,uuid,text,text) FROM {runtime_role}"
    )
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION {SCHEMA}.resolve_current_human_authority"
        f"(uuid,uuid,uuid) FROM {runtime_role}"
    )
    op.execute(
        f"DROP FUNCTION {SCHEMA}.resolve_current_service_authority(uuid,uuid,uuid,text,text)"
    )
    op.execute(
        f"DROP FUNCTION {SCHEMA}.resolve_current_human_authority(uuid,uuid,uuid)"
    )
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION {SCHEMA}.resolve_current_qualification_human_authority"
        f"(uuid,uuid,uuid) FROM {runtime_role}"
    )
    op.execute(
        f"DROP FUNCTION {SCHEMA}.resolve_current_qualification_human_authority"
        "(uuid,uuid,uuid)"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {SCHEMA}.resolve_current_authorization_generation"
        f"(uuid,uuid) TO {runtime_role}"
    )
    _restore_snapshot_resolver(runtime_role)
    op.execute(
        f"DROP TRIGGER trg_qualification_authority_advance_generation "
        f"ON {SCHEMA}.qualification_authority_bindings"
    )
    op.execute(
        f"DROP FUNCTION {SCHEMA}.advance_qualification_authorization_generation()"
    )
    op.execute(
        f"DROP TRIGGER trg_qualification_authority_immutable "
        f"ON {SCHEMA}.qualification_authority_bindings"
    )
    op.execute(
        f"DROP TRIGGER trg_qualification_authority_validate "
        f"ON {SCHEMA}.qualification_authority_bindings"
    )
    op.execute(f"DROP FUNCTION {SCHEMA}.validate_qualification_authority_binding()")
    op.drop_table("qualification_authority_bindings", schema=SCHEMA)


def _restore_snapshot_resolver(runtime_role: str) -> None:
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.resolve_current_authorization_snapshot(
            requested_issuer text, requested_subject text,
            requested_workspace_id uuid, requested_environment_id uuid
        ) RETURNS TABLE (
            acting_subject_id uuid,
            authorization_generation bigint,
            effective_team_ids uuid[]
        )
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
            WITH current_alias AS (
                SELECT canonical_human_identity_id, status
                  FROM {SCHEMA}.authentication_alias_bindings
                 WHERE issuer = requested_issuer AND subject = requested_subject
                 ORDER BY version DESC LIMIT 1
            ), current_identity AS (
                SELECT state.status FROM {SCHEMA}.canonical_human_identity_states AS state
                  JOIN current_alias AS alias ON alias.canonical_human_identity_id = state.identity_id
                 ORDER BY state.version DESC LIMIT 1
            ), current_workspace AS (
                SELECT membership.status FROM {SCHEMA}.workspace_memberships AS membership
                  JOIN current_alias AS alias ON alias.canonical_human_identity_id = membership.canonical_human_identity_id
                 WHERE membership.workspace_id = requested_workspace_id
                 ORDER BY membership.version DESC LIMIT 1
            ), current_environment AS (
                SELECT membership.status, membership.effective_team_ids
                  FROM {SCHEMA}.environment_memberships AS membership
                  JOIN current_alias AS alias ON alias.canonical_human_identity_id = membership.canonical_human_identity_id
                 WHERE membership.workspace_id = requested_workspace_id
                   AND membership.environment_id = requested_environment_id
                 ORDER BY membership.version DESC LIMIT 1
            ), current_role_binding AS (
                SELECT binding.status FROM {SCHEMA}.environment_role_bindings AS binding
                  JOIN current_alias AS alias ON alias.canonical_human_identity_id = binding.canonical_human_identity_id
                 WHERE binding.workspace_id = requested_workspace_id
                   AND binding.environment_id = requested_environment_id
                   AND binding.role = 'administrator'
                 ORDER BY binding.version DESC LIMIT 1
            )
            SELECT alias.canonical_human_identity_id, generation.generation,
                   environment_membership.effective_team_ids
              FROM current_alias AS alias CROSS JOIN current_identity AS identity
              CROSS JOIN current_workspace AS workspace_membership
              CROSS JOIN current_environment AS environment_membership
              CROSS JOIN current_role_binding AS role_binding
              JOIN {SCHEMA}.authorization_generations AS generation
                ON generation.workspace_id = requested_workspace_id
               AND generation.environment_id = requested_environment_id
             WHERE alias.status = 'active' AND identity.status = 'active'
               AND workspace_membership.status = 'active'
               AND environment_membership.status = 'active'
               AND role_binding.status = 'active';
        $function$
        """
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.resolve_current_authorization_snapshot"
        "(text,text,uuid,uuid) FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {SCHEMA}.resolve_current_authorization_snapshot"
        f"(text,text,uuid,uuid) TO {runtime_role}"
    )
