"""Persist provider-neutral connection approvals, versions and safe health."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261208_76"
down_revision = "20261207_75"
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
    op.create_table(
        "provider_connection_approvals",
        sa.Column("approval_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("service_principal_id", uuid, nullable=False),
        sa.Column("provider_kind", sa.String(32), nullable=False),
        sa.Column("credential_reference", uuid, nullable=False),
        sa.Column("grant_reference", uuid, nullable=False),
        sa.Column("approved_resource_scope_references", postgresql.ARRAY(uuid), nullable=False),
        sa.Column("authorization_generation", sa.BigInteger(), nullable=False),
        sa.Column("configuration_generation", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("approval_id", name="pk_provider_connection_approvals"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_provider_connection_approval_environment",
        ),
        sa.CheckConstraint(
            "provider_kind IN ('google', 'yandex_disk', 'bitrix24', 'mounted_filesystem')",
            name="ck_provider_connection_approval_kind",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'revoked')",
            name="ck_provider_connection_approval_status",
        ),
        sa.CheckConstraint(
            "authorization_generation > 0 AND configuration_generation > 0",
            name="ck_provider_connection_approval_generations",
        ),
        sa.CheckConstraint(
            "cardinality(approved_resource_scope_references) > 0 "
            "AND array_position(approved_resource_scope_references, NULL) IS NULL",
            name="ck_provider_connection_approval_scopes",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "provider_connection_versions",
        sa.Column("connection_version_id", uuid, nullable=False),
        sa.Column("connection_id", uuid, nullable=False),
        sa.Column("approval_id", uuid, nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("predecessor_version_id", uuid, nullable=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("service_principal_id", uuid, nullable=False),
        sa.Column("provider_kind", sa.String(32), nullable=False),
        sa.Column("credential_reference", uuid, nullable=False),
        sa.Column("grant_reference", uuid, nullable=False),
        sa.Column("approved_resource_scope_references", postgresql.ARRAY(uuid), nullable=False),
        sa.Column("authorization_generation", sa.BigInteger(), nullable=False),
        sa.Column("configuration_generation", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("capabilities", postgresql.JSONB(), nullable=False),
        sa.Column("health", sa.String(32), nullable=False),
        sa.Column("diagnostic_codes", postgresql.ARRAY(sa.String(64)), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cursor_lag_seconds", sa.BigInteger(), nullable=True),
        sa.Column("last_successful_reconciliation_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("receipt_id", uuid, nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("command_digest", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("connection_version_id", name="pk_provider_connection_versions"),
        sa.UniqueConstraint("receipt_id", name="uq_provider_connection_receipt"),
        sa.UniqueConstraint(
            "workspace_id", "environment_id", "connection_id", "version",
            name="uq_provider_connection_version",
        ),
        sa.UniqueConstraint(
            "workspace_id", "environment_id", "idempotency_key",
            name="uq_provider_connection_command",
        ),
        sa.UniqueConstraint(
            "predecessor_version_id", name="uq_provider_connection_predecessor"
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"], [f"{SCHEMA}.provider_connection_approvals.approval_id"],
            name="fk_provider_connection_approval",
        ),
        sa.ForeignKeyConstraint(
            ["predecessor_version_id"],
            [f"{SCHEMA}.provider_connection_versions.connection_version_id"],
            name="fk_provider_connection_predecessor",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_provider_connection_environment",
        ),
        sa.CheckConstraint(
            "(version = 1 AND predecessor_version_id IS NULL) OR "
            "(version > 1 AND predecessor_version_id IS NOT NULL)",
            name="ck_provider_connection_history",
        ),
        sa.CheckConstraint(
            "provider_kind IN ('google', 'yandex_disk', 'bitrix24', 'mounted_filesystem')",
            name="ck_provider_connection_kind",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'revoked')",
            name="ck_provider_connection_status",
        ),
        sa.CheckConstraint(
            "health IN ('healthy', 'degraded', 'revoked', 'reconciliation_required')",
            name="ck_provider_connection_health",
        ),
        sa.CheckConstraint(
            "authorization_generation > 0 AND configuration_generation > 0",
            name="ck_provider_connection_generations",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(capabilities) = 'array'",
            name="ck_provider_connection_capabilities",
        ),
        sa.CheckConstraint(
            "array_position(diagnostic_codes, NULL) IS NULL",
            name="ck_provider_connection_diagnostics",
        ),
        sa.CheckConstraint(
            "cursor_lag_seconds IS NULL OR cursor_lag_seconds >= 0",
            name="ck_provider_connection_cursor_lag",
        ),
        sa.CheckConstraint(
            "command_digest ~ '^[0-9a-f]{64}$'",
            name="ck_provider_connection_command_digest",
        ),
        schema=SCHEMA,
    )
    for table, writable in (
        ("provider_connection_approvals", False),
        ("provider_connection_versions", True),
    ):
        _tenant_scope(table, runtime=runtime, migration=migration, writable=writable)
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE "
            f"ON {SCHEMA}.{table} FOR EACH ROW "
            f"EXECUTE FUNCTION {SCHEMA}.reject_authorization_history_mutation()"
        )
    op.execute(
        f"COMMENT ON TABLE {SCHEMA}.provider_connection_approvals IS "
        "'classification=tenant_authorization_approval; refs_only=true; secret_values=forbidden'"
    )
    op.execute(
        f"COMMENT ON TABLE {SCHEMA}.provider_connection_versions IS "
        "'classification=tenant_connection_configuration; refs_only=true; provider_content=forbidden'"
    )


def _tenant_scope(
    table: str,
    *,
    runtime: str,
    migration: str,
    writable: bool,
) -> None:
    qualified = f'{SCHEMA}."{table}"'
    ws = "nullif(current_setting('spine.workspace_id', true), '')::uuid"
    env = "nullif(current_setting('spine.environment_id', true), '')::uuid"
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(
        f'CREATE POLICY "pol_{table}_tenant_isolation" ON {qualified} FOR ALL TO {runtime} '
        f"USING (workspace_id = {ws} AND environment_id = {env}) "
        f"WITH CHECK (workspace_id = {ws} AND environment_id = {env})"
    )
    op.execute(
        f'CREATE POLICY "pol_{table}_migration_maintenance" ON {qualified} '
        f"FOR ALL TO {migration} USING (true) WITH CHECK (true)"
    )
    privileges = "SELECT, INSERT" if writable else "SELECT"
    op.execute(f"GRANT {privileges} ON TABLE {qualified} TO {runtime}")
    op.execute(
        f"REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER "
        f"ON TABLE {qualified} FROM {runtime}"
    )


def downgrade() -> None:
    op.drop_table("provider_connection_versions", schema=SCHEMA)
    op.drop_table("provider_connection_approvals", schema=SCHEMA)
