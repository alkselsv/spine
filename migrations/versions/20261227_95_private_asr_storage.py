"""Persist restricted private ASR artifacts and report receipts atomically."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261228_96"
down_revision = "20261227_95"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, attribute))


def _tenant_table(name: str, *, mutable: bool, purpose_scoped: bool = False) -> None:
    runtime_role = _role("runtime_role")
    migration_role = _role("migration_role")
    op.execute(f"ALTER TABLE {SCHEMA}.{name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {SCHEMA}.{name} FORCE ROW LEVEL SECURITY")
    purpose_predicate = (
        " AND purpose = nullif(current_setting('spine.purpose', true), '')"
        if purpose_scoped else ""
    )
    op.execute(
        f"CREATE POLICY pol_{name}_tenant_isolation ON {SCHEMA}.{name} "
        f"FOR ALL TO {runtime_role} USING (workspace_id = "
        "nullif(current_setting('spine.workspace_id', true), '')::uuid AND "
        f"environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid{purpose_predicate}) "
        "WITH CHECK (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        f"AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid{purpose_predicate})"
    )
    op.execute(
        f"CREATE POLICY pol_{name}_migration_maintenance ON {SCHEMA}.{name} "
        f"FOR ALL TO {migration_role} USING (true) WITH CHECK (true)"
    )
    grants = "SELECT, INSERT, UPDATE" if mutable else "SELECT, INSERT"
    op.execute(f"GRANT {grants} ON TABLE {SCHEMA}.{name} TO {runtime_role}")
    op.execute(
        f"REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {SCHEMA}.{name} "
        f"FROM {runtime_role}"
    )


def upgrade() -> None:
    op.create_table(
        "private_asr_artifacts",
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("report_id", postgresql.UUID(as_uuid=True)),
        sa.Column("idempotency_key", sa.Text()),
        sa.Column("source_object_id", postgresql.UUID(as_uuid=True)),
        sa.Column("source_revision_id", postgresql.UUID(as_uuid=True)),
        sa.Column("source_revision_digest", sa.String(64)),
        sa.Column("content_sha256", sa.String(64)),
        sa.Column("byte_length", sa.BigInteger(), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("retention_profile_ref", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("audit_receipt_ref", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("admitted_record", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tombstoned_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("workspace_id", "environment_id", "artifact_id",
                                name="pk_private_asr_artifacts"),
        sa.ForeignKeyConstraint(["audit_receipt_ref"], [f"{SCHEMA}.audit_events.audit_event_id"],
                                name="fk_private_asr_artifact_audit"),
        sa.UniqueConstraint("workspace_id", "environment_id", "idempotency_key",
                            name="uq_private_asr_idempotency"),
        sa.CheckConstraint("kind IN ('staging','original','annotation','report','aggregate')",
                           name="ck_private_asr_artifact_kind"),
        sa.CheckConstraint("status IN ('staging','committed','tombstoned')",
                           name="ck_private_asr_artifact_status"),
        sa.CheckConstraint("byte_length >= 0", name="ck_private_asr_byte_length"),
        schema=SCHEMA,
    )
    op.create_index("ix_private_asr_source", "private_asr_artifacts",
                    ["workspace_id", "environment_id", "source_revision_id"], schema=SCHEMA)
    op.create_table(
        "private_asr_source_fences",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("source_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("epoch", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "environment_id", "source_revision_id",
                                name="pk_private_asr_source_fences"),
        sa.CheckConstraint("reason ~ '^[a-z][a-z0-9_.:-]{0,63}$'",
                           name="ck_private_asr_fence_reason"),
        sa.CheckConstraint("purpose = 'asr_model_evaluation'",
                           name="ck_private_asr_fence_purpose"),
        schema=SCHEMA,
    )
    op.create_table(
        "private_asr_report_sources",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("report_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint("purpose = 'asr_model_evaluation'",
                           name="ck_private_asr_report_sources_purpose"),
        sa.PrimaryKeyConstraint("workspace_id", "environment_id", "report_id", "source_record_id",
                                name="pk_private_asr_report_sources"),
        schema=SCHEMA,
    )
    op.create_table(
        "private_asr_aggregate_exports",
        sa.Column("export_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("privacy_policy_version", sa.Text(), nullable=False),
        sa.Column("audit_receipt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "environment_id", "export_id",
                                name="pk_private_asr_aggregate_exports"),
        sa.ForeignKeyConstraint(["audit_receipt_id"], [f"{SCHEMA}.audit_events.audit_event_id"],
                                name="fk_private_asr_aggregate_export_audit"),
        sa.CheckConstraint("purpose = 'asr_model_evaluation'",
                           name="ck_private_asr_aggregate_exports_purpose"),
        schema=SCHEMA,
    )
    for table, mutable in (("private_asr_artifacts", True),
                           ("private_asr_source_fences", True),
                           ("private_asr_report_sources", False),
                           ("private_asr_aggregate_exports", False)):
        purpose_scoped = True
        _tenant_table(table, mutable=mutable, purpose_scoped=purpose_scoped)


def downgrade() -> None:
    for table in ("private_asr_aggregate_exports",
                  "private_asr_report_sources", "private_asr_source_fences",
                  "private_asr_artifacts"):
        op.drop_table(table, schema=SCHEMA)
