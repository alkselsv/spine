from __future__ import annotations

from spine.infrastructure.persistence.postgresql_mappings import (
    process_only_operation_lineages,
    processing_receipts,
)

def test_governed_reprocessing_mapping_separates_process_only_output() -> None:
    lineage_columns = {
        column.name for column in process_only_operation_lineages.columns
    }
    receipt_columns = {column.name for column in processing_receipts.columns}

    assert {
        "schema_version",
        "canonical_acceptance_id",
        "canonical_accepted_generation",
        "processing_admission_id",
        "receipt_association_id",
        "verified_byte_length",
        "processing_policy_version_id",
        "lifecycle_eligibility_reference",
        "retention_applicability_reference",
        "process_only_authorization_id",
        "authorization_audit_event_id",
    } <= lineage_columns
    assert {"processing_scope", "process_only_operation_id"} <= receipt_columns
