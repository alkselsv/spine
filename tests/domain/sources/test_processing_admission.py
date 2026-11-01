from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.sources import (
    CanonicalAcceptance,
    CanonicalOrdering,
    ExactOpaqueApprovalReference,
    ProcessingAdmissionMode,
    QualifiedProcessorReference,
    SourceProcessingAdmission,
)


NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def uid(value: int) -> UUID:
    return UUID(int=value)


def admission(**overrides: object) -> SourceProcessingAdmission:
    values: dict[str, object] = {
        "processing_admission_id": uid(1),
        "workspace_id": uid(2),
        "environment_id": uid(3),
        "source_object_id": uid(4),
        "source_revision_id": uid(5),
        "selector_id": uid(6),
        "provider_connection_id": uid(7),
        "provider_connection_version_id": uid(8),
        "provider_approval_id": uid(9),
        "resource_scope_reference": uid(10),
        "authorization_generation": 3,
        "configuration_generation": 4,
        "grant_binding_id": uid(33),
        "grant_token_epoch": 7,
        "grant_security_fence": 0,
        "root_policy_id": uid(11),
        "root_policy_version": "root:v3",
        "root_policy_digest": "a" * 64,
        "content_policy_id": uid(12),
        "content_policy_version": "content:v4",
        "content_policy_digest": "b" * 64,
        "receipt_association_id": uid(13),
        "verified_sha256": "c" * 64,
        "verified_byte_length": 42,
        "verifier_evidence_id": uid(14),
        "verifier_profile_version": "verifier:v1",
        "verifier_evidence_digest": "d" * 64,
        "lifecycle_eligibility_reference": uid(15),
        "lifecycle_eligibility_generation": 5,
        "retention_applicability_reference": uid(16),
        "retention_applicability_generation": 6,
        "mode": ProcessingAdmissionMode.PROCESSOR_REQUIRED,
        "processor": QualifiedProcessorReference(
            capability_id="parse_document",
            capability_version="parse-document:v1",
            processor_profile_version="parser:v2",
            qualification_evidence_reference=uid(17),
            qualification_evidence_digest="e" * 64,
        ),
        "opaque_approval": None,
        "idempotency_receipt_id": uid(18),
        "audit_event_id": uid(19),
        "created_at": NOW,
    }
    values.update(overrides)
    return SourceProcessingAdmission.model_validate(values)


def acceptance(**overrides: object) -> CanonicalAcceptance:
    values: dict[str, object] = {
        "canonical_acceptance_schema_version": 2,
        "canonical_acceptance_id": uid(20),
        "source_object_id": uid(4),
        "source_revision_id": uid(5),
        "workspace_id": uid(2),
        "environment_id": uid(3),
        "accepted_generation": 1,
        "ordering": CanonicalOrdering.EXPECTED_CURRENT,
        "admission_decision_id": uid(21),
        "access_policy_version_id": uid(22),
        "processing_admission_id": uid(1),
        "provenance_id": uid(23),
        "idempotency_receipt_id": uid(18),
        "audit_event_id": uid(19),
        "outbox_event_id": uid(24),
        "accepted_at": NOW,
    }
    values.update(overrides)
    return CanonicalAcceptance.model_validate(values)


def test_processor_required_admission_is_immutable_and_exactly_bound() -> None:
    result = admission()

    assert result.schema_version == "source-processing-admission:v1"
    assert result.processor is not None
    assert result.opaque_approval is None
    assert result.exact_acceptance_binding == (uid(2), uid(3), uid(4), uid(5), uid(13))
    with pytest.raises(ValidationError):
        result.source_revision_id = uid(99)  # type: ignore[misc]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"processor": None}, "processor-required admission"),
        (
            {
                "opaque_approval": ExactOpaqueApprovalReference(
                    approval_id=uid(30),
                    approval_version="opaque:v1",
                    approval_digest="f" * 64,
                    approved_purpose="archive",
                    security_controls_reference=uid(31),
                    retention_applicability_reference=uid(16),
                )
            },
            "processor-required admission",
        ),
        (
            {
                "mode": ProcessingAdmissionMode.OPAQUE_ACCEPTANCE,
                "processor": None,
                "opaque_approval": None,
            },
            "opaque admission",
        ),
    ],
)
def test_processing_admission_rejects_incomplete_or_ambiguous_mode(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        admission(**overrides)


def test_opaque_admission_requires_same_retention_binding() -> None:
    with pytest.raises(ValidationError, match="retention applicability"):
        admission(
            mode=ProcessingAdmissionMode.OPAQUE_ACCEPTANCE,
            processor=None,
            opaque_approval=ExactOpaqueApprovalReference(
                approval_id=uid(30),
                approval_version="opaque:v1",
                approval_digest="f" * 64,
                approved_purpose="archive",
                security_controls_reference=uid(31),
                retention_applicability_reference=uid(32),
            ),
        )


def test_canonical_acceptance_v2_requires_exact_processing_link() -> None:
    assert acceptance().processing_admission_id == uid(1)

    with pytest.raises(ValidationError, match="version 2 requires"):
        acceptance(processing_admission_id=None)


def test_canonical_acceptance_v1_cannot_fabricate_processing_link() -> None:
    historical = acceptance(
        canonical_acceptance_schema_version=1,
        processing_admission_id=None,
    )

    assert historical.canonical_acceptance_schema_version == 1
    with pytest.raises(ValidationError, match="version 1 cannot carry"):
        acceptance(canonical_acceptance_schema_version=1)
