"""Persist the serialized backup fence, tickets and immutable recovery evidence."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261016_19"
down_revision = "20261015_18"
branch_labels = None
depends_on = None

SCHEMA = "spine"
COORDINATION_TABLES = (
    "backup_fence_leases",
    "fence_operation_tickets",
    "recovery_quiescence_evidence",
    "storage_recovery_manifests",
)
_SAFE = "^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def _scope_expression() -> str:
    setting = "pg_catalog.current_setting('spine.deployment_scope_id', true)"
    return (
        "deployment_scope_id = CASE WHEN "
        f"{setting} OPERATOR(pg_catalog.~) '{_SAFE}' THEN {setting} ELSE NULL END"
    )


def _scope_table(table: str, *, mutable: bool) -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    qualified = f'{SCHEMA}."{table}"'
    scope = _scope_expression()
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(
        f'CREATE POLICY "pol_{table}_tenant_isolation" ON {qualified} '
        f"FOR ALL TO {runtime} USING ({scope}) WITH CHECK ({scope})"
    )
    op.execute(
        f'CREATE POLICY "pol_{table}_migration_maintenance" ON {qualified} '
        f"FOR ALL TO {migration} USING (true) WITH CHECK (true)"
    )
    privileges = "SELECT, INSERT, UPDATE" if mutable else "SELECT, INSERT"
    op.execute(f"GRANT {privileges} ON TABLE {qualified} TO {runtime}")
    op.execute(
        f"REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {qualified} FROM {runtime}"
    )
    if not mutable:
        op.execute(f"REVOKE UPDATE ON TABLE {qualified} FROM {runtime}")


def _immutable_history() -> None:
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.reject_recovery_history_mutation() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ BEGIN
            RAISE EXCEPTION 'Recovery evidence is immutable.'
                USING ERRCODE = '23514', CONSTRAINT = 'ck_recovery_history_immutable';
        END; $function$
        """
    )
    for table in ("recovery_quiescence_evidence", "storage_recovery_manifests"):
        op.execute(
            f'CREATE TRIGGER "trg_{table}_immutable" BEFORE UPDATE OR DELETE ON '
            f'{SCHEMA}."{table}" FOR EACH ROW EXECUTE FUNCTION '
            f"{SCHEMA}.reject_recovery_history_mutation()"
        )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.reject_recovery_history_mutation() FROM PUBLIC"
    )


def upgrade() -> None:
    op.create_table(
        "backup_fence_leases",
        sa.Column("deployment_scope_id", sa.Text(), nullable=False),
        sa.Column("active_fence_epoch", sa.BigInteger()),
        sa.Column("fence_state", sa.String(32), nullable=False),
        sa.Column("cutoff_ticket_sequence", sa.BigInteger()),
        sa.Column("lease_owner", sa.Text()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("lease_renewal", sa.DateTime(timezone=True)),
        sa.Column("takeover_reference", sa.Text()),
        sa.Column("ticket_admission", sa.String(16), nullable=False),
        sa.Column("cleanup_release_progress", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("deployment_scope_id", name="pk_backup_fence_leases"),
        sa.CheckConstraint(
            f"deployment_scope_id ~ '{_SAFE}'",
            name="scope",
        ),
        sa.CheckConstraint(
            "fence_state IN ('inactive','requested','draining','quiescent','snapshotting','validating','releasing','released','release_degraded')",
            name="state",
        ),
        sa.CheckConstraint(
            "ticket_admission IN ('open','closed','blocked')",
            name="admission",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "fence_operation_tickets",
        sa.Column("ticket_id", sa.Text(), nullable=False),
        sa.Column("deployment_scope_id", sa.Text(), nullable=False),
        sa.Column("ticket_sequence", sa.BigInteger(), nullable=False),
        sa.Column("fence_epoch", sa.BigInteger()),
        sa.Column("operation_kind", sa.String(64), nullable=False),
        sa.Column("target_reference", sa.Text(), nullable=False),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(64), nullable=False),
        sa.Column("terminal_at", sa.DateTime(timezone=True)),
        sa.Column("safe_failure_category", sa.String(64)),
        sa.Column("correlation_id", sa.Text()),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("ticket_id", name="pk_fence_operation_tickets"),
        sa.UniqueConstraint(
            "deployment_scope_id",
            "ticket_sequence",
            name="uq_fence_operation_tickets_scope_sequence",
        ),
        sa.ForeignKeyConstraint(
            ["deployment_scope_id"],
            [f"{SCHEMA}.backup_fence_leases.deployment_scope_id"],
            name="fk_fence_operation_tickets_lease",
        ),
        sa.CheckConstraint("ticket_sequence > 0", name="sequence"),
        sa.CheckConstraint(
            f"ticket_id ~ '{_SAFE}' AND target_reference ~ '{_SAFE}'",
            name="identifiers",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_fence_operation_tickets_scope_state",
        "fence_operation_tickets",
        ["deployment_scope_id", "state", "ticket_sequence"],
        schema=SCHEMA,
    )
    op.create_table(
        "recovery_quiescence_evidence",
        sa.Column("deployment_scope_id", sa.Text(), nullable=False),
        sa.Column("fence_epoch", sa.BigInteger(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("cutoff_ticket_sequence", sa.BigInteger(), nullable=False),
        sa.Column("watermark", sa.Text(), nullable=False),
        sa.Column("pre_fence_terminal_ticket_count", sa.BigInteger(), nullable=False),
        sa.Column("indeterminate_ticket_count", sa.BigInteger(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verification_digest", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint(
            "deployment_scope_id",
            "fence_epoch",
            name="pk_recovery_quiescence_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["deployment_scope_id"],
            [f"{SCHEMA}.backup_fence_leases.deployment_scope_id"],
            name="fk_recovery_quiescence_evidence_lease",
        ),
        sa.CheckConstraint(
            f"deployment_scope_id ~ '{_SAFE}' AND watermark ~ '{_SAFE}'",
            name="identifiers",
        ),
        sa.CheckConstraint(
            "fence_epoch > 0 AND cutoff_ticket_sequence >= 0 "
            "AND pre_fence_terminal_ticket_count >= 0 "
            "AND indeterminate_ticket_count = 0",
            name="counts",
        ),
        sa.CheckConstraint(
            "verification_digest ~ '^[0-9a-f]{64}$'",
            name="digest",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "storage_recovery_manifests",
        sa.Column("recovery_set_id", sa.Text(), nullable=False),
        sa.Column("deployment_scope_id", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("profile", sa.String(32), nullable=False),
        sa.Column("manifest_status", sa.String(32), nullable=False),
        sa.Column("fence_epoch", sa.BigInteger(), nullable=False),
        sa.Column("cutoff_ticket_sequence", sa.BigInteger(), nullable=False),
        sa.Column("quiescent_evidence", postgresql.JSONB()),
        sa.Column("postgres_boundary", sa.Text(), nullable=False),
        sa.Column("postgres_recovery_watermark", sa.Text()),
        sa.Column("object_volume_generation", sa.Text(), nullable=False),
        sa.Column("postgres_component_state", sa.String(16), nullable=False),
        sa.Column("object_component_state", sa.String(16), nullable=False),
        sa.Column("postgres_backup_identity", sa.Text()),
        sa.Column("postgres_verification_reference", sa.Text()),
        sa.Column("object_verification_reference", sa.Text()),
        sa.Column("drained_operation_count", sa.BigInteger(), nullable=False),
        sa.Column("orphaned_operation_count", sa.BigInteger(), nullable=False),
        sa.Column("indeterminate_operation_count", sa.BigInteger(), nullable=False),
        sa.Column("integrity_algorithm", sa.String(16), nullable=False),
        sa.Column("manifest_digest", sa.String(64), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("start_watermark", sa.Text()),
        sa.Column("end_watermark", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("recovery_set_id", name="pk_storage_recovery_manifests"),
        sa.ForeignKeyConstraint(
            ["deployment_scope_id"],
            [f"{SCHEMA}.backup_fence_leases.deployment_scope_id"],
            name="fk_storage_recovery_manifests_lease",
        ),
        sa.CheckConstraint(
            "profile IN ('coordinated','best_effort')",
            name="profile",
        ),
        sa.CheckConstraint(
            "manifest_status IN ('building','incomplete','failed','coherent')",
            name="status",
        ),
        sa.CheckConstraint(
            "manifest_digest ~ '^[0-9a-f]{64}$' AND integrity_algorithm = 'sha256'",
            name="digest",
        ),
        sa.CheckConstraint(
            "drained_operation_count >= 0 AND orphaned_operation_count >= 0 AND indeterminate_operation_count >= 0",
            name="counts",
        ),
        sa.CheckConstraint(
            "NOT (profile = 'best_effort' AND manifest_status = 'coherent')",
            name="profile_status",
        ),
        sa.CheckConstraint(
            "manifest_status <> 'coherent' OR ("
            "quiescent_evidence IS NOT NULL "
            "AND postgres_recovery_watermark IS NOT NULL "
            "AND postgres_component_state = 'verified' "
            "AND object_component_state = 'verified' "
            "AND postgres_verification_reference IS NOT NULL "
            "AND object_verification_reference IS NOT NULL "
            "AND completed_at IS NOT NULL "
            "AND indeterminate_operation_count = 0)",
            name="coherent_evidence",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_storage_recovery_manifests_scope_created",
        "storage_recovery_manifests",
        ["deployment_scope_id", "created_at"],
        schema=SCHEMA,
    )
    _scope_table("backup_fence_leases", mutable=True)
    _scope_table("fence_operation_tickets", mutable=True)
    _scope_table("recovery_quiescence_evidence", mutable=False)
    _scope_table("storage_recovery_manifests", mutable=False)
    _immutable_history()


def downgrade() -> None:
    connection = op.get_bind()
    for table in COORDINATION_TABLES:
        if connection.execute(sa.text(f'SELECT 1 FROM {SCHEMA}."{table}" LIMIT 1')).scalar_one_or_none() is not None:
            raise RuntimeError("backup recovery records are historical and cannot be downgraded")
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_recovery_history_mutation() CASCADE")
    for table in reversed(COORDINATION_TABLES):
        op.drop_table(table, schema=SCHEMA)
