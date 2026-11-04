"""Persist email attachment processing dispositions and exact lineage."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings

revision = "20270108_107"
down_revision = "20270107_106"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, attribute))


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "email_attachment_processing_receipts",
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("processing_receipt_id", uuid, nullable=False),
        sa.Column("attachment_artifact_id", uuid, nullable=False),
        sa.Column("processor_profile", sa.Text(), nullable=False),
        sa.Column("authority_digest", sa.String(64), nullable=False),
        sa.Column("disposition", sa.Text(), nullable=False),
        sa.Column("child_source_object_id", uuid),
        sa.Column("child_source_revision_id", uuid),
        sa.Column("child_processing_receipt_id", uuid),
        sa.Column("parsed_artifact_id", uuid),
        sa.Column("parsed_artifact_digest", sa.String(64)),
        sa.Column("receipt_digest", sa.String(64), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "environment_id",
            "processing_receipt_id",
            name="pk_email_attachment_processing_receipts",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "attachment_artifact_id",
            "processor_profile",
            "authority_digest",
            name="uq_email_attachment_processing_receipts_artifact_profile",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "attachment_artifact_id"],
            [
                f"{SCHEMA}.email_attachment_artifacts.workspace_id",
                f"{SCHEMA}.email_attachment_artifacts.environment_id",
                f"{SCHEMA}.email_attachment_artifacts.artifact_id",
            ],
            name="fk_email_attachment_processing_receipts_attachment",
        ),
        sa.ForeignKeyConstraint(
            [
                "workspace_id",
                "environment_id",
                "child_source_object_id",
                "child_source_revision_id",
            ],
            [
                f"{SCHEMA}.source_revisions.workspace_id",
                f"{SCHEMA}.source_revisions.environment_id",
                f"{SCHEMA}.source_revisions.source_object_id",
                f"{SCHEMA}.source_revisions.revision_id",
            ],
            name="fk_email_attachment_processing_receipts_child_revision",
        ),
        sa.ForeignKeyConstraint(
            ["child_processing_receipt_id"],
            [f"{SCHEMA}.processing_receipts.processing_receipt_id"],
            name="fk_email_attachment_processing_receipts_child_processing",
        ),
        sa.ForeignKeyConstraint(
            ["parsed_artifact_id"],
            [f"{SCHEMA}.parsed_documents.artifact_id"],
            name="fk_email_attachment_processing_receipts_parsed_artifact",
        ),
        sa.CheckConstraint(
            "disposition IN ('processed', 'partial', 'unsupported', 'dangerous', "
            "'missing', 'oversized', 'policy_denied', 'failed')",
            name="ck_email_attachment_processing_receipts_disposition",
        ),
        sa.CheckConstraint(
            "receipt_digest ~ '^[0-9a-f]{64}$' AND authority_digest ~ '^[0-9a-f]{64}$'",
            name="ck_email_attachment_processing_receipts_digest",
        ),
        sa.CheckConstraint(
            "(child_source_object_id IS NULL) = (child_source_revision_id IS NULL) "
            "AND (child_processing_receipt_id IS NULL OR child_source_revision_id IS NOT NULL)",
            name="ck_email_attachment_processing_receipts_child_lineage",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(record) = 'object'",
            name="ck_email_attachment_processing_receipts_record",
        ),
        sa.CheckConstraint(
            "record->>'processing_receipt_id' = processing_receipt_id::text "
            "AND record->>'attachment_artifact_id' = attachment_artifact_id::text "
            "AND record->>'workspace_id' = workspace_id::text "
            "AND record->>'environment_id' = environment_id::text "
            "AND record->>'processor_profile' = processor_profile "
            "AND record->>'authority_digest' = authority_digest "
            "AND record->>'disposition' = disposition "
            "AND (record->>'child_source_object_id') IS NOT DISTINCT FROM child_source_object_id::text "
            "AND (record->>'child_source_revision_id') IS NOT DISTINCT FROM child_source_revision_id::text "
            "AND (record->>'child_processing_receipt_id') IS NOT DISTINCT FROM child_processing_receipt_id::text "
            "AND (record->>'parsed_artifact_id') IS NOT DISTINCT FROM parsed_artifact_id::text "
            "AND (record->>'parsed_artifact_digest') IS NOT DISTINCT FROM parsed_artifact_digest "
            "AND (record->>'completed_at')::timestamptz = completed_at "
            "AND record->>'receipt_sha256' = receipt_digest",
            name="ck_email_attachment_processing_receipts_record_binding",
        ),
        schema=SCHEMA,
    )
    table = f"{SCHEMA}.email_attachment_processing_receipts"
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    scope = (
        "workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid"
    )
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE FUNCTION {SCHEMA}.validate_email_attachment_processing_lineage() "
        "RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER "
        "SET search_path = pg_catalog, spine AS $fn$ BEGIN "
        "IF NOT EXISTS ("
        f"SELECT 1 FROM {SCHEMA}.email_attachment_artifacts AS attachment "
        f"JOIN {SCHEMA}.source_revisions AS parent_revision "
        "ON parent_revision.workspace_id = attachment.workspace_id "
        "AND parent_revision.environment_id = attachment.environment_id "
        "AND parent_revision.source_object_id = attachment.source_object_id "
        "AND parent_revision.revision_id = attachment.source_revision_id "
        "WHERE attachment.workspace_id = NEW.workspace_id "
        "AND attachment.environment_id = NEW.environment_id "
        "AND attachment.artifact_id = NEW.attachment_artifact_id "
        "AND attachment.source_object_id = (NEW.record->>'parent_source_object_id')::uuid "
        "AND attachment.source_revision_id = (NEW.record->>'parent_source_revision_id')::uuid "
        "AND attachment.canonical_acceptance_id = (NEW.record->>'parent_canonical_acceptance_id')::uuid "
        "AND attachment.artifact_digest = NEW.record->>'attachment_artifact_digest' "
        "AND attachment.mime_part_path = NEW.record->>'mime_part_path' "
        "AND attachment.content_sha256 = NEW.record->>'attachment_content_sha256' "
        "AND attachment.record->>'byte_length' = NEW.record->>'attachment_byte_length' "
        "AND attachment.record->>'media_type' = NEW.record->>'media_type' "
        "AND parent_revision.revision_digest = NEW.record->>'parent_source_revision_digest') THEN "
        "RAISE EXCEPTION 'Attachment processing evidence does not match its retained artifact.' "
        "USING ERRCODE = '23503'; END IF; "
        "IF NEW.child_processing_receipt_id IS NOT NULL AND NOT EXISTS ("
        f"SELECT 1 FROM {SCHEMA}.processing_receipts AS receipt "
        "WHERE receipt.processing_receipt_id = NEW.child_processing_receipt_id "
        "AND receipt.workspace_id = NEW.workspace_id "
        "AND receipt.environment_id = NEW.environment_id "
        "AND receipt.source_object_id = NEW.child_source_object_id "
        "AND receipt.source_revision_id = NEW.child_source_revision_id "
        "AND (NEW.parsed_artifact_id IS NULL OR ("
        "receipt.output_reference = NEW.parsed_artifact_id "
        "AND receipt.output_digest = NEW.parsed_artifact_digest))) THEN "
        "RAISE EXCEPTION 'Child processing lineage is outside the receipt scope.' "
        "USING ERRCODE = '23503'; END IF; "
        "IF NEW.parsed_artifact_id IS NOT NULL AND NOT EXISTS ("
        f"SELECT 1 FROM {SCHEMA}.parsed_documents AS artifact "
        "WHERE artifact.artifact_id = NEW.parsed_artifact_id "
        "AND artifact.workspace_id = NEW.workspace_id "
        "AND artifact.environment_id = NEW.environment_id "
        "AND artifact.source_object_id = NEW.child_source_object_id "
        "AND artifact.source_revision_id = NEW.child_source_revision_id "
        "AND artifact.artifact_digest = NEW.parsed_artifact_digest) THEN "
        "RAISE EXCEPTION 'Parsed artifact lineage is outside the receipt scope.' "
        "USING ERRCODE = '23503'; END IF; RETURN NEW; END; $fn$"
    )
    op.execute(
        f"CREATE TRIGGER trg_email_attachment_processing_lineage "
        f"BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION "
        f"{SCHEMA}.validate_email_attachment_processing_lineage()"
    )
    op.execute(
        f"CREATE POLICY pol_email_attachment_processing_receipts_tenant_isolation "
        f"ON {table} FOR ALL TO {runtime} USING ({scope}) WITH CHECK ({scope})"
    )
    op.execute(
        f"CREATE POLICY pol_email_attachment_processing_receipts_migration_maintenance "
        f"ON {table} FOR ALL TO {migration} USING (true) WITH CHECK (true)"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE {table} TO {runtime}")
    op.execute(
        f"REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {table} FROM {runtime}"
    )
    op.execute(
        f"CREATE FUNCTION {SCHEMA}.reject_email_attachment_processing_receipt_mutation() "
        "RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER "
        "SET search_path = pg_catalog, spine AS $fn$ "
        "BEGIN RAISE EXCEPTION 'Email attachment processing history is immutable.' "
        "USING ERRCODE = '23514'; END; $fn$"
    )
    op.execute(
        f"CREATE TRIGGER trg_email_attachment_processing_receipt_immutable "
        f"BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION "
        f"{SCHEMA}.reject_email_attachment_processing_receipt_mutation()"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.reject_email_attachment_processing_receipt_mutation() "
        f"FROM PUBLIC, {runtime}"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.validate_email_attachment_processing_lineage() "
        f"FROM PUBLIC, {runtime}"
    )


def downgrade() -> None:
    op.execute(
        f"DROP TRIGGER trg_email_attachment_processing_lineage "
        f"ON {SCHEMA}.email_attachment_processing_receipts"
    )
    op.execute(f"DROP FUNCTION {SCHEMA}.validate_email_attachment_processing_lineage()")
    op.execute(
        f"DROP TRIGGER trg_email_attachment_processing_receipt_immutable "
        f"ON {SCHEMA}.email_attachment_processing_receipts"
    )
    op.execute(
        f"DROP FUNCTION {SCHEMA}.reject_email_attachment_processing_receipt_mutation()"
    )
    op.drop_table("email_attachment_processing_receipts", schema=SCHEMA)
