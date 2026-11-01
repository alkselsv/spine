from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.persistence.canonical import CanonicalAcceptanceReadResult
from spine.domain.sources import (
    CanonicalAcceptance,
    CanonicalOrdering,
    CurrentSourceHead,
    ProcessingAdmissionMode,
    QualifiedProcessorReference,
    RevisionKind,
    SourceProcessingAdmission,
)


NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def uid(value: int) -> UUID:
    return UUID(int=value)


def processing_admission() -> SourceProcessingAdmission:
    return SourceProcessingAdmission(
        processing_admission_id=uid(1),
        workspace_id=uid(2),
        environment_id=uid(3),
        source_object_id=uid(4),
        source_revision_id=uid(5),
        selector_id=uid(6),
        provider_connection_id=uid(7),
        provider_connection_version_id=uid(8),
        provider_approval_id=uid(9),
        resource_scope_reference=uid(10),
        authorization_generation=1,
        configuration_generation=1,
        grant_binding_id=uid(11),
        grant_token_epoch=1,
        grant_security_fence=0,
        root_policy_id=uid(12),
        root_policy_version="root:v1",
        root_policy_digest="a" * 64,
        content_policy_id=uid(13),
        content_policy_version="content:v1",
        content_policy_digest="b" * 64,
        receipt_association_id=uid(14),
        verified_sha256="c" * 64,
        verified_byte_length=42,
        verifier_evidence_id=uid(15),
        verifier_profile_version="verifier:v1",
        verifier_evidence_digest="d" * 64,
        lifecycle_eligibility_reference=uid(16),
        lifecycle_eligibility_generation=1,
        retention_applicability_reference=uid(17),
        retention_applicability_generation=1,
        mode=ProcessingAdmissionMode.PROCESSOR_REQUIRED,
        processor=QualifiedProcessorReference(
            capability_id="parse_document",
            capability_version="parse-document:v1",
            processor_profile_version="parser:v1",
            qualification_evidence_reference=uid(18),
            qualification_evidence_digest="e" * 64,
        ),
        idempotency_receipt_id=uid(19),
        audit_event_id=uid(20),
        created_at=NOW,
    )


def acceptance() -> CanonicalAcceptance:
    return CanonicalAcceptance(
        canonical_acceptance_schema_version=2,
        canonical_acceptance_id=uid(21),
        source_object_id=uid(4),
        source_revision_id=uid(5),
        workspace_id=uid(2),
        environment_id=uid(3),
        accepted_generation=1,
        ordering=CanonicalOrdering.EXPECTED_CURRENT,
        admission_decision_id=uid(22),
        access_policy_version_id=uid(23),
        processing_admission_id=uid(1),
        provenance_id=uid(24),
        idempotency_receipt_id=uid(19),
        audit_event_id=uid(20),
        outbox_event_id=uid(25),
        accepted_at=NOW,
    )


def test_acceptance_read_exposes_exact_processing_disposition_and_currentness() -> None:
    current = CurrentSourceHead(
        source_object_id=uid(4),
        workspace_id=uid(2),
        environment_id=uid(3),
        current_revision_id=uid(5),
        current_revision_kind=RevisionKind.CONTENT,
        acceptance_id=uid(21),
        accepted_generation=1,
        cas_version=1,
        changed_at=NOW,
    )

    result = CanonicalAcceptanceReadResult(
        acceptance(), processing_admission(), current
    )

    assert result.processing_admission == processing_admission()
    assert result.is_current is True


def test_acceptance_read_rejects_missing_exact_processing_disposition() -> None:
    with pytest.raises(ValueError, match="processing link is incomplete"):
        CanonicalAcceptanceReadResult(acceptance(), None, None)
