"""Persist restricted private ASR artifacts and report receipts atomically."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20270101_100"
down_revision = "20261231_99"
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
    if name == "private_asr_artifacts":
        op.execute(f"GRANT SELECT, INSERT ON TABLE {SCHEMA}.{name} TO {runtime_role}")
        op.execute(
            f"GRANT UPDATE (status, payload, byte_length, content_sha256, expires_at, "
            f"tombstoned_at, audit_receipt_ref) ON TABLE {SCHEMA}.{name} TO {runtime_role}"
        )
    elif name == "private_asr_source_fences":
        op.execute(f"GRANT SELECT, INSERT ON TABLE {SCHEMA}.{name} TO {runtime_role}")
        op.execute(
            f"GRANT UPDATE (revoked, epoch, reason, updated_at) "
            f"ON TABLE {SCHEMA}.{name} TO {runtime_role}"
        )
    else:
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
        sa.CheckConstraint(
            "kind <> 'staging' OR status IN ('staging','tombstoned')",
            name="ck_private_asr_staging_not_committed",
        ),
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
        sa.CheckConstraint("revoked AND epoch >= 1",
                           name="ck_private_asr_fence_monotonic_origin"),
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
        sa.Column("report_artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("privacy_policy_version", sa.Text(), nullable=False),
        sa.Column("privacy_policy_approval_version", sa.Text(), nullable=False),
        sa.Column("retention_profile_ref", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("retention_disposition", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("audit_receipt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "environment_id", "export_id",
                                name="pk_private_asr_aggregate_exports"),
        sa.UniqueConstraint("workspace_id", "environment_id", "idempotency_key",
                            name="uq_private_asr_aggregate_exports_idempotency"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "report_artifact_id"],
            [f"{SCHEMA}.private_asr_artifacts.workspace_id",
             f"{SCHEMA}.private_asr_artifacts.environment_id",
             f"{SCHEMA}.private_asr_artifacts.artifact_id"],
            name="fk_private_asr_aggregate_export_report",
        ),
        sa.ForeignKeyConstraint(["audit_receipt_id"], [f"{SCHEMA}.audit_events.audit_event_id"],
                                name="fk_private_asr_aggregate_export_audit"),
        sa.CheckConstraint("purpose = 'asr_model_evaluation'",
                           name="ck_private_asr_aggregate_exports_purpose"),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_private_asr_report_identity",
        "private_asr_artifacts",
        ["workspace_id", "environment_id", "report_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("kind = 'report' AND report_id IS NOT NULL"),
    )
    op.execute(f"""
        CREATE FUNCTION {SCHEMA}.guard_private_asr_artifact_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF OLD.status = 'tombstoned' THEN
                RAISE EXCEPTION 'private ASR tombstone is immutable'
                    USING ERRCODE = '42501';
            END IF;

            IF NEW.status = 'tombstoned' THEN
                IF NEW.tombstoned_at IS NULL
                   OR NEW.payload IS DISTINCT FROM ''::bytea
                   OR (to_jsonb(NEW) - ARRAY['status', 'tombstoned_at', 'payload'])
                      IS DISTINCT FROM
                      (to_jsonb(OLD) - ARRAY['status', 'tombstoned_at', 'payload']) THEN
                    RAISE EXCEPTION 'private ASR tombstone transition is invalid'
                        USING ERRCODE = '42501';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM {SCHEMA}.audit_events AS audit
                    WHERE audit.workspace_id = OLD.workspace_id
                      AND audit.environment_id = OLD.environment_id
                      AND audit.target_type = 'private_asr_artifact'
                      AND audit.target_id = OLD.artifact_id
                      AND audit.event_type = 'command.outcome'
                      AND audit.outcome = 'completed'
                      AND audit.reason IN (
                          'private_asr_artifact_expired',
                          'private_asr_staging_discarded',
                          'private_asr_source_revoked'
                      )
                ) THEN
                    RAISE EXCEPTION 'private ASR tombstone requires canonical audit'
                        USING ERRCODE = '42501';
                END IF;
                RETURN NEW;
            END IF;

            IF OLD.kind = 'staging' THEN
                IF NEW.tombstoned_at IS NOT NULL
                   OR (to_jsonb(NEW) - ARRAY['status', 'tombstoned_at', 'payload',
                                               'byte_length', 'content_sha256', 'expires_at'])
                      IS DISTINCT FROM
                      (to_jsonb(OLD) - ARRAY['status', 'tombstoned_at', 'payload',
                                              'byte_length', 'content_sha256', 'expires_at']) THEN
                    RAISE EXCEPTION 'private ASR staging identity is immutable'
                        USING ERRCODE = '42501';
                END IF;
                IF NEW.status = 'staging' THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'private ASR staging transition is invalid'
                    USING ERRCODE = '42501';
            END IF;

            IF OLD.status = 'staging' AND NEW.status = 'committed'
               AND (to_jsonb(NEW) - ARRAY['status', 'audit_receipt_ref'])
                   IS NOT DISTINCT FROM
                   (to_jsonb(OLD) - ARRAY['status', 'audit_receipt_ref']) THEN
                RETURN NEW;
            END IF;

            RAISE EXCEPTION 'committed private ASR artifact is immutable'
                USING ERRCODE = '42501';
        END;
        $$
    """)
    op.execute(f"""
        CREATE TRIGGER trg_private_asr_artifact_immutable
        BEFORE UPDATE ON {SCHEMA}.private_asr_artifacts
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_private_asr_artifact_update()
    """)
    op.execute(f"""
        CREATE FUNCTION {SCHEMA}.guard_private_asr_source_fence_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.revoked IS NOT TRUE OR NEW.epoch <> OLD.epoch + 1
               OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
               OR NEW.environment_id IS DISTINCT FROM OLD.environment_id
               OR NEW.purpose IS DISTINCT FROM OLD.purpose
               OR NEW.source_revision_id IS DISTINCT FROM OLD.source_revision_id THEN
                RAISE EXCEPTION 'private ASR source fence must advance monotonically'
                    USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute(f"""
        CREATE TRIGGER trg_private_asr_source_fence_monotonic
        BEFORE UPDATE ON {SCHEMA}.private_asr_source_fences
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_private_asr_source_fence_update()
    """)
    for table, mutable in (("private_asr_artifacts", True),
                           ("private_asr_source_fences", False),
                           ("private_asr_report_sources", False),
                           ("private_asr_aggregate_exports", False)):
        purpose_scoped = True
        _tenant_table(table, mutable=mutable, purpose_scoped=purpose_scoped)
    # An aggregate is visible only while its source report is still an active,
    # unexpired private artifact.  This makes source-revocation tombstones and
    # report expiry immediately fence aggregate reads at the database boundary.
    # The application still rechecks current privacy/release authority before
    # publication; external authority revocation requires the shared #813 fence.
    runtime_role = _role("runtime_role")
    op.execute(f"""
        CREATE POLICY pol_private_asr_aggregate_exports_live_report
        ON {SCHEMA}.private_asr_aggregate_exports
        AS RESTRICTIVE FOR SELECT TO {runtime_role}
        USING (
            expires_at > statement_timestamp()
            AND EXISTS (
                SELECT 1
                FROM {SCHEMA}.private_asr_artifacts AS report
                WHERE report.workspace_id = private_asr_aggregate_exports.workspace_id
                  AND report.environment_id = private_asr_aggregate_exports.environment_id
                  AND report.artifact_id = private_asr_aggregate_exports.report_artifact_id
                  AND report.kind = 'report'
                  AND report.status = 'committed'
                  AND report.tombstoned_at IS NULL
                  AND report.expires_at > statement_timestamp()
            )
        )
    """)


def downgrade() -> None:
    op.execute(
        f"DROP POLICY pol_private_asr_aggregate_exports_live_report "
        f"ON {SCHEMA}.private_asr_aggregate_exports"
    )
    op.execute(f"DROP TRIGGER trg_private_asr_artifact_immutable ON {SCHEMA}.private_asr_artifacts")
    op.execute(f"DROP FUNCTION {SCHEMA}.guard_private_asr_artifact_update()")
    op.execute(
        f"DROP TRIGGER trg_private_asr_source_fence_monotonic "
        f"ON {SCHEMA}.private_asr_source_fences"
    )
    op.execute(f"DROP FUNCTION {SCHEMA}.guard_private_asr_source_fence_update()")
    op.drop_index("uq_private_asr_report_identity", table_name="private_asr_artifacts", schema=SCHEMA)
    for table in ("private_asr_aggregate_exports",
                  "private_asr_report_sources", "private_asr_source_fences",
                  "private_asr_artifacts"):
        op.drop_table(table, schema=SCHEMA)
