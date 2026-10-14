from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.projections import (
    ProjectionReconciliationResult,
    ProjectionSnapshot,
    ProjectionSnapshotMember,
    ProjectionSnapshotMemberDisposition,
    ProjectionSnapshotState,
    projection_reconciliation_findings_digest,
    projection_snapshot_manifest_digest,
)


NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
IDS = [UUID(int=value) for value in range(1, 20)]


def snapshot() -> ProjectionSnapshot:
    return ProjectionSnapshot(
        projection_snapshot_id=IDS[0],
        projection_run_id=IDS[1],
        projection_candidate_id=IDS[2],
        workspace_id=IDS[3],
        environment_id=IDS[4],
        projection_kind="qa",
        projection_config_version_id=IDS[5],
        canonical_boundary_id=IDS[6],
        candidate_evidence_digest="a" * 64,
        manifest_digest="b" * 64,
        member_count=1,
        created_at=NOW,
    )


def member() -> ProjectionSnapshotMember:
    return ProjectionSnapshotMember(
        projection_snapshot_member_id=IDS[7],
        projection_snapshot_id=IDS[0],
        canonical_boundary_id=IDS[6],
        canonical_boundary_member_id=IDS[8],
        workspace_id=IDS[3],
        environment_id=IDS[4],
        source_object_id=IDS[9],
        source_revision_id=IDS[10],
        disposition=ProjectionSnapshotMemberDisposition.INCLUDED,
        projection_partition_id=IDS[11],
        projection_receipt_id=IDS[12],
    )


def reconciliation() -> ProjectionReconciliationResult:
    findings_schema_version = "reconciliation-findings:v1"
    findings_payload = {"missing_artifacts": 0, "stale_artifacts": 0}
    return ProjectionReconciliationResult(
        reconciliation_result_id=IDS[0],
        workspace_id=IDS[3],
        environment_id=IDS[4],
        projection_kind="qa",
        projection_snapshot_id=IDS[5],
        projection_activation_id=IDS[6],
        activation_generation=1,
        attempt_identity="check-1",
        attempt_digest="a" * 64,
        outcome="healthy",
        findings_schema_version=findings_schema_version,
        findings_payload=findings_payload,
        findings_digest=projection_reconciliation_findings_digest(
            findings_schema_version=findings_schema_version,
            findings_payload=findings_payload,
        ),
        artifact_evidence_reference="artifact-evidence-1",
        artifact_evidence_digest="b" * 64,
        idempotency_receipt_id=IDS[7],
        audit_event_reference="audit-1",
        checked_at=NOW,
    )


def test_snapshot_manifest_digest_is_order_independent_and_membership_sensitive() -> None:
    first = member()
    second = first.model_copy(
        update={
            "projection_snapshot_member_id": IDS[13],
            "canonical_boundary_member_id": IDS[14],
            "source_object_id": IDS[15],
            "source_revision_id": IDS[16],
            "projection_partition_id": IDS[17],
            "projection_receipt_id": IDS[18],
        }
    )
    target = snapshot().model_copy(update={"member_count": 2})

    assert projection_snapshot_manifest_digest(target, (first, second)) == projection_snapshot_manifest_digest(
        target, (second, first)
    )
    assert projection_snapshot_manifest_digest(target, (first, second)) != projection_snapshot_manifest_digest(
        target, (first,)
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"projection_partition_id": None},
        {"projection_receipt_id": None},
        {"exclusion_code": "missing_receipt"},
    ],
)
def test_included_snapshot_member_requires_exact_artifact_shape(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        member().model_copy(update=updates)


def test_snapshot_initial_state_is_assembling() -> None:
    assert snapshot().snapshot_state is ProjectionSnapshotState.ASSEMBLING


def test_reconciliation_requires_digest_of_findings_payload() -> None:
    result = reconciliation()

    assert result.findings_digest == projection_reconciliation_findings_digest(
        findings_schema_version=result.findings_schema_version,
        findings_payload=result.findings_payload,
    )

    with pytest.raises(ValidationError):
        result.model_copy(update={"findings_digest": "0" * 64})
