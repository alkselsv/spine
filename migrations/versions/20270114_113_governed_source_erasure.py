"""Add a transaction-bound, operation-scoped canonical erasure entrypoint."""

from alembic import op

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20270114_113"
down_revision = "20270113_112"
branch_labels = None
depends_on = None

SCHEMA = "spine"
ERASURE_TABLES = (
    "current_tombstone_authorization_decisions",
    "tombstone_authorization_decisions",
    "qualified_deletion_evidence",
    "current_trusted_canonical_tombstone_selectors",
    "trusted_canonical_tombstone_selector_versions",
    "tombstone_authorizations",
    "admission_decisions",
    "access_policy_grants",
    "policy_proposals",
    "policy_approvals",
    "policy_activations",
    "current_access_policies",
    "access_policy_versions",
    "current_lifecycle_authorities",
    "lifecycle_decisions",
    "lifecycle_observations",
    "current_retention_applicabilities",
    "retention_applicabilities",
    "gmail_message_label_heads",
    "gmail_message_label_observations",
    "gmail_message_relationship_evidence",
    "source_objects",
    "source_revisions",
    "source_revision_provenance",
    "source_revision_receipt_associations",
    "canonical_acceptances",
    "current_source_heads",
)
AUTHORITY_TABLES = (
    "source_disposition_operations",
    "current_source_heads",
    "tombstone_authorizations",
    "current_tombstone_authorization_decisions",
    "tombstone_authorization_decisions",
    "current_source_deletion_policy_approvals",
    "source_deletion_policy_approvals",
    "current_tombstone_authorization_issuers",
    "tombstone_authorization_issuers",
    "current_retention_applicabilities",
    "retention_applicabilities",
    "current_lifecycle_authorities",
    "lifecycle_decisions",
    "source_objects",
)
IMMUTABLE_ERASURE_TABLES = (
    "gmail_message_label_observations",
    "gmail_message_relationship_evidence",
    "tombstone_authorization_decisions",
    "qualified_deletion_evidence",
    "trusted_canonical_tombstone_selector_versions",
    "tombstone_authorizations",
    "admission_decisions",
    "access_policy_grants",
    "policy_proposals",
    "policy_approvals",
    "policy_activations",
    "access_policy_versions",
    "lifecycle_decisions",
    "lifecycle_observations",
    "retention_applicabilities",
    "source_objects",
    "source_revisions",
    "source_revision_provenance",
    "source_revision_receipt_associations",
    "canonical_acceptances",
)


def _role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, attribute))


def upgrade() -> None:
    executor = _role("erasure_role")
    runtime = _role("runtime_role")
    op.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {executor}")
    op.execute(
        f"ALTER TABLE {SCHEMA}.source_disposition_operations "
        "ADD COLUMN canonical_erasure_state text NOT NULL DEFAULT 'not_started', "
        "ADD COLUMN canonical_erasure_completed_at timestamptz"
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.source_disposition_operations "
        "ADD CONSTRAINT ck_source_disposition_canonical_erasure_state "
        "CHECK ((canonical_erasure_state = 'not_started' AND "
        "canonical_erasure_completed_at IS NULL) OR "
        "(canonical_erasure_state = 'applied' AND "
        "canonical_erasure_completed_at IS NOT NULL))"
    )

    # The v3 tombstone acceptance and its authorizing decision form an exact,
    # tenant-scoped cycle. Defer both checks to transaction end so the governed
    # erasure can remove the pair atomically while preserving FK integrity.
    op.execute(
        f"ALTER TABLE {SCHEMA}.tombstone_authorizations ALTER CONSTRAINT "
        "fk_tombstone_authorizations_expected_acceptance "
        "DEFERRABLE INITIALLY DEFERRED"
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.canonical_acceptances ALTER CONSTRAINT "
        "fk_canonical_acceptances_tombstone_authorization "
        "DEFERRABLE INITIALLY DEFERRED"
    )

    op.execute(
        f"""
        CREATE TABLE {SCHEMA}.source_erasure_execution_contexts (
            transaction_id xid8 NOT NULL,
            workspace_id uuid NOT NULL,
            environment_id uuid NOT NULL,
            operation_id uuid NOT NULL,
            source_object_id uuid NOT NULL,
            target_table text NOT NULL,
            target_record jsonb NOT NULL,
            PRIMARY KEY (transaction_id, operation_id, target_table, target_record)
        )
        """
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.source_erasure_execution_contexts "
        "ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.source_erasure_execution_contexts "
        "FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        f"CREATE POLICY pol_source_erasure_context_executor ON "
        f"{SCHEMA}.source_erasure_execution_contexts FOR ALL TO {executor} "
        "USING (true) WITH CHECK (true)"
    )
    op.execute(
        f"GRANT SELECT, INSERT, DELETE ON {SCHEMA}.source_erasure_execution_contexts "
        f"TO {executor}"
    )
    op.execute(
        f"REVOKE ALL ON {SCHEMA}.source_erasure_execution_contexts FROM PUBLIC, {runtime}"
    )

    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.source_erasure_scope_allowed(
            p_workspace_id uuid, p_environment_id uuid, p_source_object_id uuid
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
                       AND c.source_object_id = p_source_object_id
                       AND c.target_table = '__scope__'
                )
        $function$
        """
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.source_erasure_scope_allowed(uuid, uuid, uuid) "
        "FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {SCHEMA}.source_erasure_scope_allowed(uuid, uuid, uuid) "
        f"TO {executor}"
    )

    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.allow_governed_source_delete() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        BEGIN
            IF TG_OP <> 'DELETE'
               OR current_user <> '{executor.strip(chr(34))}'
               OR NOT EXISTS (
                    SELECT 1 FROM {SCHEMA}.source_erasure_execution_contexts c
                     WHERE c.transaction_id = pg_current_xact_id()
                       AND c.workspace_id = OLD.workspace_id
                       AND c.environment_id = OLD.environment_id
                       AND c.target_table = TG_TABLE_NAME
                       AND c.target_record = to_jsonb(OLD)
                ) THEN
                RAISE EXCEPTION 'Canonical source rows are immutable.'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_governed_source_erasure_only';
            END IF;
            RETURN OLD;
        END
        $function$
        """
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.allow_governed_source_delete() FROM PUBLIC"
    )
    for table in IMMUTABLE_ERASURE_TABLES:
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {SCHEMA}.{table}"
        )
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON "
            f"{SCHEMA}.{table} FOR EACH ROW EXECUTE FUNCTION "
            f"{SCHEMA}.allow_governed_source_delete()"
        )
    op.execute(
        f"DROP TRIGGER trg_canonical_tombstone_selectors_immutable ON "
        f"{SCHEMA}.trusted_canonical_tombstone_selector_versions"
    )

    for table in AUTHORITY_TABLES:
        op.execute(
            f"CREATE POLICY pol_{table}_erasure_executor ON {SCHEMA}.{table} "
            f"FOR ALL TO {executor} USING (true) WITH CHECK (true)"
        )
        grants = "SELECT, UPDATE" if table in {
            "source_disposition_operations",
            "current_source_heads",
            "tombstone_authorizations",
            "current_tombstone_authorization_decisions",
            "current_source_deletion_policy_approvals",
            "current_tombstone_authorization_issuers",
            "current_retention_applicabilities",
            "current_lifecycle_authorities",
            "source_objects",
        } else "SELECT"
        op.execute(f"GRANT {grants} ON {SCHEMA}.{table} TO {executor}")
    for table in ERASURE_TABLES:
        if table not in AUTHORITY_TABLES:
            if table == "current_trusted_canonical_tombstone_selectors":
                op.execute(
                    f"CREATE POLICY pol_{table}_erasure_executor ON {SCHEMA}.{table} "
                    f"FOR ALL TO {executor} USING (EXISTS (SELECT 1 FROM "
                    f"{SCHEMA}.trusted_canonical_tombstone_selector_versions v "
                    f"WHERE v.workspace_id = {table}.workspace_id AND "
                    f"v.environment_id = {table}.environment_id AND "
                    f"v.selector_series_id = {table}.selector_series_id AND "
                    f"{SCHEMA}.source_erasure_scope_allowed(v.workspace_id, "
                    "v.environment_id, v.source_object_id))) WITH CHECK (true)"
                )
            else:
                op.execute(
                    f"CREATE POLICY pol_{table}_erasure_executor ON {SCHEMA}.{table} "
                    f"FOR ALL TO {executor} USING "
                    f"({SCHEMA}.source_erasure_scope_allowed(workspace_id, environment_id, source_object_id)) "
                    f"WITH CHECK "
                    f"({SCHEMA}.source_erasure_scope_allowed(workspace_id, environment_id, source_object_id))"
                )
        op.execute(f"GRANT SELECT, DELETE ON {SCHEMA}.{table} TO {executor}")
        op.execute(f"REVOKE DELETE ON {SCHEMA}.{table} FROM {runtime}")

    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.execute_source_disposition_erasure(
            p_workspace_id uuid,
            p_environment_id uuid,
            p_operation_id uuid,
            p_expected_state_version integer
        ) RETURNS text
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE
            op_row record;
            binding jsonb;
            source_id uuid;
            expected_revision uuid;
            expected_acceptance uuid;
            expected_generation integer;
            auth_id uuid;
            auth_digest text;
            auth_record jsonb;
            current_head record;
            decision_disposition text;
            lifecycle_status text;
            operation_xid xid8 := pg_current_xact_id();
            target record;
            target_table text;
        BEGIN
            SELECT * INTO op_row
              FROM {SCHEMA}.source_disposition_operations
             WHERE workspace_id = p_workspace_id
               AND environment_id = p_environment_id
               AND operation_id = p_operation_id;
            IF NOT FOUND THEN
                RETURN 'DENIED';
            END IF;
            binding := op_row.operation_data->'authority_binding';
            IF jsonb_typeof(binding) <> 'object'
               OR (binding->>'workspace_id')::uuid <> p_workspace_id
               OR (binding->>'environment_id')::uuid <> p_environment_id
               OR (binding->>'operation_id')::uuid <> p_operation_id THEN
                RETURN 'DENIED';
            END IF;
            source_id := (binding->>'source_object_id')::uuid;

            PERFORM pg_advisory_xact_lock(hashtextextended(
                p_workspace_id::text || ':' || p_environment_id::text || ':' || source_id::text,
                0
            ));

            SELECT * INTO op_row
              FROM {SCHEMA}.source_disposition_operations
             WHERE workspace_id = p_workspace_id
               AND environment_id = p_environment_id
               AND operation_id = p_operation_id
             FOR UPDATE;
            IF NOT FOUND OR op_row.state_version <> p_expected_state_version
               OR op_row.status NOT IN ('deletion_pending', 'incomplete')
               OR NOT op_row.fence_active THEN
                RETURN 'DENIED';
            END IF;
            IF op_row.canonical_erasure_state = 'applied' THEN
                RETURN 'CANONICAL_ERASED';
            END IF;

            binding := op_row.operation_data->'authority_binding';
            IF jsonb_typeof(binding) <> 'object'
               OR (binding->>'workspace_id')::uuid <> p_workspace_id
               OR (binding->>'environment_id')::uuid <> p_environment_id
               OR (binding->>'operation_id')::uuid <> p_operation_id
               OR (binding->>'source_object_id')::uuid <> source_id THEN
                RETURN 'DENIED';
            END IF;
            source_id := (binding->>'source_object_id')::uuid;
            expected_revision := (binding->>'expected_revision_id')::uuid;
            expected_acceptance := (binding->>'expected_acceptance_id')::uuid;
            expected_generation := (binding->>'expected_generation')::integer;
            auth_id := (binding->>'tombstone_authorization_id')::uuid;
            auth_digest := binding->>'authorization_digest';

            SELECT * INTO current_head FROM {SCHEMA}.current_source_heads
             WHERE workspace_id = p_workspace_id
               AND environment_id = p_environment_id
               AND source_object_id = source_id
             FOR UPDATE;
            IF NOT FOUND OR current_head.current_revision_id <> expected_revision
               OR current_head.acceptance_id <> expected_acceptance
               OR current_head.cas_version <> expected_generation THEN
                RETURN 'DENIED';
            END IF;

            SELECT record INTO auth_record FROM {SCHEMA}.tombstone_authorizations
             WHERE workspace_id = p_workspace_id
               AND environment_id = p_environment_id
               AND authorization_id = auth_id
               AND source_object_id = source_id
               AND command_digest = auth_digest
             FOR UPDATE;
            IF auth_record IS NULL OR (auth_record->>'expires_at')::timestamptz <= statement_timestamp()
               OR (auth_record->>'expected_current_revision_id')::uuid <> expected_revision
               OR (auth_record->>'expected_current_acceptance_id')::uuid <> expected_acceptance
               OR (auth_record->>'expected_current_cas_version')::integer <> expected_generation THEN
                RETURN 'DENIED';
            END IF;

            SELECT d.disposition INTO decision_disposition
              FROM {SCHEMA}.current_tombstone_authorization_decisions c
              JOIN {SCHEMA}.tombstone_authorization_decisions d
                ON d.workspace_id = c.workspace_id
               AND d.environment_id = c.environment_id
               AND d.authorization_id = c.authorization_id
               AND d.decision_id = c.current_decision_id
               AND d.generation = c.generation
             WHERE c.workspace_id = p_workspace_id
               AND c.environment_id = p_environment_id
               AND c.authorization_id = auth_id
             FOR UPDATE OF c;
            IF decision_disposition IS DISTINCT FROM 'issued' THEN
                RETURN 'DENIED';
            END IF;

            PERFORM 1 FROM {SCHEMA}.current_source_deletion_policy_approvals p
             WHERE p.workspace_id = p_workspace_id
               AND p.environment_id = p_environment_id
               AND p.current_approval_id = (binding->>'deletion_policy_approval_id')::uuid
               AND p.generation = (binding->>'deletion_policy_generation')::integer
             FOR UPDATE;
            IF NOT FOUND THEN
                RETURN 'DENIED';
            END IF;

            PERFORM 1 FROM {SCHEMA}.current_tombstone_authorization_issuers i
             WHERE i.workspace_id = p_workspace_id
               AND i.environment_id = p_environment_id
               AND i.current_issuer_authority_id = (binding->>'issuer_authority_id')::uuid
               AND i.generation = (binding->>'issuer_generation')::integer
             FOR UPDATE;
            IF NOT FOUND THEN
                RETURN 'DENIED';
            END IF;

            PERFORM 1 FROM {SCHEMA}.current_retention_applicabilities r
             WHERE r.workspace_id = p_workspace_id
               AND r.environment_id = p_environment_id
               AND r.current_applicability_id = (binding->>'retention_eligibility_reference')::uuid
               AND r.generation = (binding->>'retention_eligibility_generation')::integer
               AND r.target_kind = 'exact_source'
               AND r.target_reference = source_id
             FOR UPDATE;
            IF NOT FOUND THEN
                RETURN 'DENIED';
            END IF;

            SELECT d.record->>'status' INTO lifecycle_status
              FROM {SCHEMA}.current_lifecycle_authorities l
              JOIN {SCHEMA}.lifecycle_decisions d
                ON d.workspace_id = l.workspace_id
               AND d.environment_id = l.environment_id
               AND d.source_object_id = l.source_object_id
               AND d.decision_id = l.current_decision_id
               AND d.generation = l.generation
             WHERE l.workspace_id = p_workspace_id
               AND l.environment_id = p_environment_id
               AND l.source_object_id = source_id
               AND l.current_decision_id = (auth_record->>'legal_hold_check_reference')::uuid
               AND l.generation = (auth_record->>'legal_hold_generation')::integer
               AND coalesce((d.record->>'legal_hold_active')::boolean, true) = false
             FOR UPDATE OF l;
            IF lifecycle_status IS DISTINCT FROM 'eligible' THEN
                RETURN 'DENIED';
            END IF;

            PERFORM 1 FROM {SCHEMA}.source_objects s
             WHERE s.workspace_id = p_workspace_id
               AND s.environment_id = p_environment_id
               AND s.source_object_id = source_id
               AND s.connection_id = (auth_record->>'connection_id')::uuid
               AND s.external_namespace = auth_record->>'object_namespace'
               AND s.external_object_id = auth_record->>'object_identity'
             FOR UPDATE;
            IF NOT FOUND THEN
                RETURN 'DENIED';
            END IF;

            INSERT INTO {SCHEMA}.source_erasure_execution_contexts
                (transaction_id, workspace_id, environment_id, operation_id,
                 source_object_id, target_table, target_record)
            VALUES (operation_xid, p_workspace_id, p_environment_id, p_operation_id,
                    source_id, '__scope__', '{{}}'::jsonb);

            FOREACH target_table IN ARRAY ARRAY[
                'current_tombstone_authorization_decisions',
                'tombstone_authorization_decisions',
                'current_source_heads',
                'canonical_acceptances',
                'tombstone_authorizations',
                'qualified_deletion_evidence',
                'current_trusted_canonical_tombstone_selectors',
                'trusted_canonical_tombstone_selector_versions',
                'admission_decisions',
                'current_access_policies',
                'policy_activations',
                'policy_approvals',
                'policy_proposals',
                'access_policy_grants',
                'access_policy_versions',
                'current_lifecycle_authorities',
                'lifecycle_decisions',
                'lifecycle_observations',
                'current_retention_applicabilities',
                'retention_applicabilities',
                'gmail_message_label_heads',
                'gmail_message_label_observations',
                'gmail_message_relationship_evidence',
                'source_revision_receipt_associations',
                'source_revision_provenance',
                'source_revisions',
                'source_objects'
            ] LOOP
                IF target_table = 'current_tombstone_authorization_decisions' THEN
                    FOR target IN SELECT to_jsonb(t) AS row_data
                        FROM {SCHEMA}.current_tombstone_authorization_decisions t
                       WHERE t.workspace_id = p_workspace_id
                         AND t.environment_id = p_environment_id
                         AND t.authorization_id = auth_id
                    LOOP
                        INSERT INTO {SCHEMA}.source_erasure_execution_contexts
                            (transaction_id, workspace_id, environment_id, operation_id,
                             source_object_id, target_table, target_record)
                        VALUES (operation_xid, p_workspace_id, p_environment_id, p_operation_id,
                                source_id, target_table, target.row_data);
                    END LOOP;
                    DELETE FROM {SCHEMA}.current_tombstone_authorization_decisions
                     WHERE workspace_id = p_workspace_id
                       AND environment_id = p_environment_id
                       AND authorization_id = auth_id;
                ELSIF target_table = 'current_trusted_canonical_tombstone_selectors' THEN
                    FOR target IN SELECT to_jsonb(c) AS row_data
                        FROM {SCHEMA}.current_trusted_canonical_tombstone_selectors c
                        JOIN {SCHEMA}.trusted_canonical_tombstone_selector_versions v
                          ON v.workspace_id = c.workspace_id
                         AND v.environment_id = c.environment_id
                         AND v.selector_series_id = c.selector_series_id
                         AND v.selector_id = c.current_selector_id
                       WHERE v.workspace_id = p_workspace_id
                         AND v.environment_id = p_environment_id
                         AND v.source_object_id = source_id
                    LOOP
                        INSERT INTO {SCHEMA}.source_erasure_execution_contexts
                            (transaction_id, workspace_id, environment_id, operation_id,
                             source_object_id, target_table, target_record)
                        VALUES (operation_xid, p_workspace_id, p_environment_id, p_operation_id,
                                source_id, target_table, target.row_data);
                    END LOOP;
                    DELETE FROM {SCHEMA}.current_trusted_canonical_tombstone_selectors c
                     USING {SCHEMA}.trusted_canonical_tombstone_selector_versions v
                     WHERE v.workspace_id = c.workspace_id
                       AND v.environment_id = c.environment_id
                       AND v.selector_series_id = c.selector_series_id
                       AND v.selector_id = c.current_selector_id
                       AND v.workspace_id = p_workspace_id
                       AND v.environment_id = p_environment_id
                       AND v.source_object_id = source_id;
                ELSIF target_table = 'current_retention_applicabilities' THEN
                    FOR target IN SELECT to_jsonb(t) AS row_data
                        FROM {SCHEMA}.current_retention_applicabilities t
                       WHERE t.workspace_id = p_workspace_id
                         AND t.environment_id = p_environment_id
                         AND t.target_kind = 'exact_source'
                         AND t.target_reference = source_id
                    LOOP
                        INSERT INTO {SCHEMA}.source_erasure_execution_contexts
                            (transaction_id, workspace_id, environment_id, operation_id,
                             source_object_id, target_table, target_record)
                        VALUES (operation_xid, p_workspace_id, p_environment_id, p_operation_id,
                                source_id, target_table, target.row_data);
                    END LOOP;
                    DELETE FROM {SCHEMA}.current_retention_applicabilities
                     WHERE workspace_id = p_workspace_id
                       AND environment_id = p_environment_id
                       AND target_kind = 'exact_source'
                       AND target_reference = source_id;
                ELSE
                    FOR target IN EXECUTE format(
                        'SELECT to_jsonb(t) AS row_data FROM spine.%I t '
                        'WHERE t.workspace_id = $1 AND t.environment_id = $2 '
                        'AND t.source_object_id = $3', target_table
                    ) USING p_workspace_id, p_environment_id, source_id
                    LOOP
                    INSERT INTO {SCHEMA}.source_erasure_execution_contexts
                        (transaction_id, workspace_id, environment_id, operation_id,
                         source_object_id, target_table, target_record)
                    VALUES (operation_xid, p_workspace_id, p_environment_id, p_operation_id,
                            source_id, target_table, target.row_data);
                    END LOOP;
                    EXECUTE format(
                        'DELETE FROM spine.%I WHERE workspace_id = $1 '
                        'AND environment_id = $2 AND source_object_id = $3', target_table
                    ) USING p_workspace_id, p_environment_id, source_id;
                END IF;
            END LOOP;

            DELETE FROM {SCHEMA}.source_erasure_execution_contexts
             WHERE transaction_id = operation_xid
               AND operation_id = p_operation_id;
            UPDATE {SCHEMA}.source_disposition_operations
               SET canonical_erasure_state = 'applied',
                   canonical_erasure_completed_at = statement_timestamp()
             WHERE workspace_id = p_workspace_id
               AND environment_id = p_environment_id
               AND operation_id = p_operation_id
               AND canonical_erasure_state = 'not_started';
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Canonical erasure operation state changed.'
                    USING ERRCODE = '40001';
            END IF;
            RETURN 'CANONICAL_ERASED';
        END
        $function$
        """
    )
    function = (
        f"{SCHEMA}.execute_source_disposition_erasure(uuid, uuid, uuid, integer)"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC, {runtime}")



def downgrade() -> None:
    raise RuntimeError("governed erasure migration is irreversible")
