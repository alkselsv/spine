"""Add governed Google connection registration and read admission state."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261123_60"
down_revision = "20261122_59"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, attribute))


def _tenant_policy(table: str, runtime_role: str, migration_role: str) -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.{table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {SCHEMA}.{table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY pol_{table}_tenant_isolation ON {SCHEMA}.{table} "
        f"FOR ALL TO {runtime_role} "
        "USING (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid) "
        "WITH CHECK (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid)"
    )
    op.execute(
        f"CREATE POLICY pol_{table}_migration_maintenance ON {SCHEMA}.{table} "
        f"FOR ALL TO {migration_role} USING (true) WITH CHECK (true)"
    )


def upgrade() -> None:
    runtime_role = _role("runtime_role")
    migration_role = _role("migration_role")
    op.create_table(
        "google_connection_versions",
        sa.Column("connection_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("predecessor_version_id", postgresql.UUID(as_uuid=True)),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("service_principal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("credential_reference", sa.String(length=224), nullable=False),
        sa.Column("grant_reference", sa.String(length=224), nullable=False),
        sa.Column(
            "approved_resource_scope_references",
            postgresql.ARRAY(sa.String(length=224)),
            nullable=False,
        ),
        sa.Column("authorization_generation", sa.BigInteger(), nullable=False),
        sa.Column("configuration_generation", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("command_digest", sa.String(length=64), nullable=False),
        sa.Column("audit_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint(
            "connection_version_id", name="pk_google_connection_versions"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "connection_id",
            "version",
            name="uq_google_connection_version",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "idempotency_key",
            name="uq_google_connection_idempotency",
        ),
        sa.UniqueConstraint(
            "predecessor_version_id", name="uq_google_connection_predecessor"
        ),
        sa.ForeignKeyConstraint(
            ["predecessor_version_id"],
            [f"{SCHEMA}.google_connection_versions.connection_version_id"],
            name="fk_google_connection_predecessor",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_google_connection_environment",
        ),
        sa.CheckConstraint(
            "(version = 1 AND predecessor_version_id IS NULL) OR "
            "(version > 1 AND predecessor_version_id IS NOT NULL)",
            name="ck_google_connection_history",
        ),
        sa.CheckConstraint(
            "authorization_generation > 0 AND configuration_generation > 0",
            name="ck_google_connection_generations",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'revoked')",
            name="ck_google_connection_status",
        ),
        sa.CheckConstraint(
            "cardinality(approved_resource_scope_references) > 0 "
            "AND array_position(approved_resource_scope_references, NULL) IS NULL",
            name="ck_google_connection_resource_scopes",
        ),
        sa.CheckConstraint(
            "command_digest ~ '^[0-9a-f]{64}$'",
            name="ck_google_connection_command_digest",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "google_connection_audit_events",
        sa.Column("audit_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("service_principal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("authorization_generation", sa.BigInteger(), nullable=False),
        sa.Column("configuration_generation", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "resource_scope_references",
            postgresql.ARRAY(sa.String(length=224)),
            nullable=False,
        ),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint(
            "audit_event_id", name="pk_google_connection_audit_events"
        ),
        sa.ForeignKeyConstraint(
            ["connection_version_id"],
            [f"{SCHEMA}.google_connection_versions.connection_version_id"],
            name="fk_google_connection_audit_version",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_google_connection_audit_environment",
        ),
        schema=SCHEMA,
    )
    op.execute(
        f"COMMENT ON TABLE {SCHEMA}.google_connection_versions IS "
        "'classification=tenant_authorization_reference; secret_values=forbidden; "
        "provider_content=forbidden'"
    )
    op.execute(
        f"COMMENT ON TABLE {SCHEMA}.google_connection_audit_events IS "
        "'classification=safe_audit_metadata; secret_values=forbidden; "
        "provider_content=forbidden'"
    )
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.enforce_google_connection_successor()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE predecessor {SCHEMA}.google_connection_versions%ROWTYPE;
        BEGIN
            IF cardinality(NEW.approved_resource_scope_references) <> (
                SELECT count(DISTINCT scope_reference)
                  FROM unnest(NEW.approved_resource_scope_references)
                       AS scope_reference
            ) THEN
                RAISE EXCEPTION 'google connection resource scopes are duplicated'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_google_connection_resource_scopes';
            END IF;
            IF NEW.version > 1 THEN
                SELECT * INTO predecessor
                  FROM {SCHEMA}.google_connection_versions
                 WHERE connection_version_id = NEW.predecessor_version_id;
                IF NOT FOUND
                   OR predecessor.connection_id <> NEW.connection_id
                   OR predecessor.workspace_id <> NEW.workspace_id
                   OR predecessor.environment_id <> NEW.environment_id
                   OR predecessor.version + 1 <> NEW.version
                   OR predecessor.authorization_generation > NEW.authorization_generation
                   OR predecessor.configuration_generation > NEW.configuration_generation
                   OR (
                       predecessor.authorization_generation = NEW.authorization_generation
                       AND predecessor.configuration_generation = NEW.configuration_generation
                   )
                THEN
                    RAISE EXCEPTION 'google connection successor is invalid'
                        USING ERRCODE = '23514',
                              CONSTRAINT = 'ck_google_connection_successor';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.enforce_google_connection_successor() "
        "FROM PUBLIC"
    )
    op.execute(
        f"CREATE TRIGGER trg_google_connection_successor BEFORE INSERT "
        f"ON {SCHEMA}.google_connection_versions FOR EACH ROW "
        f"EXECUTE FUNCTION {SCHEMA}.enforce_google_connection_successor()"
    )
    for table in ("google_connection_versions", "google_connection_audit_events"):
        _tenant_policy(table, runtime_role, migration_role)
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE "
            f"ON {SCHEMA}.{table} FOR EACH ROW "
            f"EXECUTE FUNCTION {SCHEMA}.reject_authorization_history_mutation()"
        )
        op.execute(f"GRANT SELECT, INSERT ON {SCHEMA}.{table} TO {runtime_role}")


def downgrade() -> None:
    op.drop_table("google_connection_audit_events", schema=SCHEMA)
    op.drop_table("google_connection_versions", schema=SCHEMA)
    op.execute(f"DROP FUNCTION {SCHEMA}.enforce_google_connection_successor()")
