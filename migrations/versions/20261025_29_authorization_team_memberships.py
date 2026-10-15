"""Add team membership to the current authorization snapshot."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261025_29"
down_revision = "20261024_28"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def upgrade() -> None:
    op.add_column(
        "environment_memberships",
        sa.Column(
            "effective_team_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
        schema=SCHEMA,
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
        )
        RETURNS TABLE (
            acting_subject_id uuid,
            authorization_generation bigint,
            effective_team_ids uuid[]
        )
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
            WITH current_alias AS (
                SELECT binding.canonical_human_identity_id, binding.status
                  FROM {SCHEMA}.authentication_alias_bindings AS binding
                 WHERE binding.issuer = requested_issuer
                   AND binding.subject = requested_subject
                 ORDER BY binding.version DESC LIMIT 1
            ),
            current_identity AS (
                SELECT state.status
                  FROM {SCHEMA}.canonical_human_identity_states AS state
                  JOIN current_alias AS alias
                    ON alias.canonical_human_identity_id = state.identity_id
                 ORDER BY state.version DESC LIMIT 1
            ),
            current_workspace_membership AS (
                SELECT membership.status
                  FROM {SCHEMA}.workspace_memberships AS membership
                  JOIN current_alias AS alias
                    ON alias.canonical_human_identity_id = membership.canonical_human_identity_id
                 WHERE membership.workspace_id = requested_workspace_id
                 ORDER BY membership.version DESC LIMIT 1
            ),
            current_environment_membership AS (
                SELECT membership.status, membership.effective_team_ids
                  FROM {SCHEMA}.environment_memberships AS membership
                  JOIN current_alias AS alias
                    ON alias.canonical_human_identity_id = membership.canonical_human_identity_id
                 WHERE membership.workspace_id = requested_workspace_id
                   AND membership.environment_id = requested_environment_id
                 ORDER BY membership.version DESC LIMIT 1
            ),
            current_role_binding AS (
                SELECT binding.status, binding.role
                  FROM {SCHEMA}.environment_role_bindings AS binding
                  JOIN current_alias AS alias
                    ON alias.canonical_human_identity_id = binding.canonical_human_identity_id
                 WHERE binding.workspace_id = requested_workspace_id
                   AND binding.environment_id = requested_environment_id
                   AND binding.role = 'administrator'
                 ORDER BY binding.version DESC LIMIT 1
            )
            SELECT alias.canonical_human_identity_id,
                   generation_record.generation,
                   environment_membership.effective_team_ids
              FROM current_alias AS alias
              CROSS JOIN current_identity AS identity
              CROSS JOIN current_workspace_membership AS workspace_membership
              CROSS JOIN current_environment_membership AS environment_membership
              CROSS JOIN current_role_binding AS role_binding
              JOIN {SCHEMA}.environments AS environment_record
                ON environment_record.id = requested_environment_id
               AND environment_record.workspace_id = requested_workspace_id
              JOIN {SCHEMA}.authorization_generations AS generation_record
                ON generation_record.workspace_id = requested_workspace_id
               AND generation_record.environment_id = requested_environment_id
             WHERE alias.status = 'active'
               AND identity.status = 'active'
               AND workspace_membership.status = 'active'
               AND environment_membership.status = 'active'
               AND role_binding.status = 'active'
               AND role_binding.role = 'administrator';
        $function$
        """
    )
    runtime_role = _quoted_role("runtime_role")
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.resolve_current_authorization_snapshot(text,text,uuid,uuid) FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {SCHEMA}.resolve_current_authorization_snapshot(text,text,uuid,uuid) TO {runtime_role}"
    )


def downgrade() -> None:
    runtime_role = _quoted_role("runtime_role")
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION {SCHEMA}.resolve_current_authorization_snapshot(text,text,uuid,uuid) FROM {runtime_role}"
    )
    op.execute(
        f"DROP FUNCTION {SCHEMA}.resolve_current_authorization_snapshot(text,text,uuid,uuid)"
    )
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.resolve_current_authorization_snapshot(
            requested_issuer text, requested_subject text,
            requested_workspace_id uuid, requested_environment_id uuid
        )
        RETURNS TABLE (acting_subject_id uuid, authorization_generation bigint)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
            WITH current_alias AS (
                SELECT binding.canonical_human_identity_id, binding.status
                  FROM {SCHEMA}.authentication_alias_bindings AS binding
                 WHERE binding.issuer = requested_issuer AND binding.subject = requested_subject
                 ORDER BY binding.version DESC LIMIT 1
            ),
            current_identity AS (
                SELECT state.status FROM {SCHEMA}.canonical_human_identity_states AS state
                  JOIN current_alias AS alias ON alias.canonical_human_identity_id = state.identity_id
                 ORDER BY state.version DESC LIMIT 1
            ),
            current_workspace_membership AS (
                SELECT membership.status FROM {SCHEMA}.workspace_memberships AS membership
                  JOIN current_alias AS alias ON alias.canonical_human_identity_id = membership.canonical_human_identity_id
                 WHERE membership.workspace_id = requested_workspace_id
                 ORDER BY membership.version DESC LIMIT 1
            ),
            current_environment_membership AS (
                SELECT membership.status FROM {SCHEMA}.environment_memberships AS membership
                  JOIN current_alias AS alias ON alias.canonical_human_identity_id = membership.canonical_human_identity_id
                 WHERE membership.workspace_id = requested_workspace_id
                   AND membership.environment_id = requested_environment_id
                 ORDER BY membership.version DESC LIMIT 1
            ),
            current_role_binding AS (
                SELECT binding.status, binding.role FROM {SCHEMA}.environment_role_bindings AS binding
                  JOIN current_alias AS alias ON alias.canonical_human_identity_id = binding.canonical_human_identity_id
                 WHERE binding.workspace_id = requested_workspace_id
                   AND binding.environment_id = requested_environment_id
                   AND binding.role = 'administrator'
                 ORDER BY binding.version DESC LIMIT 1
            )
            SELECT alias.canonical_human_identity_id, generation_record.generation
              FROM current_alias AS alias
              CROSS JOIN current_identity AS identity
              CROSS JOIN current_workspace_membership AS workspace_membership
              CROSS JOIN current_environment_membership AS environment_membership
              CROSS JOIN current_role_binding AS role_binding
              JOIN {SCHEMA}.environments AS environment_record
                ON environment_record.id = requested_environment_id
               AND environment_record.workspace_id = requested_workspace_id
              JOIN {SCHEMA}.authorization_generations AS generation_record
                ON generation_record.workspace_id = requested_workspace_id
               AND generation_record.environment_id = requested_environment_id
             WHERE alias.status = 'active' AND identity.status = 'active'
               AND workspace_membership.status = 'active'
               AND environment_membership.status = 'active'
               AND role_binding.status = 'active'
               AND role_binding.role = 'administrator';
        $function$
        """
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {SCHEMA}.resolve_current_authorization_snapshot(text,text,uuid,uuid) TO {runtime_role}"
    )
    op.drop_column("environment_memberships", "effective_team_ids", schema=SCHEMA)


def _quoted_role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    role = getattr(settings, attribute)
    return op.get_bind().dialect.identifier_preparer.quote(role)
