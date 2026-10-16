"""Persist Issue #88 snapshot sealing and reconciliation evidence."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261021_25"
down_revision = "20261021_24"
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


def _receipt_scope_guard(table: str) -> None:
    op.execute(
        f"CREATE CONSTRAINT TRIGGER trg_{table}_receipt_scope "
        f"AFTER INSERT OR UPDATE OF workspace_id, environment_id, idempotency_receipt_id "
        f"ON {SCHEMA}.\"{table}\" DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        f"EXECUTE FUNCTION {SCHEMA}.validate_issue7_receipt_scope()"
    )


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    digest = sa.String(64)

    op.create_unique_constraint(
        "uq_canonical_boundary_members_scope_member",
        "canonical_boundary_members",
        ["workspace_id", "environment_id", "boundary_id", "source_object_id"],
        schema=SCHEMA,
    )
    op.create_table(
        "projection_validations",
        sa.Column("projection_validation_id", uuid, nullable=False),
        sa.Column("projection_run_id", uuid, nullable=False),
        sa.Column("projection_candidate_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("gate_kind", sa.Text(), nullable=False),
        sa.Column("gate_version", sa.Text(), nullable=False),
        sa.Column("input_digest", digest, nullable=False),
        sa.Column("result_digest", digest, nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("metrics_schema_version", sa.Text(), nullable=False),
        sa.Column("metrics_payload", postgresql.JSONB(), nullable=False),
        sa.Column("evaluator_reference", sa.Text(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("projection_validation_id", name="pk_projection_validations"),
        sa.UniqueConstraint("workspace_id", "environment_id", "projection_validation_id", name="uq_projection_validations_scope_id"),
        sa.UniqueConstraint("workspace_id", "environment_id", "projection_candidate_id", "gate_kind", "gate_version", "input_digest", name="uq_projection_validations_gate"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "projection_run_id", "projection_candidate_id"],
            [f"{SCHEMA}.projection_runs.workspace_id", f"{SCHEMA}.projection_runs.environment_id", f"{SCHEMA}.projection_runs.projection_run_id", f"{SCHEMA}.projection_runs.projection_candidate_id"],
            name="fk_projection_validations_run",
        ),
        sa.CheckConstraint("input_digest ~ '^[0-9a-f]{64}$'", name="ck_projection_validations_input_digest"),
        sa.CheckConstraint("result_digest ~ '^[0-9a-f]{64}$'", name="ck_projection_validations_result_digest"),
        sa.CheckConstraint("decision IN ('passed', 'failed')", name="ck_projection_validations_decision"),
        schema=SCHEMA,
    )
    op.create_table(
        "projection_snapshots",
        sa.Column("projection_snapshot_id", uuid, nullable=False),
        sa.Column("projection_run_id", uuid, nullable=False),
        sa.Column("projection_candidate_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("snapshot_state", sa.Text(), nullable=False),
        sa.Column("projection_kind", sa.Text(), nullable=False),
        sa.Column("projection_config_version_id", uuid, nullable=False),
        sa.Column("canonical_boundary_id", uuid, nullable=False),
        sa.Column("predecessor_snapshot_id", uuid),
        sa.Column("candidate_evidence_digest", digest, nullable=False),
        sa.Column("manifest_digest", digest, nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("projection_snapshot_id", name="pk_projection_snapshots"),
        sa.UniqueConstraint("workspace_id", "environment_id", "projection_snapshot_id", name="uq_projection_snapshots_scope_id"),
        sa.UniqueConstraint("workspace_id", "environment_id", "projection_candidate_id", name="uq_projection_snapshots_candidate"),
        sa.UniqueConstraint("workspace_id", "environment_id", "projection_kind", "manifest_digest", name="uq_projection_snapshots_kind_manifest"),
        sa.UniqueConstraint("workspace_id", "environment_id", "projection_kind", "candidate_evidence_digest", name="uq_projection_snapshots_kind_evidence"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "projection_run_id", "projection_candidate_id"],
            [f"{SCHEMA}.projection_runs.workspace_id", f"{SCHEMA}.projection_runs.environment_id", f"{SCHEMA}.projection_runs.projection_run_id", f"{SCHEMA}.projection_runs.projection_candidate_id"],
            name="fk_projection_snapshots_run",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "projection_config_version_id"],
            [f"{SCHEMA}.projection_config_versions.workspace_id", f"{SCHEMA}.projection_config_versions.environment_id", f"{SCHEMA}.projection_config_versions.projection_config_version_id"],
            name="fk_projection_snapshots_config",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "canonical_boundary_id"],
            [f"{SCHEMA}.canonical_boundaries.workspace_id", f"{SCHEMA}.canonical_boundaries.environment_id", f"{SCHEMA}.canonical_boundaries.boundary_id"],
            name="fk_projection_snapshots_boundary",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "predecessor_snapshot_id"],
            [f"{SCHEMA}.projection_snapshots.workspace_id", f"{SCHEMA}.projection_snapshots.environment_id", f"{SCHEMA}.projection_snapshots.projection_snapshot_id"],
            name="fk_projection_snapshots_predecessor",
        ),
        sa.CheckConstraint("snapshot_state IN ('assembling', 'finalized')", name="ck_projection_snapshots_state"),
        sa.CheckConstraint("candidate_evidence_digest ~ '^[0-9a-f]{64}$'", name="ck_projection_snapshots_evidence_digest"),
        sa.CheckConstraint("manifest_digest ~ '^[0-9a-f]{64}$'", name="ck_projection_snapshots_manifest_digest"),
        sa.CheckConstraint("member_count >= 0", name="ck_projection_snapshots_member_count"),
        schema=SCHEMA,
    )
    op.create_table(
        "projection_snapshot_members",
        sa.Column("projection_snapshot_member_id", uuid, nullable=False),
        sa.Column("projection_snapshot_id", uuid, nullable=False),
        sa.Column("canonical_boundary_id", uuid, nullable=False),
        sa.Column("canonical_boundary_member_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("source_object_id", uuid, nullable=False),
        sa.Column("source_revision_id", uuid, nullable=False),
        sa.Column("disposition", sa.Text(), nullable=False),
        sa.Column("exclusion_code", sa.Text()),
        sa.Column("projection_partition_id", uuid),
        sa.Column("projection_receipt_id", uuid),
        sa.PrimaryKeyConstraint("projection_snapshot_member_id", name="pk_projection_snapshot_members"),
        sa.UniqueConstraint("workspace_id", "environment_id", "projection_snapshot_member_id", name="uq_projection_snapshot_members_scope_id"),
        sa.UniqueConstraint("workspace_id", "environment_id", "projection_snapshot_id", "source_object_id", name="uq_projection_snapshot_members_source"),
        sa.UniqueConstraint("workspace_id", "environment_id", "projection_snapshot_id", "canonical_boundary_member_id", name="uq_projection_snapshot_members_boundary"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "projection_snapshot_id"],
            [f"{SCHEMA}.projection_snapshots.workspace_id", f"{SCHEMA}.projection_snapshots.environment_id", f"{SCHEMA}.projection_snapshots.projection_snapshot_id"],
            name="fk_projection_snapshot_members_snapshot",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "canonical_boundary_id", "canonical_boundary_member_id"],
            [f"{SCHEMA}.canonical_boundary_members.workspace_id", f"{SCHEMA}.canonical_boundary_members.environment_id", f"{SCHEMA}.canonical_boundary_members.boundary_id", f"{SCHEMA}.canonical_boundary_members.source_object_id"],
            name="fk_projection_snapshot_members_boundary_member",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "source_revision_id"],
            [f"{SCHEMA}.source_revisions.workspace_id", f"{SCHEMA}.source_revisions.environment_id", f"{SCHEMA}.source_revisions.source_object_id", f"{SCHEMA}.source_revisions.revision_id"],
            name="fk_projection_snapshot_members_revision",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "projection_partition_id"],
            [f"{SCHEMA}.projection_partitions.workspace_id", f"{SCHEMA}.projection_partitions.environment_id", f"{SCHEMA}.projection_partitions.projection_partition_id"],
            name="fk_projection_snapshot_members_partition",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "projection_receipt_id"],
            [f"{SCHEMA}.projection_receipts.workspace_id", f"{SCHEMA}.projection_receipts.environment_id", f"{SCHEMA}.projection_receipts.projection_receipt_id"],
            name="fk_projection_snapshot_members_receipt",
        ),
        sa.CheckConstraint("disposition IN ('included', 'tombstoned', 'excluded')", name="ck_projection_snapshot_members_disposition"),
        sa.CheckConstraint("(disposition = 'included' AND exclusion_code IS NULL AND projection_partition_id IS NOT NULL AND projection_receipt_id IS NOT NULL) OR (disposition = 'tombstoned' AND exclusion_code IS NULL AND projection_partition_id IS NULL AND projection_receipt_id IS NULL) OR (disposition = 'excluded' AND exclusion_code IS NOT NULL AND projection_partition_id IS NULL AND projection_receipt_id IS NULL)", name="ck_projection_snapshot_members_shape"),
        schema=SCHEMA,
    )
    op.create_table(
        "projection_reconciliation_results",
        sa.Column("reconciliation_result_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("projection_kind", sa.Text(), nullable=False),
        sa.Column("projection_snapshot_id", uuid, nullable=False),
        sa.Column("projection_activation_id", uuid, nullable=False),
        sa.Column("activation_generation", sa.Integer(), nullable=False),
        sa.Column("attempt_identity", sa.Text(), nullable=False),
        sa.Column("attempt_digest", digest, nullable=False),
        sa.Column("predecessor_result_id", uuid),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("findings_schema_version", sa.Text(), nullable=False),
        sa.Column("findings_payload", postgresql.JSONB(), nullable=False),
        sa.Column("findings_digest", digest, nullable=False),
        sa.Column("artifact_evidence_reference", sa.Text(), nullable=False),
        sa.Column("artifact_evidence_digest", digest, nullable=False),
        sa.Column("idempotency_receipt_id", uuid, nullable=False),
        sa.Column("audit_event_reference", sa.Text(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("reconciliation_result_id", name="pk_projection_reconciliation_results"),
        sa.UniqueConstraint("workspace_id", "environment_id", "reconciliation_result_id", name="uq_projection_reconciliation_results_scope_id"),
        sa.UniqueConstraint("workspace_id", "environment_id", "attempt_identity", name="uq_projection_reconciliation_results_attempt"),
        sa.UniqueConstraint("workspace_id", "environment_id", "projection_kind", "activation_generation", "projection_snapshot_id", "projection_activation_id", "reconciliation_result_id", name="uq_projection_reconciliation_results_target"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "projection_snapshot_id"],
            [f"{SCHEMA}.projection_snapshots.workspace_id", f"{SCHEMA}.projection_snapshots.environment_id", f"{SCHEMA}.projection_snapshots.projection_snapshot_id"],
            name="fk_projection_reconciliation_results_snapshot",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "projection_kind", "activation_generation", "projection_snapshot_id", "projection_activation_id", "predecessor_result_id"],
            [f"{SCHEMA}.projection_reconciliation_results.workspace_id", f"{SCHEMA}.projection_reconciliation_results.environment_id", f"{SCHEMA}.projection_reconciliation_results.projection_kind", f"{SCHEMA}.projection_reconciliation_results.activation_generation", f"{SCHEMA}.projection_reconciliation_results.projection_snapshot_id", f"{SCHEMA}.projection_reconciliation_results.projection_activation_id", f"{SCHEMA}.projection_reconciliation_results.reconciliation_result_id"],
            name="fk_projection_reconciliation_results_predecessor",
        ),
        sa.ForeignKeyConstraint(["idempotency_receipt_id"], [f"{SCHEMA}.idempotency_receipts.receipt_id"], name="fk_projection_reconciliation_results_idempotency"),
        sa.CheckConstraint("activation_generation >= 1", name="ck_projection_reconciliation_results_generation"),
        sa.CheckConstraint("outcome IN ('healthy', 'degraded', 'failed', 'interrupted')", name="ck_projection_reconciliation_results_outcome"),
        sa.CheckConstraint("attempt_digest ~ '^[0-9a-f]{64}$'", name="ck_projection_reconciliation_results_attempt_digest"),
        sa.CheckConstraint("findings_digest ~ '^[0-9a-f]{64}$'", name="ck_projection_reconciliation_results_findings_digest"),
        sa.CheckConstraint("artifact_evidence_digest ~ '^[0-9a-f]{64}$'", name="ck_projection_reconciliation_results_artifact_digest"),
        schema=SCHEMA,
    )

    for table in (
        "projection_validations",
        "projection_snapshots",
        "projection_snapshot_members",
        "projection_reconciliation_results",
    ):
        _tenant(table, allow_update=table == "projection_snapshots")
    for table in ("projection_validations", "projection_snapshot_members", "projection_reconciliation_results"):
        _immutable(table, "Projection snapshot evidence is immutable.")
    _receipt_scope_guard("projection_reconciliation_results")

    op.create_index("ix_projection_validations_candidate", "projection_validations", ["workspace_id", "environment_id", "projection_candidate_id", "decision"], schema=SCHEMA)
    op.create_index("ix_projection_snapshots_scope_state", "projection_snapshots", ["workspace_id", "environment_id", "projection_kind", "snapshot_state"], schema=SCHEMA)
    op.create_index("ix_projection_snapshot_members_snapshot", "projection_snapshot_members", ["workspace_id", "environment_id", "projection_snapshot_id", "disposition"], schema=SCHEMA)
    op.create_index("ix_projection_reconciliation_results_activation", "projection_reconciliation_results", ["workspace_id", "environment_id", "projection_kind", "activation_generation", "checked_at"], schema=SCHEMA)
    op.create_index("uq_projection_reconciliation_results_root", "projection_reconciliation_results", ["workspace_id", "environment_id", "projection_kind", "activation_generation", "projection_snapshot_id", "projection_activation_id"], unique=True, postgresql_where=sa.text("predecessor_result_id IS NULL"), schema=SCHEMA)
    op.create_index("uq_projection_reconciliation_results_successor", "projection_reconciliation_results", ["workspace_id", "environment_id", "predecessor_result_id"], unique=True, postgresql_where=sa.text("predecessor_result_id IS NOT NULL"), schema=SCHEMA)

    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.guard_projection_snapshot_initial_state() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ DECLARE candidate_state text; candidate_digest text; candidate_kind text; candidate_config uuid; candidate_boundary uuid; BEGIN
            IF NEW.snapshot_state IS DISTINCT FROM 'assembling' THEN
                RAISE EXCEPTION 'Snapshot headers must begin assembling.'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_projection_snapshot_initial_assembling';
            END IF;
            SELECT run.candidate_state, run.candidate_evidence_digest,
                   run.projection_kind, run.projection_config_version_id,
                   run.canonical_boundary_id
              INTO candidate_state, candidate_digest, candidate_kind,
                   candidate_config, candidate_boundary
              FROM {SCHEMA}.projection_runs AS run
             WHERE run.projection_run_id = NEW.projection_run_id
               AND run.projection_candidate_id = NEW.projection_candidate_id
               AND run.workspace_id = NEW.workspace_id
               AND run.environment_id = NEW.environment_id
             FOR KEY SHARE;
            IF candidate_state IS DISTINCT FROM 'sealed'
               OR candidate_digest IS DISTINCT FROM NEW.candidate_evidence_digest
               OR candidate_kind IS DISTINCT FROM NEW.projection_kind
               OR candidate_config IS DISTINCT FROM NEW.projection_config_version_id
               OR candidate_boundary IS DISTINCT FROM NEW.canonical_boundary_id
            THEN
                RAISE EXCEPTION 'Snapshot header does not match the sealed candidate.'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_projection_snapshots_candidate_lineage';
            END IF;
            RETURN NEW;
        END; $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.guard_projection_snapshot_initial_state() FROM PUBLIC")
    op.execute(f"CREATE TRIGGER trg_projection_snapshots_initial_assembling BEFORE INSERT ON {SCHEMA}.projection_snapshots FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_projection_snapshot_initial_state()")

    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.guard_projection_snapshot_finalization() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ BEGIN
            IF OLD.snapshot_state <> 'assembling' OR NEW.snapshot_state <> 'finalized'
               OR NEW.projection_snapshot_id IS DISTINCT FROM OLD.projection_snapshot_id
               OR NEW.projection_run_id IS DISTINCT FROM OLD.projection_run_id
               OR NEW.projection_candidate_id IS DISTINCT FROM OLD.projection_candidate_id
               OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
               OR NEW.environment_id IS DISTINCT FROM OLD.environment_id
               OR NEW.projection_kind IS DISTINCT FROM OLD.projection_kind
               OR NEW.projection_config_version_id IS DISTINCT FROM OLD.projection_config_version_id
               OR NEW.canonical_boundary_id IS DISTINCT FROM OLD.canonical_boundary_id
               OR NEW.predecessor_snapshot_id IS DISTINCT FROM OLD.predecessor_snapshot_id
               OR NEW.candidate_evidence_digest IS DISTINCT FROM OLD.candidate_evidence_digest
               OR NEW.manifest_digest IS DISTINCT FROM OLD.manifest_digest
               OR NEW.member_count IS DISTINCT FROM OLD.member_count
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
                RAISE EXCEPTION 'Projection snapshots are immutable except for finalization.'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_projection_snapshots_immutable';
            END IF;
            RETURN NEW;
        END; $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.guard_projection_snapshot_finalization() FROM PUBLIC")
    op.execute(f"CREATE TRIGGER trg_projection_snapshots_finalization_guard BEFORE UPDATE ON {SCHEMA}.projection_snapshots FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_projection_snapshot_finalization()")

    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.guard_projection_snapshot_member_insert() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ DECLARE snapshot_row record; boundary_revision uuid; partition_row record; receipt_row record; BEGIN
            SELECT * INTO snapshot_row FROM {SCHEMA}.projection_snapshots
             WHERE projection_snapshot_id = NEW.projection_snapshot_id
               AND workspace_id = NEW.workspace_id AND environment_id = NEW.environment_id
             FOR UPDATE;
            IF snapshot_row.snapshot_state IS DISTINCT FROM 'assembling' THEN
                RAISE EXCEPTION 'Snapshot membership is only writable while assembling.'
                    USING ERRCODE = '40001', CONSTRAINT = 'ck_projection_snapshot_members_assembling';
            END IF;
            IF snapshot_row.canonical_boundary_id IS DISTINCT FROM NEW.canonical_boundary_id
               OR snapshot_row.projection_snapshot_id IS DISTINCT FROM NEW.projection_snapshot_id
            THEN
                RAISE EXCEPTION 'Snapshot membership does not belong to its header.'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_projection_snapshot_members_snapshot_lineage';
            END IF;
            SELECT source_revision_id INTO boundary_revision
              FROM {SCHEMA}.canonical_boundary_members
             WHERE workspace_id = NEW.workspace_id
               AND environment_id = NEW.environment_id
               AND boundary_id = NEW.canonical_boundary_id
               AND source_object_id = NEW.canonical_boundary_member_id;
            IF boundary_revision IS NULL OR boundary_revision IS DISTINCT FROM NEW.source_revision_id
               OR NEW.source_object_id IS DISTINCT FROM NEW.canonical_boundary_member_id
            THEN
                RAISE EXCEPTION 'Snapshot membership does not match its boundary member.'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_projection_snapshot_members_boundary_lineage';
            END IF;
            IF NEW.disposition = 'included' THEN
                SELECT projection_run_id, projection_candidate_id,
                       expected_command_identity, expected_command_digest
                  INTO partition_row
                  FROM {SCHEMA}.projection_partitions
                 WHERE workspace_id = NEW.workspace_id
                   AND environment_id = NEW.environment_id
                   AND projection_partition_id = NEW.projection_partition_id;
                SELECT projection_run_id, projection_candidate_id,
                       projection_partition_id, command_identity, command_digest
                  INTO receipt_row
                  FROM {SCHEMA}.projection_receipts
                 WHERE workspace_id = NEW.workspace_id
                   AND environment_id = NEW.environment_id
                   AND projection_receipt_id = NEW.projection_receipt_id;
                IF partition_row IS NULL OR receipt_row IS NULL
                   OR partition_row.projection_run_id IS DISTINCT FROM snapshot_row.projection_run_id
                   OR partition_row.projection_candidate_id IS DISTINCT FROM snapshot_row.projection_candidate_id
                   OR receipt_row.projection_run_id IS DISTINCT FROM snapshot_row.projection_run_id
                   OR receipt_row.projection_candidate_id IS DISTINCT FROM snapshot_row.projection_candidate_id
                   OR receipt_row.projection_partition_id IS DISTINCT FROM NEW.projection_partition_id
                   OR receipt_row.command_identity IS DISTINCT FROM partition_row.expected_command_identity
                   OR receipt_row.command_digest IS DISTINCT FROM partition_row.expected_command_digest
                THEN
                    RAISE EXCEPTION 'Snapshot membership artifact lineage is invalid.'
                        USING ERRCODE = '23514', CONSTRAINT = 'ck_projection_snapshot_members_artifact_lineage';
                END IF;
            END IF;
            RETURN NEW;
        END; $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.guard_projection_snapshot_member_insert() FROM PUBLIC")
    op.execute(f"CREATE TRIGGER trg_projection_snapshot_members_assembling BEFORE INSERT ON {SCHEMA}.projection_snapshot_members FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_projection_snapshot_member_insert()")

    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.guard_projection_snapshot_evidence_insert() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ DECLARE state text; BEGIN
            SELECT candidate_state INTO state FROM {SCHEMA}.projection_runs
             WHERE projection_run_id = NEW.projection_run_id
               AND projection_candidate_id = NEW.projection_candidate_id
               AND workspace_id = NEW.workspace_id AND environment_id = NEW.environment_id
             FOR UPDATE;
            IF state IS DISTINCT FROM 'open' THEN
                RAISE EXCEPTION 'Projection candidate evidence is sealed.'
                    USING ERRCODE = '40001', CONSTRAINT = 'ck_projection_candidate_open';
            END IF;
            RETURN NEW;
        END; $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.guard_projection_snapshot_evidence_insert() FROM PUBLIC")
    op.execute(f"CREATE TRIGGER trg_projection_validations_candidate_open BEFORE INSERT ON {SCHEMA}.projection_validations FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_projection_snapshot_evidence_insert()")


def downgrade() -> None:
    runtime = _role("runtime_role")
    for table in ("projection_reconciliation_results", "projection_snapshot_members", "projection_snapshots", "projection_validations"):
        op.execute(f'REVOKE ALL PRIVILEGES ON TABLE {SCHEMA}."{table}" FROM {runtime}')
    op.execute(f"DROP TRIGGER trg_projection_validations_candidate_open ON {SCHEMA}.projection_validations")
    op.execute(f"DROP FUNCTION {SCHEMA}.guard_projection_snapshot_evidence_insert()")
    op.execute(f"DROP TRIGGER trg_projection_snapshot_members_assembling ON {SCHEMA}.projection_snapshot_members")
    op.execute(f"DROP FUNCTION {SCHEMA}.guard_projection_snapshot_member_insert()")
    op.execute(f"DROP TRIGGER trg_projection_snapshots_finalization_guard ON {SCHEMA}.projection_snapshots")
    op.execute(f"DROP FUNCTION {SCHEMA}.guard_projection_snapshot_finalization()")
    op.execute(f"DROP TRIGGER trg_projection_snapshots_initial_assembling ON {SCHEMA}.projection_snapshots")
    op.execute(f"DROP FUNCTION {SCHEMA}.guard_projection_snapshot_initial_state()")
    op.execute(f"DROP TRIGGER trg_projection_reconciliation_results_receipt_scope ON {SCHEMA}.projection_reconciliation_results")
    for table in ("projection_reconciliation_results", "projection_snapshot_members", "projection_validations"):
        op.execute(f"DROP TRIGGER trg_{table}_immutable ON {SCHEMA}.\"{table}\"")
        op.execute(f"DROP FUNCTION {SCHEMA}.reject_{table}_mutation()")
    op.drop_table("projection_reconciliation_results", schema=SCHEMA)
    op.drop_table("projection_snapshot_members", schema=SCHEMA)
    op.drop_table("projection_snapshots", schema=SCHEMA)
    op.drop_table("projection_validations", schema=SCHEMA)
    op.drop_constraint(
        "uq_canonical_boundary_members_scope_member",
        "canonical_boundary_members",
        schema=SCHEMA,
        type_="unique",
    )
