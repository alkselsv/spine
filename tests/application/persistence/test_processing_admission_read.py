from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.persistence.canonical import CanonicalAcceptanceReadResult
from spine.application.processing_disposition import (
    CanonicalCurrentProcessingDispositionAuthority,
    OrdinaryProcessingEligibilityReason,
)
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


class _CanonicalReader:
    def __init__(
        self,
        result: CanonicalAcceptanceReadResult | None,
    ) -> None:
        self.result = result

    async def resolve_current(self, source_object_id: UUID) -> CurrentSourceHead | None:
        assert source_object_id == uid(4)
        return None if self.result is None else self.result.current

    async def resolve_acceptance_read(
        self, canonical_acceptance_id: UUID
    ) -> CanonicalAcceptanceReadResult | None:
        assert canonical_acceptance_id == uid(21)
        return self.result


@pytest.mark.asyncio
async def test_current_processor_required_acceptance_allows_ordinary_processing() -> None:
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
    authority = CanonicalCurrentProcessingDispositionAuthority(
        _CanonicalReader(
            CanonicalAcceptanceReadResult(
                acceptance(), processing_admission(), current
            )
        )
    )

    result = await authority.resolve(
        workspace_id=uid(2),
        environment_id=uid(3),
        source_object_id=uid(4),
        source_revision_id=uid(5),
    )

    assert result.eligible is True
    assert result.reason is OrdinaryProcessingEligibilityReason.ELIGIBLE
    assert result.processing_mode is ProcessingAdmissionMode.PROCESSOR_REQUIRED


@pytest.mark.asyncio
async def test_opaque_current_acceptance_denies_ordinary_processing() -> None:
    from spine.domain.sources import ExactOpaqueApprovalReference

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
    opaque = processing_admission().model_copy(
        update={
            "mode": ProcessingAdmissionMode.OPAQUE_ACCEPTANCE,
            "processor": None,
            "opaque_approval": ExactOpaqueApprovalReference(
                approval_id=uid(30),
                approval_version="opaque:v1",
                approval_digest="f" * 64,
                approved_purpose="archive",
                security_controls_reference=uid(31),
                retention_applicability_reference=uid(17),
            ),
        }
    )
    authority = CanonicalCurrentProcessingDispositionAuthority(
        _CanonicalReader(CanonicalAcceptanceReadResult(acceptance(), opaque, current))
    )

    result = await authority.resolve(
        workspace_id=uid(2),
        environment_id=uid(3),
        source_object_id=uid(4),
        source_revision_id=uid(5),
    )

    assert result.eligible is False
    assert result.reason is OrdinaryProcessingEligibilityReason.OPAQUE_ACCEPTANCE


@pytest.mark.asyncio
async def test_processing_authority_isolates_current_head_between_workspaces() -> None:
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
    authority = CanonicalCurrentProcessingDispositionAuthority(
        _CanonicalReader(
            CanonicalAcceptanceReadResult(
                acceptance(), processing_admission(), current
            )
        )
    )

    result = await authority.resolve(
        workspace_id=uid(99),
        environment_id=uid(3),
        source_object_id=uid(4),
        source_revision_id=uid(5),
    )

    assert result.eligible is False
    assert result.reason is OrdinaryProcessingEligibilityReason.CURRENT_REVISION_MISMATCH
    assert result.processing_admission_id is None
