"""Persist append-only recovery evidence invalidations and chain links."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261017_20"
down_revision = "20261016_19"
branch_labels = None
depends_on = None

SCHEMA = "spine"
COORDINATION_TABLES = (
    "backup_fence_leases",
    "fence_operation_tickets",
    "recovery_quiescence_evidence",
    "storage_recovery_manifests",
    "recovery_evidence_invalidations",
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


def _scope_table(table: str) -> None:
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
    op.execute(f"GRANT SELECT, INSERT ON TABLE {qualified} TO {runtime}")
    op.execute(
        f"REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE "
        f"{qualified} FROM {runtime}"
    )


def upgrade() -> None:
    op.create_table(
        "recovery_evidence_invalidations",
        sa.Column("deployment_scope_id", sa.Text(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("recovery_set_id", sa.Text(), nullable=False),
        sa.Column("manifest_digest", sa.String(64), nullable=False),
        sa.Column("event_kind", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("replacement_recovery_set_id", sa.Text()),
        sa.Column("replacement_manifest_digest", sa.String(64)),
        sa.Column("replacement_validation_reference", sa.Text()),
        sa.Column("replacement_approval_reference", sa.Text()),
        sa.Column("previous_event_digest", sa.String(64)),
        sa.Column("event_digest", sa.String(64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "deployment_scope_id",
            "sequence",
            name="pk_recovery_evidence_invalidations",
        ),
        sa.ForeignKeyConstraint(
            ["deployment_scope_id"],
            [f"{SCHEMA}.backup_fence_leases.deployment_scope_id"],
            name="fk_recovery_evidence_invalidations_lease",
        ),
        sa.CheckConstraint(
            f"deployment_scope_id ~ '{_SAFE}' AND recovery_set_id ~ '{_SAFE}' "
            f"AND reason_code ~ '{_SAFE}'",
            name="identifiers",
        ),
        sa.CheckConstraint("sequence > 0", name="sequence_positive"),
        sa.CheckConstraint(
            "(sequence = 1 AND previous_event_digest IS NULL) OR "
            "(sequence > 1 AND previous_event_digest ~ '^[0-9a-f]{64}$')",
            name="predecessor",
        ),
        sa.CheckConstraint(
            "manifest_digest ~ '^[0-9a-f]{64}$' AND event_digest ~ '^[0-9a-f]{64}$' "
            "AND (previous_event_digest IS NULL OR previous_event_digest ~ '^[0-9a-f]{64}$')",
            name="digests",
        ),
        sa.CheckConstraint(
            "event_kind IN ('invalidated','replaced')",
            name="event_kind",
        ),
        sa.CheckConstraint(
            "(event_kind = 'invalidated' AND replacement_recovery_set_id IS NULL "
            "AND replacement_manifest_digest IS NULL "
            "AND replacement_validation_reference IS NULL "
            "AND replacement_approval_reference IS NULL) OR "
            "(event_kind = 'replaced' AND replacement_recovery_set_id IS NOT NULL "
            "AND replacement_manifest_digest IS NOT NULL "
            "AND replacement_validation_reference IS NOT NULL "
            "AND replacement_approval_reference IS NOT NULL "
            "AND replacement_recovery_set_id <> recovery_set_id "
            "AND replacement_manifest_digest <> manifest_digest)",
            name="replacement",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_recovery_evidence_invalidations_scope_digest",
        "recovery_evidence_invalidations",
        ["deployment_scope_id", "event_digest"],
        schema=SCHEMA,
        unique=True,
    )
    _scope_table("recovery_evidence_invalidations")
    op.execute(
        f'CREATE TRIGGER "trg_recovery_evidence_invalidations_immutable" '
        f"BEFORE UPDATE OR DELETE ON {SCHEMA}.\"recovery_evidence_invalidations\" "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_recovery_history_mutation()"
    )


def downgrade() -> None:
    connection = op.get_bind()
    for table in COORDINATION_TABLES:
        if (
            connection.execute(
                sa.text(f'SELECT 1 FROM {SCHEMA}."{table}" LIMIT 1')
            ).scalar_one_or_none()
            is not None
        ):
            raise RuntimeError("recovery evidence records are historical and cannot be downgraded")
    op.drop_index(
        "ix_recovery_evidence_invalidations_scope_digest",
        table_name="recovery_evidence_invalidations",
        schema=SCHEMA,
    )
    op.drop_table("recovery_evidence_invalidations", schema=SCHEMA)
