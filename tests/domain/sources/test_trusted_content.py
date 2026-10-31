from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.domain.sources import (
    SourceRevisionReceiptAssociation,
    TrustedContentFormat,
    TrustedContentVerificationEvidence,
    TrustedContentVerificationOutcome,
    VerificationCleanupOutcome,
    content_revision,
)
from spine.domain.sources.original_reference import ObjectReference


NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
WORKSPACE_ID = UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT_ID = UUID("20000000-0000-4000-8000-000000000001")
SOURCE_ID = UUID("30000000-0000-4000-8000-000000000001")
REVISION_ID = UUID("40000000-0000-4000-8000-000000000001")
ASSOCIATION_ID = UUID("50000000-0000-4000-8000-000000000001")
RECEIPT_ID = UUID("60000000-0000-4000-8000-000000000001")


def _revision_and_association() -> tuple[object, SourceRevisionReceiptAssociation]:
    reference = ObjectReference(
        schema_version=1,
        object_id=UUID("70000000-0000-4000-8000-000000000001"),
        storage_generation=UUID("80000000-0000-4000-8000-000000000001"),
        digest_algorithm="sha256",
        digest_hex="a" * 64,
        byte_length=12,
    )
    revision = content_revision(
        revision_id=REVISION_ID,
        source_object_id=SOURCE_ID,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        kind="content",
        revision_schema_version="source-revision:v1",
        revision_metadata_schema="revision-metadata:r1-document-v1",
        revision_metadata={"embedded_title": "Synthetic"},
        original_reference=reference,
        original_sha256=reference.digest_hex,
        byte_length=reference.byte_length,
        media_type="text/plain",
        observed_at=NOW,
    )
    association = SourceRevisionReceiptAssociation(
        association_id=ASSOCIATION_ID,
        source_object_id=SOURCE_ID,
        revision_id=REVISION_ID,
        provenance_id=UUID("90000000-0000-4000-8000-000000000001"),
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        original_receipt_kind="issue6.write_receipt",
        original_receipt_id=RECEIPT_ID,
        original_command_result_id=UUID("a0000000-0000-4000-8000-000000000001"),
        original_command_result_schema_version=1,
        original_upload_id=UUID("b0000000-0000-4000-8000-000000000001"),
        original_reference=reference,
        original_sha256=reference.digest_hex,
        byte_length=reference.byte_length,
        finalized_at=NOW,
        associated_at=NOW,
    )
    return revision, association


def test_verifier_evidence_is_immutable_and_binds_exact_finalized_receipt() -> None:
    revision, association = _revision_and_association()

    evidence = TrustedContentVerificationEvidence.issue(
        evidence_id=UUID("c0000000-0000-4000-8000-000000000001"),
        revision=revision,
        association=association,
        recognized_format=TrustedContentFormat.TEXT,
        format_profile_version="text-utf8:v1",
        verifier_profile_version="trusted-content-offline:v1",
        bounds_profile_version="trusted-content-bounds:test-v1",
        outcome=TrustedContentVerificationOutcome.VERIFIED,
        reason_codes=("verified",),
        cleanup_outcome=VerificationCleanupOutcome.COMPLETED,
        verified_at=NOW,
    )

    evidence.assert_matches(revision=revision, association=association)
    assert evidence.original_receipt_id == RECEIPT_ID
    assert evidence.original_sha256 == "a" * 64
    assert evidence.byte_length == 12
    assert len(evidence.evidence_digest) == 64
    assert "content" not in evidence.model_dump()
    with pytest.raises(Exception):
        evidence.outcome = TrustedContentVerificationOutcome.DENIED  # type: ignore[misc]
    with pytest.raises(ValueError, match="evidence_digest"):
        evidence.model_copy(update={"reason_codes": ("changed",)})


def test_verifier_evidence_rejects_substituted_receipt_association() -> None:
    revision, association = _revision_and_association()
    substituted = association.model_copy(
        update={"association_id": UUID("d0000000-0000-4000-8000-000000000001")}
    )

    evidence = TrustedContentVerificationEvidence.issue(
        evidence_id=UUID("c0000000-0000-4000-8000-000000000001"),
        revision=revision,
        association=association,
        recognized_format=TrustedContentFormat.TEXT,
        format_profile_version="text-utf8:v1",
        verifier_profile_version="trusted-content-offline:v1",
        bounds_profile_version="trusted-content-bounds:test-v1",
        outcome=TrustedContentVerificationOutcome.VERIFIED,
        reason_codes=("verified",),
        cleanup_outcome=VerificationCleanupOutcome.COMPLETED,
        verified_at=NOW,
    )

    with pytest.raises(ValueError, match="exact finalized receipt association"):
        evidence.assert_matches(revision=revision, association=substituted)
