"""Persist canonical identity and R1 authorization directory history.

Revision ID: 20261010_07
Revises: 20261010_06
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261010_07"
down_revision = "20261010_06"
branch_labels = None
depends_on = None

SCHEMA = "spine"
PLATFORM_TABLES = (
    "canonical_human_identities",
    "canonical_human_identity_states",
    "authentication_alias_bindings",
)
TENANT_TABLES = (
    "workspace_memberships",
    "environment_memberships",
    "environment_role_bindings",
    "authorization_generations",
)
HISTORY_TABLES = (
    "canonical_human_identities",
    "canonical_human_identity_states",
    "authentication_alias_bindings",
    "workspace_memberships",
    "environment_memberships",
    "environment_role_bindings",
)
_UUID_PATTERN = (
    "^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    "[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)


def _quoted_role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    role = getattr(settings, attribute)
    return op.get_bind().dialect.identifier_preparer.quote(role)


def _setting_uuid(name: str) -> str:
    setting = f"pg_catalog.current_setting('spine.{name}', true)"
    return (
        f"CASE WHEN {setting} OPERATOR(pg_catalog.~) '{_UUID_PATTERN}' "
        f"THEN CAST({setting} AS pg_catalog.uuid) ELSE NULL END"
    )


def _history_columns() -> tuple[sa.Column[object], ...]:
    return (
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("change_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recorded_by", sa.Text(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )


def _history_checks(table: str) -> tuple[sa.CheckConstraint, ...]:
    return (
        sa.CheckConstraint(
            "version > 0",
            name=op.f(f"ck_{table}_version_positive"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled', 'revoked')",
            name=op.f(f"ck_{table}_status"),
        ),
        sa.CheckConstraint(
            "char_length(recorded_by) BETWEEN 1 AND 255",
            name=op.f(f"ck_{table}_recorded_by_length"),
        ),
    )


def upgrade() -> None:
    runtime_role = _quoted_role("runtime_role")
    migration_role = _quoted_role("migration_role")

    op.create_table(
        "canonical_human_identities",
        sa.Column("identity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_change_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("identity_id", name="pk_canonical_human_identities"),
        sa.UniqueConstraint(
            "created_change_id",
            name="uq_canonical_human_identities_created_change_id",
        ),
        sa.CheckConstraint(
            "char_length(created_by) BETWEEN 1 AND 255",
            name=op.f("ck_canonical_human_identities_created_by_length"),
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "canonical_human_identity_states",
        sa.Column("identity_id", postgresql.UUID(as_uuid=True), nullable=False),
        *_history_columns(),
        sa.PrimaryKeyConstraint(
            "identity_id", "version", name="pk_canonical_human_identity_states"
        ),
        sa.ForeignKeyConstraint(
            ["identity_id"],
            [f"{SCHEMA}.canonical_human_identities.identity_id"],
            name="fk_identity_states_identity",
        ),
        sa.UniqueConstraint(
            "change_id", name="uq_canonical_human_identity_states_change_id"
        ),
        *_history_checks("canonical_human_identity_states"),
        schema=SCHEMA,
    )
    op.create_table(
        "authentication_alias_bindings",
        sa.Column("binding_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column(
            "canonical_human_identity_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        *_history_columns(),
        sa.PrimaryKeyConstraint("binding_id", name="pk_authentication_alias_bindings"),
        sa.ForeignKeyConstraint(
            ["canonical_human_identity_id"],
            [f"{SCHEMA}.canonical_human_identities.identity_id"],
            name="fk_alias_bindings_identity",
        ),
        sa.UniqueConstraint(
            "issuer",
            "subject",
            "version",
            name="uq_authentication_alias_bindings_alias_version",
        ),
        sa.UniqueConstraint(
            "change_id", name="uq_authentication_alias_bindings_change_id"
        ),
        sa.CheckConstraint(
            "char_length(issuer) BETWEEN 1 AND 2048",
            name=op.f("ck_authentication_alias_bindings_issuer_length"),
        ),
        sa.CheckConstraint(
            "char_length(subject) BETWEEN 1 AND 512",
            name=op.f("ck_authentication_alias_bindings_subject_length"),
        ),
        *_history_checks("authentication_alias_bindings"),
        schema=SCHEMA,
    )
    op.create_table(
        "workspace_memberships",
        sa.Column("membership_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "canonical_human_identity_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        *_history_columns(),
        sa.PrimaryKeyConstraint("membership_id", name="pk_workspace_memberships"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            [f"{SCHEMA}.workspaces.id"],
            name="fk_workspace_memberships_workspace_id_workspaces",
        ),
        sa.ForeignKeyConstraint(
            ["canonical_human_identity_id"],
            [f"{SCHEMA}.canonical_human_identities.identity_id"],
            name="fk_workspace_memberships_identity",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "canonical_human_identity_id",
            "version",
            name="uq_workspace_memberships_scope_version",
        ),
        sa.UniqueConstraint("change_id", name="uq_workspace_memberships_change_id"),
        *_history_checks("workspace_memberships"),
        schema=SCHEMA,
    )
    op.create_table(
        "environment_memberships",
        sa.Column("membership_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "canonical_human_identity_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        *_history_columns(),
        sa.PrimaryKeyConstraint("membership_id", name="pk_environment_memberships"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_environment_memberships_scope_environments",
        ),
        sa.ForeignKeyConstraint(
            ["canonical_human_identity_id"],
            [f"{SCHEMA}.canonical_human_identities.identity_id"],
            name="fk_environment_memberships_identity",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "canonical_human_identity_id",
            "version",
            name="uq_environment_memberships_scope_version",
        ),
        sa.UniqueConstraint("change_id", name="uq_environment_memberships_change_id"),
        *_history_checks("environment_memberships"),
        schema=SCHEMA,
    )
    op.create_table(
        "environment_role_bindings",
        sa.Column("binding_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "canonical_human_identity_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=32), nullable=False),
        *_history_columns(),
        sa.PrimaryKeyConstraint("binding_id", name="pk_environment_role_bindings"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_environment_role_bindings_scope_environments",
        ),
        sa.ForeignKeyConstraint(
            ["canonical_human_identity_id"],
            [f"{SCHEMA}.canonical_human_identities.identity_id"],
            name="fk_environment_role_bindings_identity",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "canonical_human_identity_id",
            "role",
            "version",
            name="uq_environment_role_bindings_scope_role_version",
        ),
        sa.UniqueConstraint(
            "change_id", name="uq_environment_role_bindings_change_id"
        ),
        sa.CheckConstraint(
            "role = 'administrator'",
            name=op.f("ck_environment_role_bindings_role"),
        ),
        *_history_checks("environment_role_bindings"),
        schema=SCHEMA,
    )
    op.create_table(
        "authorization_generations",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint(
            "workspace_id", "environment_id", name="pk_authorization_generations"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_authorization_generations_scope_environments",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "generation > 0",
            name=op.f("ck_authorization_generations_generation_positive"),
        ),
        schema=SCHEMA,
    )

    op.create_index(
        "ix_workspace_memberships_current_lookup",
        "workspace_memberships",
        ["workspace_id", "canonical_human_identity_id", sa.text("version DESC")],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_environment_memberships_current_lookup",
        "environment_memberships",
        [
            "workspace_id",
            "environment_id",
            "canonical_human_identity_id",
            sa.text("version DESC"),
        ],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_environment_role_bindings_current_lookup",
        "environment_role_bindings",
        [
            "workspace_id",
            "environment_id",
            "canonical_human_identity_id",
            "role",
            sa.text("version DESC"),
        ],
        schema=SCHEMA,
    )

    for table in PLATFORM_TABLES:
        op.execute(
            f"COMMENT ON TABLE {SCHEMA}.{table} IS "
            "'classification=platform_identity; runtime_table_access=none'"
        )
    for table in TENANT_TABLES:
        op.execute(
            f"COMMENT ON TABLE {SCHEMA}.{table} IS "
            "'classification=tenant_authorization; runtime_table_access=none'"
        )

    op.execute(
        f"INSERT INTO {SCHEMA}.authorization_generations "
        "(workspace_id, environment_id, generation) "
        f"SELECT workspace_id, id, 1 FROM {SCHEMA}.environments"
    )

    workspace = _setting_uuid("workspace_id")
    environment = _setting_uuid("environment_id")
    for table in TENANT_TABLES:
        qualified = f'{SCHEMA}."{table}"'
        expression = f"workspace_id = ({workspace})"
        if table != "workspace_memberships":
            expression += f" AND environment_id = ({environment})"
        op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
        op.execute(
            f'CREATE POLICY "pol_{table}_tenant_isolation" ON {qualified} '
            f"FOR ALL TO {runtime_role} USING ({expression}) WITH CHECK ({expression})"
        )
        op.execute(
            f'CREATE POLICY "pol_{table}_migration_maintenance" ON {qualified} '
            f"FOR ALL TO {migration_role} USING (true) WITH CHECK (true)"
        )

    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.reject_authorization_history_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        BEGIN
            RAISE EXCEPTION 'Authorization history is immutable.'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'ck_authorization_history_immutable';
        END;
        $function$
        """
    )
    for table in HISTORY_TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable "
            f"BEFORE UPDATE OR DELETE ON {SCHEMA}.{table} FOR EACH ROW "
            f"EXECUTE FUNCTION {SCHEMA}.reject_authorization_history_mutation()"
        )

    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.initialize_authorization_generation()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        BEGIN
            INSERT INTO {SCHEMA}.authorization_generations
                (workspace_id, environment_id, generation)
            VALUES (NEW.workspace_id, NEW.id, 1);
            RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(
        f"CREATE TRIGGER trg_environments_initialize_authorization_generation "
        f"AFTER INSERT ON {SCHEMA}.environments FOR EACH ROW "
        f"EXECUTE FUNCTION {SCHEMA}.initialize_authorization_generation()"
    )

    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.advance_authorization_generation()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE
            target_identity uuid;
            previous_identity uuid;
        BEGIN
            IF TG_TABLE_NAME = 'workspace_memberships' THEN
                UPDATE {SCHEMA}.authorization_generations
                   SET generation = generation + 1,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE workspace_id = NEW.workspace_id;
            ELSIF TG_TABLE_NAME IN (
                'environment_memberships', 'environment_role_bindings'
            ) THEN
                UPDATE {SCHEMA}.authorization_generations
                   SET generation = generation + 1,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE workspace_id = NEW.workspace_id
                   AND environment_id = NEW.environment_id;
            ELSIF TG_TABLE_NAME = 'canonical_human_identity_states' THEN
                target_identity := NEW.identity_id;
                UPDATE {SCHEMA}.authorization_generations AS generation_record
                   SET generation = generation_record.generation + 1,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE EXISTS (
                    SELECT 1 FROM {SCHEMA}.workspace_memberships AS membership
                     WHERE membership.workspace_id = generation_record.workspace_id
                       AND membership.canonical_human_identity_id = target_identity
                 ) OR EXISTS (
                    SELECT 1 FROM {SCHEMA}.environment_memberships AS membership
                     WHERE membership.workspace_id = generation_record.workspace_id
                       AND membership.environment_id = generation_record.environment_id
                       AND membership.canonical_human_identity_id = target_identity
                 );
            ELSE
                SELECT binding.canonical_human_identity_id
                  INTO previous_identity
                  FROM {SCHEMA}.authentication_alias_bindings AS binding
                 WHERE binding.issuer = NEW.issuer
                   AND binding.subject = NEW.subject
                   AND binding.version < NEW.version
                 ORDER BY binding.version DESC
                 LIMIT 1;
                UPDATE {SCHEMA}.authorization_generations AS generation_record
                   SET generation = generation_record.generation + 1,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE EXISTS (
                    SELECT 1 FROM {SCHEMA}.workspace_memberships AS membership
                     WHERE membership.workspace_id = generation_record.workspace_id
                       AND membership.canonical_human_identity_id IN (
                           NEW.canonical_human_identity_id,
                           previous_identity
                       )
                 ) OR EXISTS (
                    SELECT 1 FROM {SCHEMA}.environment_memberships AS membership
                     WHERE membership.workspace_id = generation_record.workspace_id
                       AND membership.environment_id = generation_record.environment_id
                       AND membership.canonical_human_identity_id IN (
                           NEW.canonical_human_identity_id,
                           previous_identity
                       )
                 );
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
    for table in (
        "canonical_human_identity_states",
        "authentication_alias_bindings",
        "workspace_memberships",
        "environment_memberships",
        "environment_role_bindings",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_advance_generation "
            f"AFTER INSERT ON {SCHEMA}.{table} FOR EACH ROW "
            f"EXECUTE FUNCTION {SCHEMA}.advance_authorization_generation()"
        )

    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.resolve_current_authorization_snapshot(
            requested_issuer text,
            requested_subject text,
            requested_workspace_id uuid,
            requested_environment_id uuid
        )
        RETURNS TABLE (acting_subject_id uuid, authorization_generation bigint)
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
            WITH current_alias AS (
                SELECT binding.canonical_human_identity_id, binding.status
                  FROM {SCHEMA}.authentication_alias_bindings AS binding
                 WHERE binding.issuer = requested_issuer
                   AND binding.subject = requested_subject
                 ORDER BY binding.version DESC
                 LIMIT 1
            ),
            current_identity AS (
                SELECT state.status
                  FROM {SCHEMA}.canonical_human_identity_states AS state
                  JOIN current_alias AS alias
                    ON alias.canonical_human_identity_id = state.identity_id
                 ORDER BY state.version DESC
                 LIMIT 1
            ),
            current_workspace_membership AS (
                SELECT membership.status
                  FROM {SCHEMA}.workspace_memberships AS membership
                  JOIN current_alias AS alias
                    ON alias.canonical_human_identity_id =
                       membership.canonical_human_identity_id
                 WHERE membership.workspace_id = requested_workspace_id
                 ORDER BY membership.version DESC
                 LIMIT 1
            ),
            current_environment_membership AS (
                SELECT membership.status
                  FROM {SCHEMA}.environment_memberships AS membership
                  JOIN current_alias AS alias
                    ON alias.canonical_human_identity_id =
                       membership.canonical_human_identity_id
                 WHERE membership.workspace_id = requested_workspace_id
                   AND membership.environment_id = requested_environment_id
                 ORDER BY membership.version DESC
                 LIMIT 1
            ),
            current_role_binding AS (
                SELECT binding.status, binding.role
                  FROM {SCHEMA}.environment_role_bindings AS binding
                  JOIN current_alias AS alias
                    ON alias.canonical_human_identity_id =
                       binding.canonical_human_identity_id
                 WHERE binding.workspace_id = requested_workspace_id
                   AND binding.environment_id = requested_environment_id
                   AND binding.role = 'administrator'
                 ORDER BY binding.version DESC
                 LIMIT 1
            )
            SELECT alias.canonical_human_identity_id,
                   generation_record.generation
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

    for function in (
        "reject_authorization_history_mutation()",
        "initialize_authorization_generation()",
        "advance_authorization_generation()",
        "resolve_current_authorization_snapshot(text,text,uuid,uuid)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.{function} FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {SCHEMA}.resolve_current_authorization_snapshot"
        f"(text,text,uuid,uuid) TO {runtime_role}"
    )


def downgrade() -> None:
    runtime_role = _quoted_role("runtime_role")
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION {SCHEMA}.resolve_current_authorization_snapshot"
        f"(text,text,uuid,uuid) FROM {runtime_role}"
    )
    op.execute(
        f"DROP FUNCTION {SCHEMA}.resolve_current_authorization_snapshot"
        "(text,text,uuid,uuid)"
    )
    op.execute(
        f"DROP TRIGGER trg_environments_initialize_authorization_generation "
        f"ON {SCHEMA}.environments"
    )
    for table in reversed(TENANT_TABLES):
        op.drop_table(table, schema=SCHEMA)
    for table in reversed(PLATFORM_TABLES):
        op.drop_table(table, schema=SCHEMA)
    op.execute(f"DROP FUNCTION {SCHEMA}.advance_authorization_generation()")
    op.execute(f"DROP FUNCTION {SCHEMA}.initialize_authorization_generation()")
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_authorization_history_mutation()")
