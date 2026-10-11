"""Atomically finalize outbox delivery with its required Audit Event.

Revision ID: 20261012_14
Revises: 20261011_13
"""

from __future__ import annotations

from alembic import op

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261012_14"
down_revision = "20261011_13"
branch_labels = None
depends_on = None

SCHEMA = "spine"
FUNCTION = "finalize_outbox_delivery"
_IDENTIFIER_PATTERN = "^[a-z][a-z0-9_.:-]{0,127}$"


def _quoted_role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    role = getattr(settings, attribute)
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    dispatcher_role = _quoted_role("dispatcher_role")
    signature = (
        f"{SCHEMA}.{FUNCTION}(uuid, text, uuid, integer, integer, text, "
        "timestamp with time zone, timestamp with time zone, text, uuid, uuid)"
    )
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.{FUNCTION}(
            p_event_id uuid,
            p_worker_identity text,
            p_lease_token uuid,
            p_attempt_number integer,
            p_delivery_version integer,
            p_status text,
            p_occurred_at timestamp with time zone,
            p_next_attempt_at timestamp with time zone,
            p_error_code text,
            p_audit_event_id uuid,
            p_service_principal_id uuid
        )
        RETURNS SETOF {SCHEMA}.outbox_deliveries
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE
            changed {SCHEMA}.outbox_deliveries%ROWTYPE;
            source_intent {SCHEMA}.outbox_intents%ROWTYPE;
            audit_outcome text;
            audit_reason text;
        BEGIN
            IF p_worker_identity IS NULL
               OR p_worker_identity !~ '{_IDENTIFIER_PATTERN}'
               OR p_attempt_number <= 0
               OR p_delivery_version <= 0
               OR p_occurred_at IS NULL
               OR p_audit_event_id IS NULL
               OR p_audit_event_id = '00000000-0000-0000-0000-000000000000'::uuid
               OR p_service_principal_id IS NULL
               OR p_service_principal_id = '00000000-0000-0000-0000-000000000000'::uuid
               OR NOT (
                   (p_status = 'delivered' AND p_next_attempt_at IS NULL
                    AND p_error_code IS NULL)
                   OR
                   (p_status = 'retry_scheduled'
                    AND p_next_attempt_at > p_occurred_at
                    AND p_error_code ~ '{_IDENTIFIER_PATTERN}')
                   OR
                   (p_status = 'quarantined' AND p_next_attempt_at IS NULL
                    AND p_error_code ~ '{_IDENTIFIER_PATTERN}')
               ) THEN
                RAISE EXCEPTION 'Outbox delivery finalization is invalid.'
                    USING ERRCODE = '22023';
            END IF;

            SELECT * INTO source_intent
            FROM {SCHEMA}.outbox_intents
            WHERE event_id = p_event_id;
            IF NOT FOUND THEN
                RETURN;
            END IF;

            UPDATE {SCHEMA}.outbox_deliveries
            SET status = p_status,
                next_attempt_at = CASE
                    WHEN p_status = 'retry_scheduled' THEN p_next_attempt_at
                    ELSE NULL
                END,
                lease_owner = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                last_error_code = p_error_code,
                delivered_at = CASE
                    WHEN p_status = 'delivered' THEN p_occurred_at ELSE NULL
                END,
                quarantined_at = CASE
                    WHEN p_status = 'quarantined' THEN p_occurred_at ELSE NULL
                END,
                version = version + 1,
                updated_at = p_occurred_at
            WHERE event_id = p_event_id
              AND status = 'in_flight'
              AND lease_owner = p_worker_identity
              AND lease_token = p_lease_token
              AND attempt_count = p_attempt_number
              AND version = p_delivery_version
              AND lease_expires_at > p_occurred_at
            RETURNING * INTO changed;
            IF NOT FOUND THEN
                RETURN;
            END IF;

            audit_outcome := CASE p_status
                WHEN 'delivered' THEN 'succeeded'
                WHEN 'retry_scheduled' THEN 'retry_scheduled'
                ELSE 'quarantined'
            END;
            audit_reason := CASE
                WHEN p_status = 'delivered' THEN 'delivery.succeeded'
                ELSE p_error_code
            END;

            INSERT INTO {SCHEMA}.audit_events (
                audit_event_id, workspace_id, environment_id, event_type,
                schema_version, origin, acting_subject_id, service_principal_id,
                trace_id, correlation_id, causation_id, occurred_at,
                target_type, target_id, target_schema_version, outcome, reason,
                producer_deduplication_id, payload
            ) VALUES (
                p_audit_event_id, source_intent.workspace_id,
                source_intent.environment_id, 'outbox.delivery', 1, 'worker',
                NULL, p_service_principal_id, source_intent.trace_id,
                source_intent.correlation_id, source_intent.event_id,
                p_occurred_at, NULL, NULL, NULL, audit_outcome, audit_reason,
                'delivery.' || replace(source_intent.event_id::text, '-', '') ||
                    '.' || p_attempt_number::text || '.' || audit_outcome,
                jsonb_build_object(
                    'outbox_event_id', source_intent.event_id,
                    'attempt_id', p_lease_token,
                    'attempt_number', p_attempt_number
                )
            );
            RETURN NEXT changed;
        END;
        $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {dispatcher_role}")


def downgrade() -> None:
    signature = (
        f"{SCHEMA}.{FUNCTION}(uuid, text, uuid, integer, integer, text, "
        "timestamp with time zone, timestamp with time zone, text, uuid, uuid)"
    )
    op.execute(f"DROP FUNCTION {signature}")
