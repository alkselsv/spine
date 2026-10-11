"""Persist the Issue #86 ingestion operational ledger."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261013_15"
down_revision = "20261012_14"
branch_labels = None
depends_on = None
SCHEMA = "spine"
_UUID_PATTERN = "^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def _uuid_setting(name: str) -> str:
    setting = f"pg_catalog.current_setting('spine.{name}', true)"
    return f"CASE WHEN {setting} OPERATOR(pg_catalog.~) '{_UUID_PATTERN}' THEN CAST({setting} AS pg_catalog.uuid) ELSE NULL END"


def _tenant(table: str) -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    qualified = f'{SCHEMA}."{table}"'
    predicate = f"workspace_id = ({_uuid_setting('workspace_id')}) AND environment_id = ({_uuid_setting('environment_id')})"
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(f'CREATE POLICY "pol_{table}_tenant_isolation" ON {qualified} FOR ALL TO {runtime} USING ({predicate}) WITH CHECK ({predicate})')
    op.execute(f'CREATE POLICY "pol_{table}_migration_maintenance" ON {qualified} FOR ALL TO {migration} USING (true) WITH CHECK (true)')
    op.execute(f"GRANT SELECT, INSERT ON TABLE {qualified} TO {runtime}")
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON TABLE {qualified} FROM {runtime}")


def _immutable(table: str) -> None:
    function = f"reject_{table}_mutation"
    qualified = f'{SCHEMA}."{table}"'
    op.execute(f"""
        CREATE FUNCTION {SCHEMA}.{function}() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        BEGIN
            RAISE EXCEPTION 'Ingestion operational ledger rows are immutable.'
                USING ERRCODE = '23514', CONSTRAINT = 'ck_{table}_immutable';
        END; $function$
    """)
    op.execute(f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {qualified} FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.{function}()")
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.{function}() FROM PUBLIC")


def _receipt_trigger(table: str) -> None:
    qualified = f'{SCHEMA}."{table}"'
    op.execute(
        f"CREATE CONSTRAINT TRIGGER trg_{table}_receipt_scope "
        f"AFTER INSERT OR UPDATE ON {qualified} "
        f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        f"EXECUTE FUNCTION {SCHEMA}.validate_issue7_receipt_scope()"
    )


def _safe_detail_triggers() -> None:
    op.execute(f"""
        CREATE FUNCTION {SCHEMA}.validate_issue7_safe_detail() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE key_name text;
        BEGIN
            IF NEW.safe_detail_schema <> 'ingestion-safe-detail:v1'
               OR jsonb_typeof(NEW.safe_detail) <> 'object'
               OR NEW.safe_detail->>'schema_version' <> 'ingestion-safe-detail:v1'
            THEN
                RAISE EXCEPTION 'Safe diagnostic schema is invalid.' USING ERRCODE = '23514', CONSTRAINT = 'ck_issue7_safe_detail_schema';
            END IF;
            FOR key_name IN SELECT jsonb_object_keys(NEW.safe_detail) LOOP
                IF key_name NOT IN ('schema_version', 'attempt', 'item_count', 'byte_count', 'duration_ms', 'retryable', 'opaque_detail_ref') THEN
                    RAISE EXCEPTION 'Safe diagnostic field is not allowlisted.' USING ERRCODE = '23514', CONSTRAINT = 'ck_issue7_safe_detail_allowlist';
                END IF;
            END LOOP;
            IF NEW.safe_detail ? 'attempt' AND (
                jsonb_typeof(NEW.safe_detail->'attempt') <> 'number'
                OR NOT (NEW.safe_detail->>'attempt' ~ '^[1-9][0-9]{0,9}$')
            ) THEN
                RAISE EXCEPTION 'Safe diagnostic counter is invalid.' USING ERRCODE = '23514', CONSTRAINT = 'ck_issue7_safe_detail_counter';
            END IF;
            IF (NEW.safe_detail ? 'item_count' OR NEW.safe_detail ? 'byte_count' OR NEW.safe_detail ? 'duration_ms') AND (
                (NEW.safe_detail ? 'item_count' AND (jsonb_typeof(NEW.safe_detail->'item_count') <> 'number' OR NOT (NEW.safe_detail->>'item_count' ~ '^[0-9]{1,10}$')))
                OR (NEW.safe_detail ? 'byte_count' AND (jsonb_typeof(NEW.safe_detail->'byte_count') <> 'number' OR NOT (NEW.safe_detail->>'byte_count' ~ '^[0-9]{1,10}$')))
                OR (NEW.safe_detail ? 'duration_ms' AND (jsonb_typeof(NEW.safe_detail->'duration_ms') <> 'number' OR NOT (NEW.safe_detail->>'duration_ms' ~ '^[0-9]{1,10}$')))
            ) THEN
                RAISE EXCEPTION 'Safe diagnostic counter is invalid.' USING ERRCODE = '23514', CONSTRAINT = 'ck_issue7_safe_detail_counter';
            END IF;
            IF NEW.safe_detail ? 'retryable' AND jsonb_typeof(NEW.safe_detail->'retryable') <> 'boolean' THEN
                RAISE EXCEPTION 'Safe diagnostic boolean is invalid.' USING ERRCODE = '23514', CONSTRAINT = 'ck_issue7_safe_detail_boolean';
            END IF;
            IF NEW.safe_detail ? 'opaque_detail_ref' AND (
                jsonb_typeof(NEW.safe_detail->'opaque_detail_ref') <> 'string'
                OR NEW.safe_detail->>'opaque_detail_ref' !~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
                OR NEW.safe_detail->>'opaque_detail_ref' = '00000000-0000-0000-0000-000000000000'
            ) THEN
                RAISE EXCEPTION 'Safe diagnostic reference is invalid.' USING ERRCODE = '23514', CONSTRAINT = 'ck_issue7_safe_detail_reference';
            END IF;
            RETURN NEW;
        END; $function$
    """)
    for table in ("ingestion_warnings", "ingestion_failures"):
        qualified = f'{SCHEMA}."{table}"'
        op.execute(f"CREATE TRIGGER trg_{table}_safe_detail BEFORE INSERT OR UPDATE ON {qualified} FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.validate_issue7_safe_detail()")
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.validate_issue7_safe_detail() FROM PUBLIC")


def _binding_original_trigger() -> None:
    op.execute(f"""
        CREATE FUNCTION {SCHEMA}.validate_issue7_binding_original() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE revision_kind text;
        DECLARE revision_sha256 text;
        BEGIN
            SELECT revision.kind, revision.original_sha256
              INTO revision_kind, revision_sha256
              FROM {SCHEMA}.source_revisions AS revision
             WHERE revision.workspace_id = NEW.workspace_id
               AND revision.environment_id = NEW.environment_id
               AND revision.source_object_id = NEW.source_object_id
               AND revision.revision_id = NEW.source_revision_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Binding revision is unavailable.'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_ingestion_binding_revision';
            END IF;
            IF revision_kind = 'content' AND (
                NEW.original_receipt_id IS NULL
                OR NEW.original_receipt_kind <> 'issue6.write_receipt'
                OR NEW.original_receipt_digest IS NULL
                OR NEW.original_reference IS NULL
                OR NEW.original_reference->>'digest_hex' IS DISTINCT FROM revision_sha256
            ) THEN
                RAISE EXCEPTION 'Content binding requires finalized original evidence.'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_ingestion_binding_original_evidence';
            END IF;
            IF revision_kind = 'tombstone' AND (
                NEW.original_receipt_id IS NOT NULL
                OR NEW.original_receipt_kind IS NOT NULL
                OR NEW.original_receipt_digest IS NOT NULL
                OR NEW.original_reference IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'Tombstone binding cannot contain original evidence.'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_ingestion_binding_tombstone_evidence';
            END IF;
            RETURN NEW;
        END; $function$
    """)
    op.execute(
        f"CREATE TRIGGER trg_ingestion_run_source_bindings_original "
        f"BEFORE INSERT ON {SCHEMA}.\"ingestion_run_source_bindings\" "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.validate_issue7_binding_original()"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.validate_issue7_binding_original() FROM PUBLIC")


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    digest = sa.String(length=64)
    scope_unique = lambda name, identifier: sa.UniqueConstraint("workspace_id", "environment_id", identifier, name=name)

    op.create_table(
        "ingestion_runs",
        sa.Column("ingestion_run_id", uuid_type, nullable=False),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("environment_id", uuid_type, nullable=False),
        sa.Column("command_identity", sa.Text(), nullable=False),
        sa.Column("command_digest", digest, nullable=False),
        sa.Column("idempotency_receipt_id", uuid_type, nullable=False),
        sa.Column("trigger_kind", sa.Text(), nullable=False),
        sa.Column("trigger_reference", sa.Text()),
        sa.Column("requested_scope_schema", sa.Text(), nullable=False),
        sa.Column("requested_scope_digest", digest, nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("ingestion_run_id", name="pk_ingestion_runs"),
        scope_unique("uq_ingestion_runs_scope_id", "ingestion_run_id"),
        sa.UniqueConstraint("workspace_id", "environment_id", "command_identity", name="uq_ingestion_runs_command"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id"], [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"], name="fk_ingestion_runs_environment"),
        sa.ForeignKeyConstraint(["idempotency_receipt_id"], [f"{SCHEMA}.idempotency_receipts.receipt_id"], name="fk_ingestion_runs_receipt"),
        sa.CheckConstraint("trigger_kind IN ('upload', 'connector', 'reprocess', 'retry')", name="ck_ingestion_runs_trigger_kind"),
        sa.CheckConstraint("requested_scope_schema = 'ingestion-scope:v1'", name="ck_ingestion_runs_scope_schema"),
        sa.CheckConstraint("command_digest OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$' AND requested_scope_digest OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$'", name="ck_ingestion_runs_digests"),
        sa.CheckConstraint("char_length(command_identity) BETWEEN 1 AND 64", name="ck_ingestion_runs_command_identity"),
        schema=SCHEMA,
    )
    op.create_index("ix_ingestion_runs_requested", "ingestion_runs", ["workspace_id", "environment_id", "requested_at"], schema=SCHEMA)

    op.create_table(
        "ingestion_run_sources",
        sa.Column("ingestion_run_source_id", uuid_type, nullable=False),
        sa.Column("ingestion_run_id", uuid_type, nullable=False),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("environment_id", uuid_type, nullable=False),
        sa.Column("input_ordinal", sa.Integer(), nullable=False),
        sa.Column("input_identity", sa.Text(), nullable=False),
        sa.Column("input_digest", digest, nullable=False),
        sa.Column("command_identity", sa.Text(), nullable=False),
        sa.Column("command_digest", digest, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("ingestion_run_source_id", name="pk_ingestion_run_sources"),
        scope_unique("uq_ingestion_run_sources_scope_id", "ingestion_run_source_id"),
        sa.UniqueConstraint("workspace_id", "environment_id", "ingestion_run_id", "input_ordinal", name="uq_ingestion_run_sources_ordinal"),
        sa.UniqueConstraint("workspace_id", "environment_id", "ingestion_run_id", "input_identity", name="uq_ingestion_run_sources_input"),
        sa.UniqueConstraint("workspace_id", "environment_id", "command_identity", name="uq_ingestion_run_sources_command"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "ingestion_run_id"], [f"{SCHEMA}.ingestion_runs.workspace_id", f"{SCHEMA}.ingestion_runs.environment_id", f"{SCHEMA}.ingestion_runs.ingestion_run_id"], name="fk_ingestion_run_sources_run"),
        sa.CheckConstraint("input_ordinal > 0", name="ordinal_positive"),
        sa.CheckConstraint("input_digest OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$' AND command_digest OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$'", name="ck_ingestion_run_sources_digests"),
        schema=SCHEMA,
    )
    op.create_index("ix_ingestion_run_sources_run", "ingestion_run_sources", ["workspace_id", "environment_id", "ingestion_run_id", "input_ordinal"], schema=SCHEMA)

    op.create_table(
        "ingestion_run_source_bindings",
        sa.Column("binding_id", uuid_type, nullable=False),
        sa.Column("ingestion_run_source_id", uuid_type, nullable=False),
        sa.Column("source_object_id", uuid_type, nullable=False),
        sa.Column("source_revision_id", uuid_type, nullable=False),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("environment_id", uuid_type, nullable=False),
        sa.Column("binding_command_identity", sa.Text(), nullable=False),
        sa.Column("binding_command_digest", digest, nullable=False),
        sa.Column("original_reference", postgresql.JSONB()),
        sa.Column("original_receipt_kind", sa.Text()),
        sa.Column("original_receipt_id", uuid_type),
        sa.Column("original_receipt_digest", digest),
        sa.Column("idempotency_receipt_id", uuid_type, nullable=False),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("binding_id", name="pk_ingestion_run_source_bindings"),
        scope_unique("uq_ingestion_run_source_bindings_scope_id", "binding_id"),
        sa.UniqueConstraint("workspace_id", "environment_id", "ingestion_run_source_id", name="uq_ingestion_run_source_bindings_item"),
        sa.UniqueConstraint("workspace_id", "environment_id", "binding_command_identity", name="uq_ingestion_run_source_bindings_command"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "ingestion_run_source_id"], [f"{SCHEMA}.ingestion_run_sources.workspace_id", f"{SCHEMA}.ingestion_run_sources.environment_id", f"{SCHEMA}.ingestion_run_sources.ingestion_run_source_id"], name="fk_ingestion_run_source_bindings_item"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "source_object_id", "source_revision_id"], [f"{SCHEMA}.source_revisions.workspace_id", f"{SCHEMA}.source_revisions.environment_id", f"{SCHEMA}.source_revisions.source_object_id", f"{SCHEMA}.source_revisions.revision_id"], name="fk_ingestion_run_source_bindings_revision"),
        sa.ForeignKeyConstraint(["idempotency_receipt_id"], [f"{SCHEMA}.idempotency_receipts.receipt_id"], name="fk_ingestion_run_source_bindings_receipt"),
        sa.CheckConstraint("(original_receipt_id IS NULL AND original_receipt_kind IS NULL AND original_receipt_digest IS NULL AND original_reference IS NULL) OR (original_receipt_id IS NOT NULL AND original_receipt_kind = 'issue6.write_receipt' AND original_receipt_digest OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$' AND original_reference IS NOT NULL)", name="receipt_complete"),
        sa.CheckConstraint("binding_command_digest OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$'", name="digest"),
        schema=SCHEMA,
    )
    op.create_index("ix_ingestion_run_source_bindings_revision", "ingestion_run_source_bindings", ["workspace_id", "environment_id", "source_object_id", "source_revision_id"], schema=SCHEMA)

    op.create_table(
        "ingestion_transitions",
        sa.Column("ingestion_transition_id", uuid_type, nullable=False),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("environment_id", uuid_type, nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("ingestion_run_id", uuid_type, nullable=False),
        sa.Column("ingestion_run_source_id", uuid_type),
        sa.Column("predecessor_transition_id", uuid_type),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("safe_metrics_schema", sa.Text()),
        sa.Column("safe_metrics", postgresql.JSONB()),
        sa.Column("actor_or_service", sa.Text(), nullable=False),
        sa.Column("trace_id", uuid_type, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("ingestion_transition_id", name="pk_ingestion_transitions"),
        scope_unique("uq_ingestion_transitions_scope_id", "ingestion_transition_id"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "ingestion_run_id"], [f"{SCHEMA}.ingestion_runs.workspace_id", f"{SCHEMA}.ingestion_runs.environment_id", f"{SCHEMA}.ingestion_runs.ingestion_run_id"], name="fk_ingestion_transitions_run"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "ingestion_run_source_id"], [f"{SCHEMA}.ingestion_run_sources.workspace_id", f"{SCHEMA}.ingestion_run_sources.environment_id", f"{SCHEMA}.ingestion_run_sources.ingestion_run_source_id"], name="fk_ingestion_transitions_item"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "predecessor_transition_id"], [f"{SCHEMA}.ingestion_transitions.workspace_id", f"{SCHEMA}.ingestion_transitions.environment_id", f"{SCHEMA}.ingestion_transitions.ingestion_transition_id"], name="fk_ingestion_transitions_predecessor"),
        sa.CheckConstraint("target_kind IN ('run', 'item')", name="ck_ingestion_transitions_target_kind"),
        sa.CheckConstraint("((target_kind = 'run' AND ingestion_run_source_id IS NULL AND state IN ('requested', 'receiving', 'processing', 'partially_succeeded', 'succeeded', 'failed', 'cancelled')) OR (target_kind = 'item' AND ingestion_run_source_id IS NOT NULL AND state IN ('received', 'stored', 'observed', 'admission_pending', 'quarantined', 'rejected', 'accepted', 'processing', 'processed', 'processing_failed', 'projection_requested')))", name="ck_ingestion_transitions_target_state"),
        sa.CheckConstraint("attempt > 0", name="attempt_positive"),
        sa.CheckConstraint("(safe_metrics_schema IS NULL AND safe_metrics IS NULL) OR (safe_metrics_schema = 'ingestion-safe-detail:v1' AND jsonb_typeof(safe_metrics) = 'object')", name="ck_ingestion_transitions_safe_metrics"),
        schema=SCHEMA,
    )
    op.create_index("uq_ingestion_transitions_root", "ingestion_transitions", ["workspace_id", "environment_id", "target_kind", "ingestion_run_id", "ingestion_run_source_id"], unique=True, schema=SCHEMA, postgresql_where=sa.text("predecessor_transition_id IS NULL"))
    op.create_index("uq_ingestion_transitions_predecessor", "ingestion_transitions", ["workspace_id", "environment_id", "predecessor_transition_id"], unique=True, schema=SCHEMA, postgresql_where=sa.text("predecessor_transition_id IS NOT NULL"))
    op.create_index("ix_ingestion_transitions_target", "ingestion_transitions", ["workspace_id", "environment_id", "ingestion_run_id", "ingestion_run_source_id", "occurred_at"], schema=SCHEMA)

    for table, code_check, stage_check in (
        ("ingestion_warnings", "code IN ('metadata_normalized', 'partial_coverage', 'source_order_unknown', 'orphan_candidate')", "stage IN ('receiving', 'validating', 'storing', 'observing', 'parsing', 'locating')"),
        ("ingestion_failures", "code IN ('invalid_input', 'storage_unavailable', 'storage_integrity_conflict', 'observation_conflict', 'parser_failed', 'locator_failed', 'persistence_failed', 'cancelled')", "stage IN ('receiving', 'validating', 'storing', 'observing', 'parsing', 'locating', 'persisting')"),
    ):
        columns = [
            sa.Column(f"{table[:-1]}_id", uuid_type, nullable=False),
            sa.Column("workspace_id", uuid_type, nullable=False), sa.Column("environment_id", uuid_type, nullable=False),
            sa.Column("ingestion_run_source_id", uuid_type, nullable=False), sa.Column("attempt", sa.Integer(), nullable=False),
            sa.Column("stage", sa.Text(), nullable=False), sa.Column("code", sa.Text(), nullable=False),
            sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("safe_detail_schema", sa.Text(), nullable=False), sa.Column("safe_detail", postgresql.JSONB(), nullable=False),
            sa.Column("safe_detail_digest", digest, nullable=False), sa.Column("opaque_detail_ref", uuid_type),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        ]
        op.create_table(table, *columns,
            sa.PrimaryKeyConstraint(f"{table[:-1]}_id", name=f"pk_{table}"),
            scope_unique(f"uq_{table}_scope_id", f"{table[:-1]}_id"),
            sa.ForeignKeyConstraint(["workspace_id", "environment_id", "ingestion_run_source_id"], [f"{SCHEMA}.ingestion_run_sources.workspace_id", f"{SCHEMA}.ingestion_run_sources.environment_id", f"{SCHEMA}.ingestion_run_sources.ingestion_run_source_id"], name=f"fk_{table}_item"),
            sa.CheckConstraint("attempt > 0", name=f"ck_{table}_attempt_positive"),
            sa.CheckConstraint(stage_check, name=f"ck_{table}_stage"), sa.CheckConstraint(code_check, name=f"ck_{table}_code"),
            sa.CheckConstraint("safe_detail_schema = 'ingestion-safe-detail:v1' AND jsonb_typeof(safe_detail) = 'object'", name=f"ck_{table}_safe_detail"),
            sa.CheckConstraint("safe_detail_digest OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$'", name=f"ck_{table}_safe_detail_digest"),
            schema=SCHEMA,
        )
        op.create_index(f"uq_{table}_dedup", table, ["workspace_id", "environment_id", "ingestion_run_source_id", "attempt", "code", "safe_detail_digest"], unique=True, schema=SCHEMA)
        op.create_index(f"ix_{table}_item", table, ["workspace_id", "environment_id", "ingestion_run_source_id", "attempt"], schema=SCHEMA)

    op.create_table(
        "processing_receipts",
        sa.Column("processing_receipt_id", uuid_type, nullable=False), sa.Column("workspace_id", uuid_type, nullable=False), sa.Column("environment_id", uuid_type, nullable=False),
        sa.Column("ingestion_run_source_id", uuid_type, nullable=False), sa.Column("binding_id", uuid_type, nullable=False), sa.Column("source_object_id", uuid_type, nullable=False), sa.Column("source_revision_id", uuid_type, nullable=False),
        sa.Column("processor_kind", sa.Text(), nullable=False), sa.Column("parser_name", sa.Text(), nullable=False), sa.Column("parser_version", sa.Text(), nullable=False), sa.Column("parser_config_reference", sa.Text(), nullable=False), sa.Column("locator_contract_version", sa.Text(), nullable=False),
        sa.Column("original_sha256", digest, nullable=False), sa.Column("output_kind", sa.Text()), sa.Column("output_reference", uuid_type), sa.Column("output_digest", digest), sa.Column("coverage", sa.Text(), nullable=False), sa.Column("exclusions", postgresql.JSONB(), nullable=False), sa.Column("warning_ids", postgresql.JSONB(), nullable=False), sa.Column("failure_ids", postgresql.JSONB(), nullable=False),
        sa.Column("command_identity", sa.Text(), nullable=False), sa.Column("command_digest", digest, nullable=False), sa.Column("idempotency_receipt_id", uuid_type, nullable=False), sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("processing_receipt_id", name="pk_processing_receipts"), scope_unique("uq_processing_receipts_scope_id", "processing_receipt_id"), sa.UniqueConstraint("workspace_id", "environment_id", "command_identity", name="uq_processing_receipts_command"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "ingestion_run_source_id"], [f"{SCHEMA}.ingestion_run_sources.workspace_id", f"{SCHEMA}.ingestion_run_sources.environment_id", f"{SCHEMA}.ingestion_run_sources.ingestion_run_source_id"], name="fk_processing_receipts_item"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "binding_id"], [f"{SCHEMA}.ingestion_run_source_bindings.workspace_id", f"{SCHEMA}.ingestion_run_source_bindings.environment_id", f"{SCHEMA}.ingestion_run_source_bindings.binding_id"], name="fk_processing_receipts_binding"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "source_object_id", "source_revision_id"], [f"{SCHEMA}.source_revisions.workspace_id", f"{SCHEMA}.source_revisions.environment_id", f"{SCHEMA}.source_revisions.source_object_id", f"{SCHEMA}.source_revisions.revision_id"], name="fk_processing_receipts_revision"),
        sa.ForeignKeyConstraint(["idempotency_receipt_id"], [f"{SCHEMA}.idempotency_receipts.receipt_id"], name="fk_processing_receipts_receipt"),
        sa.CheckConstraint("coverage IN ('complete', 'partial', 'failed')", name="ck_processing_receipts_coverage"), sa.CheckConstraint("locator_contract_version <> ''", name="ck_processing_receipts_locator_version"),
        sa.CheckConstraint("(coverage = 'failed' AND output_reference IS NULL AND output_digest IS NULL) OR (coverage <> 'failed' AND output_kind IS NOT NULL AND output_reference IS NOT NULL AND output_digest IS NOT NULL)", name="ck_processing_receipts_output"), sa.CheckConstraint("(coverage = 'complete' AND exclusions = '[]'::jsonb) OR coverage <> 'complete'", name="complete_exclusions"),
        sa.CheckConstraint("original_sha256 OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$' AND command_digest OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$'", name="ck_processing_receipts_digests"),
        schema=SCHEMA,
    )
    op.create_index("ix_processing_receipts_revision", "processing_receipts", ["workspace_id", "environment_id", "source_revision_id", "parser_name"], schema=SCHEMA)
    op.create_index("ix_processing_receipts_item", "processing_receipts", ["workspace_id", "environment_id", "ingestion_run_source_id"], schema=SCHEMA)

    for table in ("ingestion_runs", "ingestion_run_sources", "ingestion_run_source_bindings", "ingestion_transitions", "ingestion_warnings", "ingestion_failures", "processing_receipts"):
        _tenant(table)
        _immutable(table)
    _safe_detail_triggers()
    _binding_original_trigger()
    for table in ("ingestion_runs", "ingestion_run_source_bindings", "processing_receipts"):
        _receipt_trigger(table)


def downgrade() -> None:
    runtime = _role("runtime_role")
    for table in ("processing_receipts", "ingestion_failures", "ingestion_warnings", "ingestion_transitions", "ingestion_run_source_bindings", "ingestion_run_sources", "ingestion_runs"):
        qualified = f'{SCHEMA}."{table}"'
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {qualified} FROM {runtime}")
        op.drop_table(table, schema=SCHEMA)
        op.execute(f"DROP FUNCTION {SCHEMA}.reject_{table}_mutation()")
    op.execute(f"DROP FUNCTION {SCHEMA}.validate_issue7_binding_original()")
    op.execute(f"DROP FUNCTION {SCHEMA}.validate_issue7_safe_detail()")
