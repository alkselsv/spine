"""Remove exact Gmail lineage from shared email-sync coordination rows.

Email sync pages are shared checkpoint envelopes. Their message effects are
removed selectively while the coordination envelope and unrelated effects are
preserved. This hook runs only as part of the existing governed source delete.
"""

from alembic import op

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20270115_114"
down_revision = "20270114_113"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, attribute))


def upgrade() -> None:
    executor = _role("erasure_role")
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.email_sync_erasure_scope_allowed(
            p_workspace_id uuid, p_environment_id uuid
        ) RETURNS boolean
        LANGUAGE sql STABLE SECURITY INVOKER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
            SELECT current_user = '{executor.strip(chr(34))}'
               AND EXISTS (
                    SELECT 1 FROM {SCHEMA}.source_erasure_execution_contexts c
                     WHERE c.transaction_id = pg_current_xact_id()
                       AND c.workspace_id = p_workspace_id
                       AND c.environment_id = p_environment_id
                       AND c.target_table = '__scope__'
                )
        $function$
        """
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.email_sync_erasure_scope_allowed(uuid, uuid) "
        "FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {SCHEMA}.email_sync_erasure_scope_allowed(uuid, uuid) "
        f"TO {executor}"
    )

    # The helper is owned by the non-login executor and is reachable only from
    # the existing source_objects DELETE trigger. FORCE RLS remains enabled;
    # policies grant this transaction-scoped, tenant-scoped helper access.
    for table in (
        "email_sync_attempts",
        "email_sync_pages",
        "email_sync_effect_receipts",
        "email_sync_state_observations",
        "outbox_intents",
        "outbox_deliveries",
    ):
        op.execute(
            f"CREATE POLICY pol_{table}_governed_erasure ON {SCHEMA}.{table} "
            f"FOR ALL TO {executor} USING "
            f"({SCHEMA}.email_sync_erasure_scope_allowed(workspace_id, environment_id)) "
            f"WITH CHECK ({SCHEMA}.email_sync_erasure_scope_allowed(workspace_id, environment_id))"
        )
        grants = {
            "email_sync_pages": "SELECT, UPDATE",
            "email_sync_attempts": "SELECT, UPDATE",
            "email_sync_effect_receipts": "SELECT, INSERT, UPDATE, DELETE",
            "email_sync_state_observations": "SELECT, UPDATE, DELETE",
            "outbox_intents": "SELECT, UPDATE, DELETE",
        }.get(table, "SELECT, DELETE")
        op.execute(f"GRANT {grants} ON {SCHEMA}.{table} TO {executor}")

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.reject_outbox_intent_mutation()
        RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        BEGIN
            IF TG_OP = 'DELETE'
               AND current_user = '{executor.strip(chr(34))}'
               AND EXISTS (
                    SELECT 1 FROM {SCHEMA}.source_erasure_execution_contexts c
                     WHERE c.transaction_id = pg_current_xact_id()
                       AND c.workspace_id = OLD.workspace_id
                       AND c.environment_id = OLD.environment_id
                       AND c.target_table = 'outbox_intents'
                       AND c.target_record = to_jsonb(OLD)
                ) THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'Outbox intents are immutable.'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'ck_outbox_intents_immutable';
        END
        $function$
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.erase_email_sync_source_lineage()
        RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE
            message_digest text;
            page_row record;
            effect jsonb;
            kept_effects jsonb;
            removed_keys text[];
            old_page_id text;
            new_page_id text;
            new_record jsonb;
            attempt_row record;
            observation_row record;
            intent_row record;
            receipt_row record;
            kept_receipts jsonb;
        BEGIN
            IF current_user <> '{executor.strip(chr(34))}'
               OR NOT {SCHEMA}.source_erasure_scope_allowed(
                    OLD.workspace_id, OLD.environment_id, OLD.source_object_id
               ) THEN
                RAISE EXCEPTION 'Email sync source disposition is not authorized.'
                    USING ERRCODE = '42501';
            END IF;
            IF OLD.source_kind <> 'email_message' OR OLD.external_namespace <> 'gmail' THEN
                RETURN OLD;
            END IF;
            IF OLD.external_object_id IS NULL OR OLD.connection_id IS NULL THEN
                RAISE EXCEPTION 'Gmail source identity is incomplete.'
                    USING ERRCODE = '23514';
            END IF;
            message_digest := encode(sha256(convert_to(OLD.external_object_id, 'UTF8')), 'hex');

            FOR page_row IN
                SELECT p.*
                  FROM {SCHEMA}.email_sync_pages p
                 WHERE p.workspace_id = OLD.workspace_id
                   AND p.environment_id = OLD.environment_id
                   AND EXISTS (
                       SELECT 1
                         FROM jsonb_array_elements(p.record->'effects') e
                        WHERE (e->>'kind' = 'content' AND EXISTS (
                                  SELECT 1 FROM {SCHEMA}.source_erasure_execution_contexts c
                                   WHERE c.transaction_id = pg_current_xact_id()
                                     AND c.workspace_id = OLD.workspace_id
                                     AND c.environment_id = OLD.environment_id
                                     AND c.source_object_id = OLD.source_object_id
                                     AND c.target_table = 'canonical_acceptances'
                                     AND c.target_record->>'canonical_acceptance_id' = e->>'effect_reference'
                              ))
                           OR (e->>'kind' = 'state'
                               AND e->'state_observation'->>'subject_reference_digest' = message_digest)
                   )
            LOOP
                IF jsonb_typeof(page_row.record) <> 'object'
                   OR jsonb_typeof(page_row.record->'effects') <> 'array'
                   OR page_row.record ? 'unclassified_lineage' THEN
                    RAISE EXCEPTION 'Email sync page lineage is unclassified.' USING ERRCODE = '23514';
                END IF;
                IF NOT page_row.committed OR NOT EXISTS (
                    SELECT 1 FROM {SCHEMA}.email_sync_attempts a
                     WHERE a.workspace_id = page_row.workspace_id
                       AND a.environment_id = page_row.environment_id
                       AND a.attempt_id = page_row.attempt_id
                       AND a.committed_checkpoint IS NOT DISTINCT FROM
                           page_row.record->>'next_checkpoint'
                ) THEN
                    RAISE EXCEPTION 'Email sync page is not safely checkpointed for disposition.'
                        USING ERRCODE = '23514';
                END IF;
                old_page_id := page_row.page_id;
                kept_effects := '[]'::jsonb;
                kept_receipts := '[]'::jsonb;
                removed_keys := ARRAY[]::text[];
                FOR effect IN SELECT value FROM jsonb_array_elements(page_row.record->'effects')
                LOOP
                    IF (effect->>'kind' = 'content' AND EXISTS (
                            SELECT 1 FROM {SCHEMA}.source_erasure_execution_contexts c
                             WHERE c.transaction_id = pg_current_xact_id()
                               AND c.workspace_id = OLD.workspace_id
                               AND c.environment_id = OLD.environment_id
                               AND c.source_object_id = OLD.source_object_id
                               AND c.target_table = 'canonical_acceptances'
                               AND c.target_record->>'canonical_acceptance_id' = effect->>'effect_reference'
                        ))
                       OR (effect->>'kind' = 'state'
                           AND effect->'state_observation'->>'subject_reference_digest' = message_digest) THEN
                        removed_keys := array_append(removed_keys, effect->>'effect_key');
                    ELSE
                        kept_effects := kept_effects || jsonb_build_array(effect);
                    END IF;
                END LOOP;
                IF coalesce(array_length(removed_keys, 1), 0) = 0 THEN
                    CONTINUE;
                END IF;

                IF page_row.record ? 'lifecycle_receipts'
                   AND jsonb_array_length(page_row.record->'lifecycle_receipts') > 0 THEN
                    RAISE EXCEPTION 'Email sync lifecycle lineage requires separate classification.'
                        USING ERRCODE = '23514';
                END IF;
                new_page_id := 'erased.' || gen_random_uuid()::text;
                FOR receipt_row IN
                    SELECT r.* FROM {SCHEMA}.email_sync_effect_receipts r
                     WHERE r.workspace_id = page_row.workspace_id
                       AND r.environment_id = page_row.environment_id
                       AND r.attempt_id = page_row.attempt_id
                       AND r.page_id = old_page_id
                LOOP
                    IF NOT receipt_row.effect_key = ANY(removed_keys) THEN
                        kept_receipts := kept_receipts || jsonb_build_array(jsonb_build_object(
                            'effect_key', receipt_row.effect_key,
                            'receipt_reference', receipt_row.receipt_reference,
                            'durable', receipt_row.durable
                        ));
                    END IF;
                END LOOP;
                DELETE FROM {SCHEMA}.email_sync_effect_receipts
                 WHERE workspace_id = page_row.workspace_id
                   AND environment_id = page_row.environment_id
                   AND attempt_id = page_row.attempt_id
                   AND page_id = old_page_id;

                new_record := jsonb_set(page_row.record, '{{effects}}', kept_effects, false);
                new_record := jsonb_set(new_record, '{{page_id}}', to_jsonb(new_page_id), true);
                new_record := jsonb_set(new_record, '{{page_digest}}',
                    to_jsonb(encode(sha256(convert_to(new_record::text, 'UTF8')), 'hex')), true);
                UPDATE {SCHEMA}.email_sync_pages
                   SET page_id = new_page_id,
                       page_digest = new_record->>'page_digest',
                       record = new_record
                 WHERE workspace_id = page_row.workspace_id
                   AND environment_id = page_row.environment_id
                   AND attempt_id = page_row.attempt_id
                   AND page_id = old_page_id;
                FOR receipt_row IN SELECT value FROM jsonb_array_elements(kept_receipts)
                LOOP
                    INSERT INTO {SCHEMA}.email_sync_effect_receipts
                        (workspace_id, environment_id, attempt_id, page_id,
                         effect_key, receipt_reference, durable)
                    VALUES (page_row.workspace_id, page_row.environment_id,
                            page_row.attempt_id, new_page_id,
                            receipt_row.value->>'effect_key',
                            receipt_row.value->>'receipt_reference',
                            (receipt_row.value->>'durable')::boolean);
                END LOOP;

                SELECT * INTO attempt_row FROM {SCHEMA}.email_sync_attempts a
                 WHERE a.workspace_id = page_row.workspace_id
                   AND a.environment_id = page_row.environment_id
                   AND a.attempt_id = page_row.attempt_id
                 FOR UPDATE;
                IF FOUND THEN
                    UPDATE {SCHEMA}.email_sync_attempts
                       SET revision = revision + 1,
                           record = jsonb_set(record, '{{revision}}', to_jsonb(revision + 1), true),
                           updated_at = statement_timestamp()
                     WHERE workspace_id = attempt_row.workspace_id
                       AND environment_id = attempt_row.environment_id
                       AND attempt_id = attempt_row.attempt_id;
                END IF;
            END LOOP;

            FOR observation_row IN
                SELECT o.* FROM {SCHEMA}.email_sync_state_observations o
                 WHERE o.workspace_id = OLD.workspace_id
                   AND o.environment_id = OLD.environment_id
                   AND o.connection_id = OLD.connection_id
                   AND o.record->>'subject_reference_digest' = message_digest
                 FOR UPDATE
            LOOP
                SELECT i.* INTO intent_row FROM {SCHEMA}.outbox_intents i
                 WHERE i.event_id = observation_row.outbox_event_id
                   AND i.workspace_id = OLD.workspace_id
                   AND i.environment_id = OLD.environment_id
                 FOR UPDATE;
                IF FOUND THEN
                    INSERT INTO {SCHEMA}.source_erasure_execution_contexts
                        (transaction_id, workspace_id, environment_id, operation_id,
                         source_object_id, target_table, target_record)
                    SELECT pg_current_xact_id(), c.workspace_id, c.environment_id,
                           c.operation_id, OLD.source_object_id, 'outbox_intents', to_jsonb(intent_row)
                      FROM {SCHEMA}.source_erasure_execution_contexts c
                     WHERE c.transaction_id = pg_current_xact_id()
                       AND c.workspace_id = OLD.workspace_id
                       AND c.environment_id = OLD.environment_id
                       AND c.source_object_id = OLD.source_object_id
                       AND c.target_table = '__scope__';
                    DELETE FROM {SCHEMA}.outbox_deliveries WHERE event_id = intent_row.event_id;
                END IF;
                DELETE FROM {SCHEMA}.email_sync_state_observations
                 WHERE workspace_id = observation_row.workspace_id
                   AND environment_id = observation_row.environment_id
                   AND observation_id = observation_row.observation_id;
                IF FOUND AND intent_row.event_id IS NOT NULL THEN
                    DELETE FROM {SCHEMA}.outbox_intents WHERE event_id = intent_row.event_id;
                    DELETE FROM {SCHEMA}.source_erasure_execution_contexts
                     WHERE transaction_id = pg_current_xact_id()
                       AND target_table = 'outbox_intents'
                       AND target_record = to_jsonb(intent_row);
                END IF;
                intent_row := NULL;
            END LOOP;
            RETURN OLD;
        END
        $function$
        """
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.erase_email_sync_source_lineage() FROM PUBLIC"
    )
    op.execute(
        f"CREATE TRIGGER trg_source_objects_email_sync_erasure AFTER DELETE ON {SCHEMA}.source_objects "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.erase_email_sync_source_lineage()"
    )


def downgrade() -> None:
    raise RuntimeError("Gmail email-sync lineage erasure migration is irreversible")
