"""Harden the Issue #86 ledger after its initial release."""

from __future__ import annotations

from alembic import op


revision = "20261013_16"
down_revision = "20261013_15"
branch_labels = None
depends_on = None

SCHEMA = "spine"


def upgrade() -> None:
    """Align catalog names and verify the original Issue #6 receipt binding."""
    constraint_renames = (
        (
            "ingestion_run_sources",
            "ordinal_positive",
            "ck_ingestion_run_sources_ordinal_positive",
        ),
        (
            "ingestion_run_source_bindings",
            "receipt_complete",
            "ck_ingestion_run_source_bindings_receipt_complete",
        ),
        (
            "ingestion_run_source_bindings",
            "digest",
            "ck_ingestion_run_source_bindings_digest",
        ),
        (
            "ingestion_transitions",
            "attempt_positive",
            "ck_ingestion_transitions_attempt_positive",
        ),
        (
            "processing_receipts",
            "complete_exclusions",
            "ck_processing_receipts_complete_exclusions",
        ),
    )
    for table, old_name, new_name in constraint_renames:
        op.execute(
            f"""
            DO $block$
            BEGIN
                IF EXISTS (
                    SELECT 1
                      FROM pg_catalog.pg_constraint AS constraint_record
                      JOIN pg_catalog.pg_class AS relation
                        ON relation.oid = constraint_record.conrelid
                      JOIN pg_catalog.pg_namespace AS namespace
                        ON namespace.oid = relation.relnamespace
                     WHERE namespace.nspname = '{SCHEMA}'
                       AND relation.relname = '{table}'
                       AND constraint_record.conname = '{old_name}'
                ) THEN
                    EXECUTE 'ALTER TABLE {SCHEMA}."{table}" '
                        || 'RENAME CONSTRAINT "{old_name}" TO "{new_name}"';
                END IF;
            END;
            $block$
            """
        )

    op.create_foreign_key(
        "fk_ingestion_run_source_bindings_original_receipt",
        "ingestion_run_source_bindings",
        "idempotency_receipts",
        ["original_receipt_id"],
        ["receipt_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )
    op.execute(
        f"""
        ALTER TABLE {SCHEMA}."processing_receipts"
        ADD CONSTRAINT ck_processing_receipts_complete_failures
        CHECK (coverage <> 'complete' OR failure_ids = '[]'::jsonb)
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.validate_issue86_binding_original_receipt()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY INVOKER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE revision_kind text;
        BEGIN
            SELECT revision.kind
              INTO revision_kind
              FROM {SCHEMA}.source_revisions AS revision
             WHERE revision.source_revision_id = NEW.source_revision_id
               AND revision.workspace_id = NEW.workspace_id
               AND revision.environment_id = NEW.environment_id
             FOR KEY SHARE;

            IF NOT FOUND THEN
                RETURN NEW;
            END IF;

            IF revision_kind = 'content' THEN
                RAISE EXCEPTION 'Original receipt verification unavailable.'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_ingestion_binding_original_receipt_scope';
            END IF;

            IF NEW.original_receipt_id IS NULL THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'Contentless ingestion binding cannot include an original receipt.'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'ck_ingestion_binding_original_receipt_scope';
        END;
        $function$
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.validate_issue86_binding_original_digest()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY INVOKER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        BEGIN
            IF NEW.original_receipt_id IS NOT NULL
               AND NEW.original_receipt_digest IS DISTINCT FROM NEW.original_reference->>'digest_hex'
            THEN
                RAISE EXCEPTION 'Original receipt digest conflict.'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_ingestion_binding_original_receipt_digest';
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(
        f"""
        CREATE CONSTRAINT TRIGGER trg_ingestion_run_source_bindings_original_receipt_tenant
        AFTER INSERT OR UPDATE ON {SCHEMA}."ingestion_run_source_bindings"
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION {SCHEMA}.validate_issue86_binding_original_receipt()
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_ingestion_run_source_bindings_original_digest
        BEFORE INSERT OR UPDATE ON {SCHEMA}."ingestion_run_source_bindings"
        FOR EACH ROW
        EXECUTE FUNCTION {SCHEMA}.validate_issue86_binding_original_digest()
        """
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.validate_issue86_binding_original_receipt() FROM PUBLIC"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.validate_issue86_binding_original_digest() FROM PUBLIC"
    )
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.validate_issue86_safe_metrics()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY INVOKER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE key_name text;
        BEGIN
            IF NEW.safe_metrics IS NULL THEN
                RETURN NEW;
            END IF;
            IF jsonb_typeof(NEW.safe_metrics) <> 'object'
               OR NEW.safe_metrics->>'schema_version' <> 'ingestion-safe-detail:v1'
            THEN
                RAISE EXCEPTION 'Safe metrics schema is invalid.'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_ingestion_transitions_safe_metrics_schema';
            END IF;
            FOR key_name IN SELECT jsonb_object_keys(NEW.safe_metrics) LOOP
                IF key_name NOT IN ('schema_version', 'attempt', 'item_count', 'byte_count', 'duration_ms', 'retryable', 'opaque_detail_ref') THEN
                    RAISE EXCEPTION 'Safe metrics field is not allowlisted.'
                        USING ERRCODE = '23514',
                              CONSTRAINT = 'ck_ingestion_transitions_safe_metrics_allowlist';
                END IF;
            END LOOP;
            IF NEW.safe_metrics ? 'attempt' AND (
                jsonb_typeof(NEW.safe_metrics->'attempt') <> 'number'
                OR NEW.safe_metrics->>'attempt' !~ '^[1-9][0-9]{{0,9}}$'
            ) THEN
                RAISE EXCEPTION 'Safe metrics attempt is invalid.'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_ingestion_transitions_safe_metrics_counter';
            END IF;
            IF (NEW.safe_metrics ? 'item_count' OR NEW.safe_metrics ? 'byte_count' OR NEW.safe_metrics ? 'duration_ms') AND (
                (NEW.safe_metrics ? 'item_count' AND (jsonb_typeof(NEW.safe_metrics->'item_count') <> 'number' OR NEW.safe_metrics->>'item_count' !~ '^[0-9]{{1,10}}$'))
                OR (NEW.safe_metrics ? 'byte_count' AND (jsonb_typeof(NEW.safe_metrics->'byte_count') <> 'number' OR NEW.safe_metrics->>'byte_count' !~ '^[0-9]{{1,10}}$'))
                OR (NEW.safe_metrics ? 'duration_ms' AND (jsonb_typeof(NEW.safe_metrics->'duration_ms') <> 'number' OR NEW.safe_metrics->>'duration_ms' !~ '^[0-9]{{1,10}}$'))
            ) THEN
                RAISE EXCEPTION 'Safe metrics counter is invalid.'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_ingestion_transitions_safe_metrics_counter';
            END IF;
            IF NEW.safe_metrics ? 'retryable' AND jsonb_typeof(NEW.safe_metrics->'retryable') <> 'boolean' THEN
                RAISE EXCEPTION 'Safe metrics boolean is invalid.'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_ingestion_transitions_safe_metrics_boolean';
            END IF;
            IF NEW.safe_metrics ? 'opaque_detail_ref' AND (
                jsonb_typeof(NEW.safe_metrics->'opaque_detail_ref') <> 'string'
                OR NEW.safe_metrics->>'opaque_detail_ref' !~ '^[0-9a-fA-F]{{8}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{12}}$'
                OR NEW.safe_metrics->>'opaque_detail_ref' = '00000000-0000-0000-0000-000000000000'
            ) THEN
                RAISE EXCEPTION 'Safe metrics reference is invalid.'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_ingestion_transitions_safe_metrics_reference';
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(
        f"CREATE TRIGGER trg_ingestion_transitions_safe_metrics "
        f"BEFORE INSERT OR UPDATE ON {SCHEMA}.\"ingestion_transitions\" "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.validate_issue86_safe_metrics()"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.validate_issue86_safe_metrics() FROM PUBLIC"
    )


def downgrade() -> None:
    """Restore the original Issue #86 catalog names and trigger set."""
    op.execute(
        f'DROP TRIGGER trg_ingestion_run_source_bindings_original_digest '
        f'ON {SCHEMA}."ingestion_run_source_bindings"'
    )
    op.execute(
        f'DROP TRIGGER trg_ingestion_transitions_safe_metrics '
        f'ON {SCHEMA}."ingestion_transitions"'
    )
    op.execute(f"DROP FUNCTION {SCHEMA}.validate_issue86_safe_metrics()")
    op.execute(
        f'DROP TRIGGER trg_ingestion_run_source_bindings_original_receipt_tenant '
        f'ON {SCHEMA}."ingestion_run_source_bindings"'
    )
    op.execute(f"DROP FUNCTION {SCHEMA}.validate_issue86_binding_original_digest()")
    op.execute(f"DROP FUNCTION {SCHEMA}.validate_issue86_binding_original_receipt()")
    op.drop_constraint(
        "fk_ingestion_run_source_bindings_original_receipt",
        "ingestion_run_source_bindings",
        schema=SCHEMA,
        type_="foreignkey",
    )
    op.execute(
        f'ALTER TABLE {SCHEMA}."processing_receipts" '
        'DROP CONSTRAINT "ck_processing_receipts_complete_failures"'
    )
    constraint_renames = (
        (
            "ingestion_run_sources",
            "ck_ingestion_run_sources_ordinal_positive",
            "ordinal_positive",
        ),
        (
            "ingestion_run_source_bindings",
            "ck_ingestion_run_source_bindings_receipt_complete",
            "receipt_complete",
        ),
        (
            "ingestion_run_source_bindings",
            "ck_ingestion_run_source_bindings_digest",
            "digest",
        ),
        (
            "ingestion_transitions",
            "ck_ingestion_transitions_attempt_positive",
            "attempt_positive",
        ),
        (
            "processing_receipts",
            "ck_processing_receipts_complete_exclusions",
            "complete_exclusions",
        ),
    )
    for table, old_name, new_name in constraint_renames:
        op.execute(
            f'ALTER TABLE {SCHEMA}."{table}" '
            f'RENAME CONSTRAINT "{old_name}" TO "{new_name}"'
        )
