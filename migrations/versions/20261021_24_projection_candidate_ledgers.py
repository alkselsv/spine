"""Persist Issue #87 ontology, projection configuration and candidate evidence."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261021_24"
down_revision = "20261020_23"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def _tenant(table: str, *, allow_update: bool = False) -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    qualified = f'{SCHEMA}."{table}"'
    predicate = (
        "workspace_id = (CASE WHEN pg_catalog.current_setting('spine.workspace_id', true) "
        "OPERATOR(pg_catalog.~) '^[0-9A-Fa-f-]{36}$' THEN "
        "CAST(pg_catalog.current_setting('spine.workspace_id', true) AS pg_catalog.uuid) ELSE NULL END) "
        "AND environment_id = (CASE WHEN pg_catalog.current_setting('spine.environment_id', true) "
        "OPERATOR(pg_catalog.~) '^[0-9A-Fa-f-]{36}$' THEN "
        "CAST(pg_catalog.current_setting('spine.environment_id', true) AS pg_catalog.uuid) ELSE NULL END)"
    )
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(
        f'CREATE POLICY "pol_{table}_tenant_isolation" ON {qualified} '
        f"FOR ALL TO {runtime} USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.execute(
        f'CREATE POLICY "pol_{table}_migration_maintenance" ON {qualified} '
        f"FOR ALL TO {migration} USING (true) WITH CHECK (true)"
    )
    privileges = "SELECT, INSERT, UPDATE" if allow_update else "SELECT, INSERT"
    op.execute(f"GRANT {privileges} ON TABLE {qualified} TO {runtime}")
    op.execute(f"REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {qualified} FROM {runtime}")


def _immutable(table: str, message: str) -> None:
    qualified = f'{SCHEMA}."{table}"'
    function = f"reject_{table}_mutation"
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.{function}() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ BEGIN
            RAISE EXCEPTION '{message}'
                USING ERRCODE = '23514', CONSTRAINT = 'ck_{table}_immutable';
        END; $function$
        """
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON "
        f"{qualified} FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.{function}()"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.{function}() FROM PUBLIC")


def _common(table: str, *, allow_update: bool = False) -> None:
    _tenant(table, allow_update=allow_update)


def _receipt_scope_guard(table: str) -> None:
    qualified = f'{SCHEMA}."{table}"'
    op.execute(
        f"CREATE CONSTRAINT TRIGGER trg_{table}_receipt_scope "
        f"AFTER INSERT OR UPDATE OF workspace_id, environment_id, idempotency_receipt_id "
        f"ON {qualified} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        f"EXECUTE FUNCTION {SCHEMA}.validate_issue7_receipt_scope()"
    )


def _candidate_evidence_guard(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_candidate_open "
        f"BEFORE INSERT ON {SCHEMA}.\"{table}\" FOR EACH ROW "
        f"EXECUTE FUNCTION {SCHEMA}.reject_projection_evidence_after_seal()"
    )


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    digest = sa.String(64)
    op.create_table(
        "ontology_versions",
        sa.Column("ontology_version_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("ontology_name", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("predecessor_ontology_version_id", uuid),
        sa.Column("definition_schema_version", sa.Text(), nullable=False),
        sa.Column("definition_payload", postgresql.JSONB(), nullable=False),
        sa.Column("definition_digest", digest, nullable=False),
        sa.Column("publication_eligible", sa.Boolean(), nullable=False),
        sa.Column("publication_decision_reference", sa.Text(), nullable=False),
        sa.Column("publisher_authority_reference", sa.Text(), nullable=False),
        sa.Column("audit_event_reference", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("ontology_version_id", name="pk_ontology_versions"),
        sa.UniqueConstraint("workspace_id", "environment_id", "ontology_version_id", name="uq_ontology_versions_scope_id"),
        sa.UniqueConstraint("workspace_id", "environment_id", "ontology_name", "version", name="uq_ontology_versions_name_version"),
        sa.UniqueConstraint("workspace_id", "environment_id", "ontology_name", "definition_digest", name="uq_ontology_versions_name_digest"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "predecessor_ontology_version_id"],
            [f"{SCHEMA}.ontology_versions.workspace_id", f"{SCHEMA}.ontology_versions.environment_id", f"{SCHEMA}.ontology_versions.ontology_version_id"],
            name="fk_ontology_versions_predecessor",
        ),
        sa.CheckConstraint("version > 0", name="ck_ontology_versions_version"),
        sa.CheckConstraint("definition_schema_version = 'ontology-definition:r1'", name="ck_ontology_versions_schema"),
        sa.CheckConstraint("definition_digest ~ '^[0-9a-f]{64}$'", name="ck_ontology_versions_digest"),
        schema=SCHEMA,
    )
    op.create_table(
        "projection_config_versions",
        sa.Column("projection_config_version_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("projection_kind", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("predecessor_projection_config_version_id", uuid),
        sa.Column("ontology_version_id", uuid, nullable=False),
        sa.Column("parser_contract_version", sa.Text(), nullable=False),
        sa.Column("locator_contract_version", sa.Text(), nullable=False),
        sa.Column("extraction_contract_version", sa.Text(), nullable=False),
        sa.Column("embedding_profile_version", sa.Text(), nullable=False),
        sa.Column("adapter_contract_version", sa.Text(), nullable=False),
        sa.Column("config_schema_version", sa.Text(), nullable=False),
        sa.Column("config_payload", postgresql.JSONB(), nullable=False),
        sa.Column("config_digest", digest, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("projection_config_version_id", name="pk_projection_config_versions"),
        sa.UniqueConstraint("workspace_id", "environment_id", "projection_config_version_id", name="uq_projection_configs_scope_id"),
        sa.UniqueConstraint("workspace_id", "environment_id", "projection_kind", "version", name="uq_projection_configs_kind_version"),
        sa.UniqueConstraint("workspace_id", "environment_id", "projection_kind", "config_digest", name="uq_projection_configs_kind_digest"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "ontology_version_id"],
            [f"{SCHEMA}.ontology_versions.workspace_id", f"{SCHEMA}.ontology_versions.environment_id", f"{SCHEMA}.ontology_versions.ontology_version_id"],
            name="fk_projection_configs_ontology",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "predecessor_projection_config_version_id"],
            [f"{SCHEMA}.projection_config_versions.workspace_id", f"{SCHEMA}.projection_config_versions.environment_id", f"{SCHEMA}.projection_config_versions.projection_config_version_id"],
            name="fk_projection_configs_predecessor",
        ),
        sa.CheckConstraint("version > 0", name="ck_projection_configs_version"),
        sa.CheckConstraint("config_schema_version = 'projection-config:r1'", name="ck_projection_configs_schema"),
        sa.CheckConstraint("config_digest ~ '^[0-9a-f]{64}$'", name="ck_projection_configs_digest"),
        schema=SCHEMA,
    )
    op.create_table(
        "projection_runs",
        sa.Column("projection_run_id", uuid, nullable=False),
        sa.Column("projection_candidate_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("projection_kind", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("projection_config_version_id", uuid, nullable=False),
        sa.Column("canonical_boundary_id", uuid, nullable=False),
        sa.Column("command_identity", sa.Text(), nullable=False),
        sa.Column("command_digest", digest, nullable=False),
        sa.Column("idempotency_receipt_id", uuid, nullable=False),
        sa.Column("candidate_state", sa.Text(), nullable=False),
        sa.Column("candidate_evidence_digest", digest),
        sa.Column("candidate_sealed_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("projection_run_id", name="pk_projection_runs"),
        sa.UniqueConstraint("workspace_id", "environment_id", "projection_run_id", "projection_candidate_id", name="uq_projection_runs_candidate_target"),
        sa.UniqueConstraint("workspace_id", "environment_id", "projection_candidate_id", name="uq_projection_runs_candidate"),
        sa.UniqueConstraint("workspace_id", "environment_id", "command_identity", name="uq_projection_runs_command"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "projection_config_version_id"],
            [f"{SCHEMA}.projection_config_versions.workspace_id", f"{SCHEMA}.projection_config_versions.environment_id", f"{SCHEMA}.projection_config_versions.projection_config_version_id"],
            name="fk_projection_runs_config",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "canonical_boundary_id"],
            [
                f"{SCHEMA}.canonical_boundaries.workspace_id",
                f"{SCHEMA}.canonical_boundaries.environment_id",
                f"{SCHEMA}.canonical_boundaries.boundary_id",
            ],
            name="fk_projection_runs_boundary",
        ),
        sa.ForeignKeyConstraint(["idempotency_receipt_id"], [f"{SCHEMA}.idempotency_receipts.receipt_id"], name="fk_projection_runs_idempotency"),
        sa.CheckConstraint("operation IN ('build', 'catch_up', 'rebuild', 'rollback')", name="ck_projection_runs_operation"),
        sa.CheckConstraint("candidate_state IN ('open', 'sealed')", name="ck_projection_runs_candidate_state"),
        sa.CheckConstraint("(candidate_state = 'open' AND candidate_evidence_digest IS NULL AND candidate_sealed_at IS NULL) OR (candidate_state = 'sealed' AND candidate_evidence_digest IS NOT NULL AND candidate_sealed_at IS NOT NULL)", name="ck_projection_runs_open_or_sealed"),
        schema=SCHEMA,
    )
    # The boundary table's globally named primary key is boundary_id; the
    # composite unique target is used here to keep tenant lineage explicit.
    op.create_table(
        "projection_run_transitions",
        sa.Column("projection_run_transition_id", uuid, nullable=False),
        sa.Column("projection_run_id", uuid, nullable=False),
        sa.Column("projection_candidate_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("predecessor_transition_id", uuid),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.Text()),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("projection_run_transition_id", name="pk_projection_run_transitions"),
        sa.UniqueConstraint("workspace_id", "environment_id", "projection_run_id", "predecessor_transition_id", name="uq_projection_transitions_predecessor"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "projection_run_id", "projection_candidate_id"], [f"{SCHEMA}.projection_runs.workspace_id", f"{SCHEMA}.projection_runs.environment_id", f"{SCHEMA}.projection_runs.projection_run_id", f"{SCHEMA}.projection_runs.projection_candidate_id"], name="fk_projection_transitions_run"),
        sa.CheckConstraint("state IN ('requested', 'building', 'validating', 'complete', 'failed', 'abandoned')", name="ck_projection_transitions_state"),
        sa.CheckConstraint("attempt > 0", name="ck_projection_transitions_attempt"),
        schema=SCHEMA,
    )
    op.create_table(
        "projection_partitions",
        sa.Column("projection_partition_id", uuid, nullable=False),
        sa.Column("projection_run_id", uuid, nullable=False),
        sa.Column("projection_candidate_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("security_domain_reference", sa.Text(), nullable=False),
        sa.Column("security_domain_version", sa.Text(), nullable=False),
        sa.Column("partition_identity", sa.Text(), nullable=False),
        sa.Column("expected_command_identity", sa.Text(), nullable=False),
        sa.Column("expected_command_digest", digest, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("projection_partition_id", name="pk_projection_partitions"),
        sa.UniqueConstraint("workspace_id", "environment_id", "projection_partition_id", name="uq_projection_partitions_scope_id"),
        sa.UniqueConstraint("workspace_id", "environment_id", "projection_run_id", "projection_candidate_id", "partition_identity", name="uq_projection_partitions_identity"),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "projection_candidate_id",
            "security_domain_reference",
            "security_domain_version",
            "partition_identity",
            name="uq_projection_partitions_candidate_domain",
        ),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "projection_run_id", "projection_candidate_id"], [f"{SCHEMA}.projection_runs.workspace_id", f"{SCHEMA}.projection_runs.environment_id", f"{SCHEMA}.projection_runs.projection_run_id", f"{SCHEMA}.projection_runs.projection_candidate_id"], name="fk_projection_partitions_run"),
        schema=SCHEMA,
    )
    op.create_table(
        "projection_receipts",
        sa.Column("projection_receipt_id", uuid, nullable=False),
        sa.Column("projection_run_id", uuid, nullable=False),
        sa.Column("projection_candidate_id", uuid, nullable=False),
        sa.Column("projection_partition_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("command_identity", sa.Text(), nullable=False),
        sa.Column("command_digest", digest, nullable=False),
        sa.Column("artifact_kind", sa.Text(), nullable=False),
        sa.Column("artifact_reference", sa.Text(), nullable=False),
        sa.Column("artifact_integrity_digest", digest, nullable=False),
        sa.Column("counts", postgresql.JSONB(), nullable=False),
        sa.Column("warnings", postgresql.JSONB(), nullable=False),
        sa.Column("failures", postgresql.JSONB(), nullable=False),
        sa.Column("implementation_schema_version", sa.Text(), nullable=False),
        sa.Column("implementation_metadata", postgresql.JSONB(), nullable=False),
        sa.Column("idempotency_receipt_id", uuid, nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("projection_receipt_id", name="pk_projection_receipts"),
        sa.UniqueConstraint("workspace_id", "environment_id", "projection_receipt_id", name="uq_projection_receipts_scope_id"),
        sa.UniqueConstraint("workspace_id", "environment_id", "command_identity", name="uq_projection_receipts_command"),
        sa.UniqueConstraint("workspace_id", "environment_id", "projection_partition_id", "command_identity", name="uq_projection_receipts_partition_command"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "projection_run_id", "projection_candidate_id"], [f"{SCHEMA}.projection_runs.workspace_id", f"{SCHEMA}.projection_runs.environment_id", f"{SCHEMA}.projection_runs.projection_run_id", f"{SCHEMA}.projection_runs.projection_candidate_id"], name="fk_projection_receipts_run"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "projection_partition_id"],
            [
                f"{SCHEMA}.projection_partitions.workspace_id",
                f"{SCHEMA}.projection_partitions.environment_id",
                f"{SCHEMA}.projection_partitions.projection_partition_id",
            ],
            name="fk_projection_receipts_partition",
        ),
        sa.ForeignKeyConstraint(["idempotency_receipt_id"], [f"{SCHEMA}.idempotency_receipts.receipt_id"], name="fk_projection_receipts_idempotency"),
        sa.CheckConstraint("command_digest ~ '^[0-9a-f]{64}$'", name="ck_projection_receipts_command_digest"),
        schema=SCHEMA,
    )
    op.create_table(
        "source_projection_references",
        sa.Column("source_projection_reference_id", uuid, nullable=False),
        sa.Column("projection_run_id", uuid, nullable=False),
        sa.Column("projection_candidate_id", uuid, nullable=False),
        sa.Column("projection_receipt_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("source_object_id", uuid, nullable=False),
        sa.Column("source_revision_id", uuid, nullable=False),
        sa.Column("processing_receipt_id", uuid),
        sa.Column("segment_id", sa.Text(), nullable=False),
        sa.Column("locator_schema_version", sa.Text(), nullable=False),
        sa.Column("locator_reference", sa.Text(), nullable=False),
        sa.Column("locator_digest", digest, nullable=False),
        sa.Column("projected_kind", sa.Text(), nullable=False),
        sa.Column("projected_object_id", sa.Text(), nullable=False),
        sa.Column("projection_config_version_id", uuid, nullable=False),
        sa.Column("security_domain_reference", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("source_projection_reference_id", name="pk_source_projection_references"),
        sa.UniqueConstraint("workspace_id", "environment_id", "projection_candidate_id", "source_object_id", "source_revision_id", "segment_id", name="uq_source_projection_references_lineage"),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "projection_candidate_id",
            "projected_kind",
            "projected_object_id",
            name="uq_source_projection_references_projected_object",
        ),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "projection_run_id", "projection_candidate_id"], [f"{SCHEMA}.projection_runs.workspace_id", f"{SCHEMA}.projection_runs.environment_id", f"{SCHEMA}.projection_runs.projection_run_id", f"{SCHEMA}.projection_runs.projection_candidate_id"], name="fk_source_projection_references_run"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "projection_receipt_id"],
            [
                f"{SCHEMA}.projection_receipts.workspace_id",
                f"{SCHEMA}.projection_receipts.environment_id",
                f"{SCHEMA}.projection_receipts.projection_receipt_id",
            ],
            name="fk_source_projection_references_receipt",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id"],
            [
                f"{SCHEMA}.source_objects.workspace_id",
                f"{SCHEMA}.source_objects.environment_id",
                f"{SCHEMA}.source_objects.source_object_id",
            ],
            name="fk_source_projection_references_source",
        ),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "source_object_id", "source_revision_id"], [f"{SCHEMA}.source_revisions.workspace_id", f"{SCHEMA}.source_revisions.environment_id", f"{SCHEMA}.source_revisions.source_object_id", f"{SCHEMA}.source_revisions.revision_id"], name="fk_source_projection_references_revision"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "processing_receipt_id"],
            [
                f"{SCHEMA}.processing_receipts.workspace_id",
                f"{SCHEMA}.processing_receipts.environment_id",
                f"{SCHEMA}.processing_receipts.processing_receipt_id",
            ],
            name="fk_source_projection_references_processing_receipt",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "projection_config_version_id"],
            [
                f"{SCHEMA}.projection_config_versions.workspace_id",
                f"{SCHEMA}.projection_config_versions.environment_id",
                f"{SCHEMA}.projection_config_versions.projection_config_version_id",
            ],
            name="fk_source_projection_references_config",
        ),
        schema=SCHEMA,
    )
    for table in (
        "ontology_versions",
        "projection_config_versions",
        "projection_runs",
        "projection_run_transitions",
        "projection_partitions",
        "projection_receipts",
        "source_projection_references",
    ):
        _common(table, allow_update=table == "projection_runs")
    for table in (
        "ontology_versions",
        "projection_config_versions",
        "projection_run_transitions",
        "projection_partitions",
        "projection_receipts",
        "source_projection_references",
    ):
        _immutable(table, "Projection ledger records are immutable.")
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.reject_projection_evidence_after_seal() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE candidate_state text;
        BEGIN
            SELECT run.candidate_state
              INTO candidate_state
              FROM {SCHEMA}.projection_runs AS run
             WHERE run.workspace_id = NEW.workspace_id
               AND run.environment_id = NEW.environment_id
               AND run.projection_run_id = NEW.projection_run_id
               AND run.projection_candidate_id = NEW.projection_candidate_id
             FOR KEY SHARE;
            IF candidate_state IS DISTINCT FROM 'open' THEN
                RAISE EXCEPTION 'Projection candidate evidence is sealed.'
                    USING ERRCODE = '40001', CONSTRAINT = 'ck_projection_candidate_open';
            END IF;
            RETURN NEW;
        END; $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.reject_projection_evidence_after_seal() FROM PUBLIC")
    for table in (
        "projection_run_transitions",
        "projection_partitions",
        "projection_receipts",
        "source_projection_references",
    ):
        _candidate_evidence_guard(table)
    for table in ("projection_runs", "projection_receipts"):
        _receipt_scope_guard(table)
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.guard_projection_run_seal() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        BEGIN
            IF OLD.candidate_state <> 'open'
               OR NEW.candidate_state <> 'sealed'
               OR NEW.projection_run_id IS DISTINCT FROM OLD.projection_run_id
               OR NEW.projection_candidate_id IS DISTINCT FROM OLD.projection_candidate_id
               OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
               OR NEW.environment_id IS DISTINCT FROM OLD.environment_id
               OR NEW.projection_kind IS DISTINCT FROM OLD.projection_kind
               OR NEW.operation IS DISTINCT FROM OLD.operation
               OR NEW.projection_config_version_id IS DISTINCT FROM OLD.projection_config_version_id
               OR NEW.canonical_boundary_id IS DISTINCT FROM OLD.canonical_boundary_id
               OR NEW.command_identity IS DISTINCT FROM OLD.command_identity
               OR NEW.command_digest IS DISTINCT FROM OLD.command_digest
               OR NEW.idempotency_receipt_id IS DISTINCT FROM OLD.idempotency_receipt_id
               OR NEW.started_at IS DISTINCT FROM OLD.started_at
               OR NEW.candidate_evidence_digest IS NULL
               OR NEW.candidate_sealed_at IS NULL
            THEN
                RAISE EXCEPTION 'Projection run is immutable except for candidate sealing.'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_projection_runs_immutable';
            END IF;
            RETURN NEW;
        END; $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.guard_projection_run_seal() FROM PUBLIC")
    op.execute(
        f"CREATE TRIGGER trg_projection_runs_seal_guard BEFORE UPDATE ON {SCHEMA}.projection_runs "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_projection_run_seal()"
    )
    op.create_index("ix_projection_runs_scope_state", "projection_runs", ["workspace_id", "environment_id", "projection_kind", "candidate_state"], schema=SCHEMA)
    op.create_index(
        "uq_projection_transitions_root",
        "projection_run_transitions",
        ["workspace_id", "environment_id", "projection_run_id"],
        unique=True,
        postgresql_where=sa.text("predecessor_transition_id IS NULL"),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_projection_transitions_successor",
        "projection_run_transitions",
        ["workspace_id", "environment_id", "predecessor_transition_id"],
        unique=True,
        postgresql_where=sa.text("predecessor_transition_id IS NOT NULL"),
        schema=SCHEMA,
    )
    op.create_index("ix_projection_partitions_candidate", "projection_partitions", ["workspace_id", "environment_id", "projection_candidate_id"], schema=SCHEMA)
    op.create_index("ix_projection_receipts_candidate", "projection_receipts", ["workspace_id", "environment_id", "projection_candidate_id"], schema=SCHEMA)
    op.create_index("ix_source_projection_references_source", "source_projection_references", ["workspace_id", "environment_id", "source_object_id", "source_revision_id"], schema=SCHEMA)


def downgrade() -> None:
    runtime = _role("runtime_role")
    tables = (
        "source_projection_references",
        "projection_receipts",
        "projection_partitions",
        "projection_run_transitions",
        "projection_runs",
        "projection_config_versions",
        "ontology_versions",
    )
    for table in tables:
        op.execute(f'REVOKE ALL PRIVILEGES ON TABLE {SCHEMA}."{table}" FROM {runtime}')
    for table in (
        "source_projection_references",
        "projection_receipts",
        "projection_partitions",
        "projection_run_transitions",
        "projection_config_versions",
        "ontology_versions",
    ):
        op.execute(f"DROP TRIGGER trg_{table}_immutable ON {SCHEMA}.\"{table}\"")
        op.execute(f"DROP FUNCTION {SCHEMA}.reject_{table}_mutation()")
    op.execute(f"DROP TRIGGER trg_projection_runs_seal_guard ON {SCHEMA}.projection_runs")
    op.execute(f"DROP FUNCTION {SCHEMA}.guard_projection_run_seal()")
    for table in (
        "projection_run_transitions",
        "projection_partitions",
        "projection_receipts",
        "source_projection_references",
    ):
        op.execute(f"DROP TRIGGER trg_{table}_candidate_open ON {SCHEMA}.\"{table}\"")
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_projection_evidence_after_seal()")
    for table in ("projection_runs", "projection_receipts"):
        op.execute(f"DROP TRIGGER trg_{table}_receipt_scope ON {SCHEMA}.\"{table}\"")
    for table in tables:
        op.drop_table(table, schema=SCHEMA)
