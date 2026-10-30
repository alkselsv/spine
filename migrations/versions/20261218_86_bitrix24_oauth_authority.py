"""Persist refs-only Bitrix24 OAuth binding, callback and refresh authority."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings

revision = "20261218_86"
down_revision = "20261217_85"
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
        "uq_provider_connection_version_bitrix24_binding",
        "provider_connection_versions",
        [
            "connection_version_id",
            "workspace_id",
            "environment_id",
            "approval_id",
            "connection_id",
            "grant_reference",
            "authorization_generation",
            "configuration_generation",
        ],
        schema=SCHEMA,
    )
    op.create_table(
        "bitrix24_grant_bindings",
        sa.Column("binding_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("approval_id", uuid, nullable=False),
        sa.Column("connection_id", uuid, nullable=False),
        sa.Column("connection_version_id", uuid, nullable=False),
        sa.Column("grant_reference", uuid, nullable=False),
        sa.Column("portal_member_id", sa.String(255), nullable=False),
        sa.Column("application_id", sa.String(255), nullable=False),
        sa.Column("acting_user_id", sa.String(255), nullable=False),
        sa.Column("granted_scopes", postgresql.ARRAY(sa.String(64)), nullable=False),
        sa.Column("authorization_generation", sa.BigInteger(), nullable=False),
        sa.Column("configuration_generation", sa.BigInteger(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("binding_id", name="pk_bitrix24_grant_bindings"),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "connection_version_id",
            "grant_reference",
            name="uq_bitrix24_grant_binding_authority",
        ),
        sa.UniqueConstraint(
            "binding_id",
            "workspace_id",
            "environment_id",
            name="uq_bitrix24_grant_binding_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_bitrix24_grant_binding_environment",
        ),
        sa.ForeignKeyConstraint(
            [
                "connection_version_id",
                "workspace_id",
                "environment_id",
                "approval_id",
                "connection_id",
                "grant_reference",
                "authorization_generation",
                "configuration_generation",
            ],
            [
                f"{SCHEMA}.provider_connection_versions.connection_version_id",
                f"{SCHEMA}.provider_connection_versions.workspace_id",
                f"{SCHEMA}.provider_connection_versions.environment_id",
                f"{SCHEMA}.provider_connection_versions.approval_id",
                f"{SCHEMA}.provider_connection_versions.connection_id",
                f"{SCHEMA}.provider_connection_versions.grant_reference",
                f"{SCHEMA}.provider_connection_versions.authorization_generation",
                f"{SCHEMA}.provider_connection_versions.configuration_generation",
            ],
            name="fk_bitrix24_grant_binding_connection_version",
        ),
        sa.CheckConstraint(
            "authorization_generation > 0 AND configuration_generation > 0",
            name="ck_bitrix24_grant_binding_generations",
        ),
        sa.CheckConstraint(
            "cardinality(granted_scopes) > 0 "
            "AND granted_scopes = ARRAY['disk']::varchar[]",
            name="ck_bitrix24_grant_binding_scopes",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "bitrix24_grant_authorities",
        sa.Column("binding_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("active_pair_reference", uuid, nullable=False),
        sa.Column("token_epoch", sa.BigInteger(), nullable=False),
        sa.Column("security_fence", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("refresh_attempt_id", uuid, nullable=True),
        sa.Column("incident_reference", uuid, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("binding_id", name="pk_bitrix24_grant_authorities"),
        sa.ForeignKeyConstraint(
            ["binding_id", "workspace_id", "environment_id"],
            [
                f"{SCHEMA}.bitrix24_grant_bindings.binding_id",
                f"{SCHEMA}.bitrix24_grant_bindings.workspace_id",
                f"{SCHEMA}.bitrix24_grant_bindings.environment_id",
            ],
            name="fk_bitrix24_grant_authority_binding",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_bitrix24_grant_authority_environment",
        ),
        sa.CheckConstraint("token_epoch > 0", name="ck_bitrix24_grant_authority_epoch"),
        sa.CheckConstraint(
            "security_fence >= 0", name="ck_bitrix24_grant_authority_fence"
        ),
        sa.CheckConstraint(
            "state IN ('ready', 'refresh_claimed', 'uncertain', "
            "'reauthorization_required', 'contained')",
            name="ck_bitrix24_grant_authority_state",
        ),
        sa.CheckConstraint(
            "(state = 'refresh_claimed' AND refresh_attempt_id IS NOT NULL) OR "
            "(state <> 'refresh_claimed')",
            name="ck_bitrix24_grant_authority_refresh_claim",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "bitrix24_oauth_attempts",
        sa.Column("attempt_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("approval_id", uuid, nullable=False),
        sa.Column("connection_id", uuid, nullable=False),
        sa.Column("connection_version_id", uuid, nullable=False),
        sa.Column("grant_reference", uuid, nullable=False),
        sa.Column("portal_member_id", sa.String(255), nullable=False),
        sa.Column("application_id", sa.String(255), nullable=False),
        sa.Column("installing_user_id", sa.String(255), nullable=False),
        sa.Column("approved_scopes", postgresql.ARRAY(sa.String(64)), nullable=False),
        sa.Column("authorization_generation", sa.BigInteger(), nullable=False),
        sa.Column("configuration_generation", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("attempt_id", name="pk_bitrix24_oauth_attempts"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_bitrix24_oauth_attempt_environment",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'uncertain', 'accepted', 'rejected', 'quarantined')",
            name="ck_bitrix24_oauth_attempt_status",
        ),
        sa.CheckConstraint(
            "cardinality(approved_scopes) > 0 "
            "AND array_position(approved_scopes, NULL) IS NULL",
            name="ck_bitrix24_oauth_attempt_scopes",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_bitrix24_oauth_attempt_pending_tuple",
        "bitrix24_oauth_attempts",
        [
            "workspace_id",
            "environment_id",
            "application_id",
            "portal_member_id",
            "installing_user_id",
        ],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("status IN ('pending', 'uncertain')"),
    )
    op.create_table(
        "bitrix24_oauth_callback_receipts",
        sa.Column("callback_reference", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("attempt_id", uuid, nullable=False),
        sa.Column("payload_fingerprint", sa.String(64), nullable=False),
        sa.Column("receipt_id", uuid, nullable=False),
        sa.Column("binding_id", uuid, nullable=False),
        sa.Column("pair_candidate_reference", uuid, nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "callback_reference", name="pk_bitrix24_oauth_callback_receipts"
        ),
        sa.UniqueConstraint("receipt_id", name="uq_bitrix24_oauth_callback_receipt"),
        sa.UniqueConstraint("attempt_id", name="uq_bitrix24_oauth_callback_attempt"),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "payload_fingerprint",
            name="uq_bitrix24_oauth_callback_fingerprint",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            [f"{SCHEMA}.bitrix24_oauth_attempts.attempt_id"],
            name="fk_bitrix24_oauth_callback_attempt",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_bitrix24_oauth_callback_environment",
        ),
        sa.CheckConstraint(
            "payload_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_bitrix24_oauth_callback_fingerprint",
        ),
        sa.CheckConstraint(
            "outcome IN ('accepted', 'quarantined')",
            name="ck_bitrix24_oauth_callback_outcome",
        ),
        schema=SCHEMA,
    )
    for table, privileges in (
        ("bitrix24_grant_bindings", "SELECT, INSERT"),
        ("bitrix24_grant_authorities", "SELECT, INSERT, UPDATE"),
        ("bitrix24_oauth_attempts", "SELECT, INSERT, UPDATE"),
        ("bitrix24_oauth_callback_receipts", "SELECT, INSERT, UPDATE"),
    ):
        _tenant_scope(
            table,
            runtime=runtime,
            migration=migration,
            privileges=privileges,
        )
    op.execute(
        f"CREATE TRIGGER trg_bitrix24_grant_bindings_immutable BEFORE UPDATE OR DELETE "
        f"ON {SCHEMA}.bitrix24_grant_bindings FOR EACH ROW "
        f"EXECUTE FUNCTION {SCHEMA}.reject_authorization_history_mutation()"
    )
    for table in (
        "bitrix24_grant_bindings",
        "bitrix24_grant_authorities",
        "bitrix24_oauth_attempts",
        "bitrix24_oauth_callback_receipts",
    ):
        op.execute(
            f"COMMENT ON TABLE {SCHEMA}.{table} IS "
            "'classification=tenant_oauth_authority; refs_only=true; "
            "secret_values_and_provider_payloads=forbidden'"
        )


def _tenant_scope(
    table: str,
    *,
    runtime: str,
    migration: str,
    privileges: str,
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
    op.execute(f"REVOKE ALL ON TABLE {qualified} FROM {runtime}")
    op.execute(f"GRANT {privileges} ON TABLE {qualified} TO {runtime}")


def downgrade() -> None:
    op.drop_table("bitrix24_oauth_callback_receipts", schema=SCHEMA)
    op.drop_index(
        "uq_bitrix24_oauth_attempt_pending_tuple",
        table_name="bitrix24_oauth_attempts",
        schema=SCHEMA,
    )
    op.drop_table("bitrix24_oauth_attempts", schema=SCHEMA)
    op.drop_table("bitrix24_grant_authorities", schema=SCHEMA)
    op.drop_table("bitrix24_grant_bindings", schema=SCHEMA)
    op.drop_constraint(
        "uq_provider_connection_version_bitrix24_binding",
        "provider_connection_versions",
        type_="unique",
        schema=SCHEMA,
    )
