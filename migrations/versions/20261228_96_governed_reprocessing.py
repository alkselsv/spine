"""Bind governed process-only reprocessing to original canonical evidence."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261228_96"
down_revision = "20261227_95"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_unique_constraint(
        "uq_canonical_acceptances_reprocess_authority",
        "canonical_acceptances",
        ["workspace_id", "environment_id", "source_object_id", "canonical_acceptance_id", "source_revision_id", "accepted_generation", "processing_admission_id", "access_policy_version_id"],
        schema=SCHEMA,
    )
    op.create_unique_constraint(
        "uq_processing_admissions_reprocess_evidence",
        "source_processing_admissions",
        ["workspace_id", "environment_id", "source_object_id", "source_revision_id", "processing_admission_id", "receipt_association_id", "verified_sha256", "verified_byte_length", "lifecycle_eligibility_reference", "lifecycle_eligibility_generation", "retention_applicability_reference", "retention_applicability_generation"],
        schema=SCHEMA,
    )
    op.create_unique_constraint(
        "uq_source_receipt_associations_reprocess_receipt",
        "source_revision_receipt_associations",
        ["association_id", "workspace_id", "environment_id", "source_object_id", "revision_id", "original_receipt_id", "original_sha256", "byte_length"],
        schema=SCHEMA,
    )

    op.add_column(
        "process_only_operation_lineages",
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        schema=SCHEMA,
    )
    for name in (
        "canonical_acceptance_id",
        "processing_admission_id",
        "receipt_association_id",
        "processing_policy_version_id",
        "lifecycle_eligibility_reference",
        "retention_applicability_reference",
        "process_only_authorization_id",
        "authorization_audit_event_id",
    ):
        op.add_column(
            "process_only_operation_lineages",
            sa.Column(name, uuid),
            schema=SCHEMA,
        )
    op.add_column(
        "process_only_operation_lineages",
        sa.Column("canonical_accepted_generation", sa.Integer()),
        schema=SCHEMA,
    )
    op.add_column(
        "process_only_operation_lineages",
        sa.Column("verified_byte_length", sa.BigInteger()),
        schema=SCHEMA,
    )
    op.add_column(
        "process_only_operation_lineages",
        sa.Column("lifecycle_eligibility_generation", sa.Integer()),
        schema=SCHEMA,
    )
    op.add_column(
        "process_only_operation_lineages",
        sa.Column("retention_applicability_generation", sa.Integer()),
        schema=SCHEMA,
    )
    op.add_column(
        "process_only_operation_lineages",
        sa.Column("process_only_authorization_version", sa.String(128)),
        schema=SCHEMA,
    )
    op.add_column(
        "process_only_operation_lineages",
        sa.Column("process_only_authorization_digest", sa.String(64)),
        schema=SCHEMA,
    )
    op.add_column(
        "process_only_operation_lineages",
        sa.Column("authorized_at", sa.DateTime(timezone=True)),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        op.f("ck_process_only_lineages_governed_reprocess"),
        "process_only_operation_lineages",
        """
        (schema_version = 1 AND canonical_acceptance_id IS NULL
          AND canonical_accepted_generation IS NULL
          AND processing_admission_id IS NULL AND receipt_association_id IS NULL
          AND verified_byte_length IS NULL AND processing_policy_version_id IS NULL
          AND lifecycle_eligibility_reference IS NULL
          AND lifecycle_eligibility_generation IS NULL
          AND retention_applicability_reference IS NULL
          AND retention_applicability_generation IS NULL
          AND process_only_authorization_id IS NULL
          AND process_only_authorization_version IS NULL
          AND process_only_authorization_digest IS NULL
          AND authorization_audit_event_id IS NULL AND authorized_at IS NULL)
        OR
        (schema_version = 2 AND trigger_kind = 'reprocess'
          AND canonical_acceptance_id IS NOT NULL
          AND canonical_accepted_generation >= 1
          AND processing_admission_id IS NOT NULL
          AND receipt_association_id IS NOT NULL
          AND verified_byte_length >= 0
          AND processing_policy_version_id IS NOT NULL
          AND lifecycle_eligibility_reference IS NOT NULL
          AND lifecycle_eligibility_generation >= 1
          AND retention_applicability_reference IS NOT NULL
          AND retention_applicability_generation >= 1
          AND authorization_audit_event_id IS NOT NULL AND authorized_at IS NOT NULL
          AND ((process_only_authorization_id IS NULL
                AND process_only_authorization_version IS NULL
                AND process_only_authorization_digest IS NULL)
               OR (process_only_authorization_id IS NOT NULL
                   AND process_only_authorization_version IS NOT NULL
                   AND process_only_authorization_digest ~ '^[0-9a-f]{64}$')))
        """,
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_process_only_lineages_canonical_acceptance",
        "process_only_operation_lineages",
        "canonical_acceptances",
        ["workspace_id", "environment_id", "source_object_id", "canonical_acceptance_id", "source_revision_id", "canonical_accepted_generation", "processing_admission_id", "processing_policy_version_id"],
        ["workspace_id", "environment_id", "source_object_id", "canonical_acceptance_id", "source_revision_id", "accepted_generation", "processing_admission_id", "access_policy_version_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_process_only_lineages_processing_admission",
        "process_only_operation_lineages",
        "source_processing_admissions",
        ["workspace_id", "environment_id", "source_object_id", "source_revision_id", "processing_admission_id", "receipt_association_id", "original_receipt_digest", "verified_byte_length", "lifecycle_eligibility_reference", "lifecycle_eligibility_generation", "retention_applicability_reference", "retention_applicability_generation"],
        ["workspace_id", "environment_id", "source_object_id", "source_revision_id", "processing_admission_id", "receipt_association_id", "verified_sha256", "verified_byte_length", "lifecycle_eligibility_reference", "lifecycle_eligibility_generation", "retention_applicability_reference", "retention_applicability_generation"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_process_only_lineages_receipt_association",
        "process_only_operation_lineages",
        "source_revision_receipt_associations",
        ["receipt_association_id", "workspace_id", "environment_id", "source_object_id", "source_revision_id", "original_receipt_id", "original_receipt_digest", "verified_byte_length"],
        ["association_id", "workspace_id", "environment_id", "source_object_id", "revision_id", "original_receipt_id", "original_sha256", "byte_length"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_process_only_lineages_processing_policy",
        "process_only_operation_lineages",
        "access_policy_versions",
        ["workspace_id", "environment_id", "source_object_id", "processing_policy_version_id"],
        ["workspace_id", "environment_id", "source_object_id", "access_policy_version_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_process_only_lineages_lifecycle",
        "process_only_operation_lineages",
        "lifecycle_decisions",
        ["workspace_id", "environment_id", "source_object_id", "lifecycle_eligibility_reference", "lifecycle_eligibility_generation"],
        ["workspace_id", "environment_id", "source_object_id", "decision_id", "generation"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_process_only_lineages_retention",
        "process_only_operation_lineages",
        "retention_applicabilities",
        ["workspace_id", "environment_id", "source_object_id", "retention_applicability_reference", "retention_applicability_generation"],
        ["workspace_id", "environment_id", "source_object_id", "applicability_id", "generation"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_process_only_lineages_authorization_audit",
        "process_only_operation_lineages",
        "audit_events",
        ["workspace_id", "environment_id", "authorization_audit_event_id"],
        ["workspace_id", "environment_id", "audit_event_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_unique_constraint(
        "uq_process_only_lineages_receipt_binding",
        "process_only_operation_lineages",
        ["workspace_id", "environment_id", "operation_id", "source_object_id", "source_revision_id", "ingestion_run_source_id"],
        schema=SCHEMA,
    )

    op.add_column(
        "processing_receipts",
        sa.Column("processing_scope", sa.Text(), nullable=False, server_default="ordinary"),
        schema=SCHEMA,
    )
    op.add_column(
        "processing_receipts",
        sa.Column("process_only_operation_id", uuid),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        op.f("ck_processing_receipts_process_only_scope"),
        "processing_receipts",
        "(processing_scope = 'ordinary' AND process_only_operation_id IS NULL) OR (processing_scope = 'process_only' AND process_only_operation_id IS NOT NULL)",
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_processing_receipts_process_only_lineage",
        "processing_receipts",
        "process_only_operation_lineages",
        ["workspace_id", "environment_id", "process_only_operation_id", "source_object_id", "source_revision_id", "ingestion_run_source_id"],
        ["workspace_id", "environment_id", "operation_id", "source_object_id", "source_revision_id", "ingestion_run_source_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_processing_receipts_process_only_lineage",
        "processing_receipts",
        schema=SCHEMA,
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_process_only_lineages_receipt_binding",
        "process_only_operation_lineages",
        schema=SCHEMA,
        type_="unique",
    )
    op.drop_constraint(
        op.f("ck_processing_receipts_process_only_scope"),
        "processing_receipts",
        schema=SCHEMA,
        type_="check",
    )
    op.drop_column("processing_receipts", "process_only_operation_id", schema=SCHEMA)
    op.drop_column("processing_receipts", "processing_scope", schema=SCHEMA)

    for name in (
        "fk_process_only_lineages_authorization_audit",
        "fk_process_only_lineages_retention",
        "fk_process_only_lineages_lifecycle",
        "fk_process_only_lineages_processing_policy",
        "fk_process_only_lineages_receipt_association",
        "fk_process_only_lineages_processing_admission",
        "fk_process_only_lineages_canonical_acceptance",
    ):
        op.drop_constraint(
            name,
            "process_only_operation_lineages",
            schema=SCHEMA,
            type_="foreignkey",
        )
    op.drop_constraint(
        op.f("ck_process_only_lineages_governed_reprocess"),
        "process_only_operation_lineages",
        schema=SCHEMA,
        type_="check",
    )
    for name in (
        "authorized_at",
        "authorization_audit_event_id",
        "process_only_authorization_digest",
        "process_only_authorization_version",
        "process_only_authorization_id",
        "retention_applicability_generation",
        "retention_applicability_reference",
        "lifecycle_eligibility_generation",
        "lifecycle_eligibility_reference",
        "processing_policy_version_id",
        "verified_byte_length",
        "receipt_association_id",
        "processing_admission_id",
        "canonical_acceptance_id",
        "canonical_accepted_generation",
        "schema_version",
    ):
        op.drop_column("process_only_operation_lineages", name, schema=SCHEMA)
    op.drop_constraint(
        "uq_processing_admissions_reprocess_evidence",
        "source_processing_admissions",
        schema=SCHEMA,
        type_="unique",
    )
    op.drop_constraint(
        "uq_source_receipt_associations_reprocess_receipt",
        "source_revision_receipt_associations",
        schema=SCHEMA,
        type_="unique",
    )
    op.drop_constraint(
        "uq_canonical_acceptances_reprocess_authority",
        "canonical_acceptances",
        schema=SCHEMA,
        type_="unique",
    )
