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


def _role_exists(name: str) -> bool:
    return bool(
        op.get_bind().execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"),
            {"role": name},
        ).scalar_one()
    )


def upgrade() -> None:
    control_name = _settings().control_role
    if not _role_exists(control_name):
        raise RuntimeError(
            "control role must be created by operator bootstrap before migration 112"
        )
    control = _quoted_role(control_name)
    for table in ("provider_connection_approvals", "provider_connection_versions"):
        op.add_column(
            table,
            sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
            schema=SCHEMA,
        )
        op.add_column(
            table,
            sa.Column("approved_manifest_digest", sa.String(64), nullable=True),
            schema=SCHEMA,
        )
        op.create_check_constraint(
            f"ck_{table}_valid_until",
            table,
            "valid_until IS NULL OR valid_until > recorded_at",
            schema=SCHEMA,
        )
        op.create_check_constraint(
            f"ck_{table}_approved_manifest_digest",
            table,
            "approved_manifest_digest IS NULL OR "
            "approved_manifest_digest ~ '^[0-9a-f]{64}$'",
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
            p_manifest_digest varchar(64),
            p_valid_until timestamptz,
            p_recorded_at timestamptz,
            p_connection_version_id uuid,
            p_receipt_id uuid,
            p_idempotency_key varchar(255),
            p_command_digest varchar(64),
            p_authority_audit_event_id uuid,
            p_connection_audit_event_id uuid,
            p_acting_subject_id uuid,
            p_trace_id uuid,
            p_authority_payload jsonb,
            p_connection_payload jsonb
        ) RETURNS boolean
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog, {SCHEMA}
        AS $$
        DECLARE
            current_record {SCHEMA}.provider_connection_versions%ROWTYPE;
            approval_record {SCHEMA}.provider_connection_approvals%ROWTYPE;
        BEGIN
            IF session_user <> '{control_name}' THEN
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
               OR p_manifest_digest <> '487360a49df37aa6fd3dac3b78750b0d991a8a7de44f5fc405a85f304befedc7'
               OR p_idempotency_key <> 'issue-789-yandex-binary-read-authority-v1'
               OR p_command_digest !~ '^[0-9a-f]{{64}}$'
               OR p_connection_version_id = '00000000-0000-0000-0000-000000000000'::uuid
               OR p_receipt_id = '00000000-0000-0000-0000-000000000000'::uuid
               OR p_authority_audit_event_id = '00000000-0000-0000-0000-000000000000'::uuid
               OR p_connection_audit_event_id = '00000000-0000-0000-0000-000000000000'::uuid
               OR p_acting_subject_id = '00000000-0000-0000-0000-000000000000'::uuid
               OR p_trace_id = '00000000-0000-0000-0000-000000000000'::uuid
               OR p_recorded_at > clock_timestamp()
               OR p_valid_until <= p_recorded_at
               OR p_valid_until <= clock_timestamp()
               OR p_valid_until > clock_timestamp() + interval '7 days'
               OR (SELECT count(*) FROM jsonb_object_keys(p_authority_payload)) <> 8
               OR p_authority_payload->>'security_approval_reference'
                    <> 'issue-789-comment-6687500665'
               OR p_authority_payload->>'data_approval_reference'
                    <> 'issue-789-comment-6687500665'
               OR p_authority_payload->>'manifest_digest' <> p_manifest_digest
               OR (p_authority_payload->>'resource_scope_reference_id')::uuid
                    <> p_resource_scope_reference
               OR (p_authority_payload->>'qualification_reference_id')::uuid
                    <> '9a276053-634f-581b-bc55-24e1f69fb24b'::uuid
               OR (p_authority_payload->>'valid_until')::timestamptz <> p_valid_until
               OR (p_authority_payload->>'max_live_runs')::integer <> 10
               OR p_authority_payload->>'redirect_host_digest'
                    <> 'e21421ebdaf188dc6084e98cd73b864a3a8a574de8fb0644dabaacd8c57745f4'
               OR p_connection_payload
                    <> '{{"command_type":"provider_connection_configuration"}}'::jsonb
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
                   AND approval_record.approved_manifest_digest = p_manifest_digest
                   AND EXISTS (
                       SELECT 1 FROM {SCHEMA}.provider_connection_versions AS version_record
                       WHERE version_record.connection_version_id = p_connection_version_id
                         AND version_record.connection_id = p_connection_id
                         AND version_record.approval_id = p_approval_id
                         AND version_record.version = 2
                         AND version_record.predecessor_version_id = p_predecessor_version_id
                         AND version_record.workspace_id = p_workspace_id
                         AND version_record.environment_id = p_environment_id
                         AND version_record.service_principal_id = p_service_principal_id
                         AND version_record.provider_kind = 'yandex_disk'
                         AND version_record.credential_reference = p_credential_reference
                         AND version_record.grant_reference = p_grant_reference
                         AND version_record.approved_resource_scope_references
                              = ARRAY[p_resource_scope_reference]::uuid[]
                         AND version_record.authorization_generation = p_authorization_generation
                         AND version_record.configuration_generation = p_configuration_generation
                         AND version_record.status = 'active'
                         AND version_record.valid_until = p_valid_until
                         AND version_record.approved_manifest_digest = p_manifest_digest
                         AND version_record.receipt_id = p_receipt_id
                         AND version_record.idempotency_key = p_idempotency_key
                         AND version_record.command_digest = p_command_digest
                         AND version_record.recorded_at = p_recorded_at
                   )
                   AND EXISTS (
                       SELECT 1 FROM {SCHEMA}.audit_events AS audit
                       WHERE audit.audit_event_id = p_authority_audit_event_id
                         AND audit.event_type = 'yandex.qualification_binary_read_authority'
                         AND audit.payload = p_authority_payload
                   )
                   AND EXISTS (
                       SELECT 1 FROM {SCHEMA}.audit_events AS audit
                       WHERE audit.audit_event_id = p_connection_audit_event_id
                         AND audit.event_type = 'command.outcome'
                         AND audit.payload = p_connection_payload
                   )
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
                recorded_at, valid_until, approved_manifest_digest
            ) VALUES (
                p_approval_id, p_workspace_id, p_environment_id,
                p_service_principal_id, 'yandex_disk', p_credential_reference,
                p_grant_reference, ARRAY[p_resource_scope_reference]::uuid[],
                ARRAY[]::varchar[], p_authorization_generation,
                p_configuration_generation, 'active', p_recorded_at, p_valid_until,
                p_manifest_digest
            );

            INSERT INTO {SCHEMA}.audit_events (
                audit_event_id, workspace_id, environment_id, event_type,
                schema_version, origin, acting_subject_id, service_principal_id,
                trace_id, correlation_id, causation_id, occurred_at, appended_at,
                target_type, target_id, target_schema_version, outcome, reason,
                producer_deduplication_id, payload
            ) VALUES
            (
                p_authority_audit_event_id, p_workspace_id, p_environment_id,
                'yandex.qualification_binary_read_authority', 1, 'interactive',
                p_acting_subject_id, NULL, p_trace_id, NULL, NULL, p_recorded_at,
                p_recorded_at, 'provider_connection_approval', p_approval_id, 2,
                'accepted', 'qualification_binary_read_authority_recorded',
                'issue-789-yandex-binary-read-authority-v1:authority',
                p_authority_payload
            ),
            (
                p_connection_audit_event_id, p_workspace_id, p_environment_id,
                'command.outcome', 1, 'interactive', p_acting_subject_id, NULL,
                p_trace_id, NULL, NULL, p_recorded_at, p_recorded_at,
                'provider_connection_version', p_connection_version_id, 1,
                'accepted', 'connection_version_registered', p_idempotency_key,
                p_connection_payload
            );

            INSERT INTO {SCHEMA}.provider_connection_versions (
                connection_version_id, connection_id, approval_id, version,
                predecessor_version_id, workspace_id, environment_id,
                service_principal_id, provider_kind, credential_reference,
                grant_reference, approved_resource_scope_references,
                permitted_operations, authorization_generation,
                configuration_generation, status, valid_until,
                approved_manifest_digest, capabilities, health,
                diagnostic_codes, observed_at, cursor_lag_seconds,
                last_successful_reconciliation_at, receipt_id, idempotency_key,
                command_digest, recorded_at
            ) VALUES (
                p_connection_version_id, p_connection_id, p_approval_id, 2,
                p_predecessor_version_id, p_workspace_id, p_environment_id,
                p_service_principal_id, 'yandex_disk', p_credential_reference,
                p_grant_reference, ARRAY[p_resource_scope_reference]::uuid[],
                ARRAY[]::varchar[], p_authorization_generation,
                p_configuration_generation, 'active', p_valid_until,
                p_manifest_digest,
                '[
                  {{"schema_version":"provider-capability:v2","kind":"binary_read","status":"supported","qualification_reference":{{"value":"9a276053-634f-581b-bc55-24e1f69fb24b"}}}},
                  {{"schema_version":"provider-capability:v2","kind":"enumeration","status":"supported","qualification_reference":{{"value":"64f31622-ccbe-5187-aba5-132cbefbaf83"}}}},
                  {{"schema_version":"provider-capability:v2","kind":"full_reconciliation","status":"supported","qualification_reference":{{"value":"64f31622-ccbe-5187-aba5-132cbefbaf83"}}}},
                  {{"schema_version":"provider-capability:v2","kind":"metadata_read","status":"supported","qualification_reference":{{"value":"64f31622-ccbe-5187-aba5-132cbefbaf83"}}}}
                ]'::jsonb,
                'healthy', ARRAY['connection_ready']::varchar[], p_recorded_at,
                NULL, NULL, p_receipt_id, p_idempotency_key, p_command_digest,
                p_recorded_at
            );
            RETURN true;
        END;
        $$
        """
    )
    signature = (
        f"{SCHEMA}.provision_yandex_qualification_binary_read_approval("
        "uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,bigint,bigint,varchar,"
        "timestamptz,timestamptz,uuid,uuid,varchar,varchar,uuid,uuid,uuid,uuid,"
        "jsonb,jsonb)"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    ws = "nullif(current_setting('spine.workspace_id', true), '')::uuid"
    env = "nullif(current_setting('spine.environment_id', true), '')::uuid"
    tenant = f"workspace_id = ({ws}) AND environment_id = ({env})"
    op.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {control}")
    op.execute(
        f"CREATE POLICY pol_provider_connection_approvals_control_789 "
        f"ON {SCHEMA}.provider_connection_approvals FOR SELECT TO {control} "
        f"USING ({tenant} AND approval_id IN ("
        f"'03865999-0abe-53c6-b845-8cdfb441e238'::uuid, "
        f"'6c18ec26-4024-52c5-9c2b-dad11aba208f'::uuid))"
    )
    op.execute(
        f"CREATE POLICY pol_provider_connection_versions_control_789 "
        f"ON {SCHEMA}.provider_connection_versions FOR SELECT TO {control} "
        f"USING ({tenant} AND connection_id = "
        f"'d62e75a6-6487-5ae8-a551-d1ccb37d89c8'::uuid)"
    )
    op.execute(
        f"CREATE POLICY pol_audit_events_control_789 "
        f"ON {SCHEMA}.audit_events FOR SELECT TO {control} "
        f"USING ({tenant} AND producer_deduplication_id IN ("
        f"'issue-789-yandex-binary-read-authority-v1', "
        f"'issue-789-yandex-binary-read-authority-v1:authority'))"
    )
    op.execute(
        f"GRANT SELECT ON TABLE {SCHEMA}.provider_connection_approvals, "
        f"{SCHEMA}.provider_connection_versions, {SCHEMA}.audit_events TO {control}"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {control}")


def downgrade() -> None:
    control_name = _settings().control_role
    control = _quoted_role(control_name)
    if _role_exists(control_name):
        op.execute(
            f"DROP POLICY IF EXISTS pol_audit_events_control_789 "
            f"ON {SCHEMA}.audit_events"
        )
        op.execute(
            f"DROP POLICY IF EXISTS pol_provider_connection_versions_control_789 "
            f"ON {SCHEMA}.provider_connection_versions"
        )
        op.execute(
            f"DROP POLICY IF EXISTS pol_provider_connection_approvals_control_789 "
            f"ON {SCHEMA}.provider_connection_approvals"
        )
        op.execute(
            f"REVOKE SELECT ON TABLE {SCHEMA}.provider_connection_approvals "
            f"FROM {control}"
        )
        op.execute(
            f"REVOKE SELECT ON TABLE {SCHEMA}.provider_connection_versions, "
            f"{SCHEMA}.audit_events FROM {control}"
        )
        op.execute(f"REVOKE USAGE ON SCHEMA {SCHEMA} FROM {control}")
    op.execute(
        f"DROP FUNCTION IF EXISTS {SCHEMA}."
        "provision_yandex_qualification_binary_read_approval("
        "uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,bigint,bigint,varchar,"
        "timestamptz,timestamptz,uuid,uuid,varchar,varchar,uuid,uuid,uuid,uuid,"
        "jsonb,jsonb)"
    )
    for table in ("provider_connection_versions", "provider_connection_approvals"):
        op.drop_constraint(
            f"ck_{table}_valid_until", table, schema=SCHEMA, type_="check"
        )
        op.drop_constraint(
            f"ck_{table}_approved_manifest_digest",
            table,
            schema=SCHEMA,
            type_="check",
        )
        op.drop_column(table, "approved_manifest_digest", schema=SCHEMA)
        op.drop_column(table, "valid_until", schema=SCHEMA)
