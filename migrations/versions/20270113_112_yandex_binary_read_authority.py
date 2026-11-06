"""Add expiring provider authority and the exact Yandex #789 provisioner."""

from alembic import op
import sqlalchemy as sa

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20270113_112"
down_revision = "20270112_111"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _settings() -> MigrationDatabaseSettings:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return settings


def _quoted_role(name: str) -> str:
    return op.get_bind().dialect.identifier_preparer.quote(name)


def upgrade() -> None:
    runtime_name = _settings().runtime_role
    runtime = _quoted_role(runtime_name)
    for table in ("provider_connection_approvals", "provider_connection_versions"):
        op.add_column(
            table,
            sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
            schema=SCHEMA,
        )
        op.create_check_constraint(
            f"ck_{table}_valid_until",
            table,
            "valid_until IS NULL OR valid_until > recorded_at",
            schema=SCHEMA,
        )

    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.provision_yandex_qualification_binary_read_approval(
            p_approval_id uuid,
            p_workspace_id uuid,
            p_environment_id uuid,
            p_connection_id uuid,
            p_predecessor_version_id uuid,
            p_service_principal_id uuid,
            p_credential_reference uuid,
            p_grant_reference uuid,
            p_resource_scope_reference uuid,
            p_authorization_generation bigint,
            p_configuration_generation bigint,
            p_valid_until timestamptz,
            p_recorded_at timestamptz
        ) RETURNS boolean
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog, {SCHEMA}
        AS $$
        DECLARE
            current_record {SCHEMA}.provider_connection_versions%ROWTYPE;
            approval_record {SCHEMA}.provider_connection_approvals%ROWTYPE;
        BEGIN
            IF session_user <> '{runtime_name}' THEN
                RAISE EXCEPTION 'Yandex binary-read provisioning denied'
                    USING ERRCODE = '42501';
            END IF;
            IF current_setting('spine.workspace_id', true) IS DISTINCT FROM p_workspace_id::text
               OR current_setting('spine.environment_id', true) IS DISTINCT FROM p_environment_id::text
               OR p_approval_id <> '6c18ec26-4024-52c5-9c2b-dad11aba208f'::uuid
               OR p_connection_id <> 'd62e75a6-6487-5ae8-a551-d1ccb37d89c8'::uuid
               OR p_service_principal_id <> '1a25e55c-2837-5d5e-b423-615488ee5d95'::uuid
               OR p_credential_reference <> 'e6bd80cf-875c-5411-a160-a024c17eb6b1'::uuid
               OR p_grant_reference <> 'e93deb25-93d9-53ea-8703-baa635508eb4'::uuid
               OR p_resource_scope_reference <> '01dfe7b0-652c-57bd-8e0b-2bce66d1982e'::uuid
               OR p_valid_until <= p_recorded_at
               OR p_valid_until <= clock_timestamp()
               OR p_valid_until > p_recorded_at + interval '7 days'
            THEN
                RAISE EXCEPTION 'Yandex binary-read provisioning denied'
                    USING ERRCODE = '42501';
            END IF;

            SELECT * INTO current_record
            FROM {SCHEMA}.provider_connection_versions
            WHERE connection_id = p_connection_id
              AND workspace_id = p_workspace_id
              AND environment_id = p_environment_id
            ORDER BY version DESC
            LIMIT 1
            FOR UPDATE;

            IF NOT FOUND
               OR current_record.connection_version_id <> p_predecessor_version_id
               OR current_record.version <> 1
               OR current_record.approval_id
                    <> '03865999-0abe-53c6-b845-8cdfb441e238'::uuid
               OR current_record.provider_kind <> 'yandex_disk'
               OR current_record.service_principal_id <> p_service_principal_id
               OR current_record.credential_reference <> p_credential_reference
               OR current_record.grant_reference <> p_grant_reference
               OR current_record.approved_resource_scope_references
                    <> ARRAY[p_resource_scope_reference]::uuid[]
               OR current_record.authorization_generation <> p_authorization_generation
               OR current_record.configuration_generation + 1
                    <> p_configuration_generation
               OR current_record.status <> 'active'
               OR current_record.valid_until IS NOT NULL
               OR current_record.permitted_operations <> ARRAY[]::varchar[]
               OR current_record.capabilities <> '[
                    {{"schema_version":"provider-capability:v2","kind":"enumeration","status":"supported","qualification_reference":{{"value":"64f31622-ccbe-5187-aba5-132cbefbaf83"}}}},
                    {{"schema_version":"provider-capability:v2","kind":"full_reconciliation","status":"supported","qualification_reference":{{"value":"64f31622-ccbe-5187-aba5-132cbefbaf83"}}}},
                    {{"schema_version":"provider-capability:v2","kind":"metadata_read","status":"supported","qualification_reference":{{"value":"64f31622-ccbe-5187-aba5-132cbefbaf83"}}}}
                  ]'::jsonb
            THEN
                RAISE EXCEPTION 'Yandex binary-read provisioning denied'
                    USING ERRCODE = '42501';
            END IF;

            SELECT * INTO approval_record
            FROM {SCHEMA}.provider_connection_approvals
            WHERE approval_id = p_approval_id;
            IF FOUND THEN
                IF approval_record.workspace_id = p_workspace_id
                   AND approval_record.environment_id = p_environment_id
                   AND approval_record.service_principal_id = p_service_principal_id
                   AND approval_record.provider_kind = 'yandex_disk'
                   AND approval_record.credential_reference = p_credential_reference
                   AND approval_record.grant_reference = p_grant_reference
                   AND approval_record.approved_resource_scope_references
                        = ARRAY[p_resource_scope_reference]::uuid[]
                   AND approval_record.authorization_generation
                        = p_authorization_generation
                   AND approval_record.configuration_generation
                        = p_configuration_generation
                   AND approval_record.status = 'active'
                   AND approval_record.valid_until = p_valid_until
                THEN
                    RETURN false;
                END IF;
                RAISE EXCEPTION 'Yandex binary-read provisioning denied'
                    USING ERRCODE = '42501';
            END IF;

            INSERT INTO {SCHEMA}.provider_connection_approvals (
                approval_id, workspace_id, environment_id, service_principal_id,
                provider_kind, credential_reference, grant_reference,
                approved_resource_scope_references, permitted_operations,
                authorization_generation, configuration_generation, status,
                recorded_at, valid_until
            ) VALUES (
                p_approval_id, p_workspace_id, p_environment_id,
                p_service_principal_id, 'yandex_disk', p_credential_reference,
                p_grant_reference, ARRAY[p_resource_scope_reference]::uuid[],
                ARRAY[]::varchar[], p_authorization_generation,
                p_configuration_generation, 'active', p_recorded_at, p_valid_until
            );
            RETURN true;
        END;
        $$
        """
    )
    signature = (
        f"{SCHEMA}.provision_yandex_qualification_binary_read_approval("
        "uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,bigint,bigint,"
        "timestamptz,timestamptz)"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {runtime}")


def downgrade() -> None:
    op.execute(
        f"DROP FUNCTION IF EXISTS {SCHEMA}."
        "provision_yandex_qualification_binary_read_approval("
        "uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,bigint,bigint,"
        "timestamptz,timestamptz)"
    )
    for table in ("provider_connection_versions", "provider_connection_approvals"):
        op.drop_constraint(
            f"ck_{table}_valid_until", table, schema=SCHEMA, type_="check"
        )
        op.drop_column(table, "valid_until", schema=SCHEMA)
