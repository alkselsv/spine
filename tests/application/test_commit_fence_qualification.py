from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.commit_fence_qualification import (
    CommitFenceDiagnostic,
    FenceDiagnosticMode,
    FenceDiagnosticOutcome,
    FenceDiagnosticReason,
    QualificationAuthorityClass,
    QualificationAuthorityBinding,
    QualificationEvidence,
    QualificationEvidenceStatus,
    QualificationParameters,
    QualificationProgressStatus,
    QualificationReviewerDecision,
    QualificationReviewerDecisionRecord,
    QualificationReviewerRole,
    UnsupportedQualificationScope,
    UnsupportedQualificationScopes,
    build_offline_qualification_template,
    evaluate_f2_qualification,
    parse_f2_qualification,
)


def reference(value: int) -> UUID:
    return UUID(int=value)


def external_record_with_identity_and_contract():
    template = build_offline_qualification_template(record_reference=reference(10))
    payload = template.model_dump(mode="python")
    payload.update(
        {
            "authority_class": QualificationAuthorityClass.EXTERNAL_F2,
            "provider_reference": reference(11),
            "provider_identity": QualificationEvidence(
                status=QualificationEvidenceStatus.VERIFIED,
                evidence_reference=reference(12),
            ),
            "provider_contract_version": "1.0.0",
            "provider_contract": QualificationEvidence(
                status=QualificationEvidenceStatus.VERIFIED,
                evidence_reference=reference(13),
            ),
        }
    )
    return type(template).model_validate(payload)


def complete_synthetic_evidence_record():
    template = build_offline_qualification_template(record_reference=reference(10))
    payload = template.model_dump(mode="python")
    payload.update(
        {
            "authority_class": QualificationAuthorityClass.EXTERNAL_F2,
            "provider_reference": reference(11),
            "provider_identity": QualificationEvidence(
                status=QualificationEvidenceStatus.VERIFIED,
                evidence_reference=reference(12),
            ),
            "provider_contract_version": "1.0.0",
            "provider_contract": QualificationEvidence(
                status=QualificationEvidenceStatus.VERIFIED,
                evidence_reference=reference(13),
            ),
            "environment_reference": reference(14),
            "environment": QualificationEvidence(
                status=QualificationEvidenceStatus.VERIFIED,
                evidence_reference=reference(15),
            ),
            "authority_binding": QualificationAuthorityBinding(
                status=QualificationEvidenceStatus.VERIFIED,
                issuer_reference=reference(16),
                subject_reference=reference(17),
                workspace_reference=reference(18),
                environment_reference=reference(14),
                purpose="qualification",
                action="commit",
                evidence_reference=reference(19),
            ),
            "adapter_version": "1.0.0",
            "composition_version": "1.0.0",
            "parameters": QualificationParameters(
                status=QualificationEvidenceStatus.VERIFIED,
                permit_lifetime_ms=1000,
                maximum_transaction_ms=100,
                renewal_supported=False,
                clock_uncertainty_ms=1,
                safety_margin_ms=1,
                request_timeout_ms=100,
                retry_limit=0,
                evidence_reference=reference(20),
            ),
            "reviewer_decisions": tuple(
                QualificationReviewerDecisionRecord(
                    role=role,
                    decision=QualificationReviewerDecision.ACCEPTED,
                    reviewer_reference=reference(30 + index),
                    decided_at=datetime(2026, 11, 6, tzinfo=timezone.utc),
                    evidence_reference=reference(40 + index),
                )
                for index, role in enumerate(QualificationReviewerRole)
            ),
            "remaining_unsupported_scopes": UnsupportedQualificationScopes(
                status=QualificationEvidenceStatus.VERIFIED,
                scopes=(
                    UnsupportedQualificationScope(
                        scope_reference=reference(80),
                        reason_code=FenceDiagnosticReason.UNKNOWN_AUTHORITY_CLASS,
                    ),
                ),
                evidence_reference=reference(81),
            ),
        }
    )
    for index, field in enumerate(
        (
            "permit_acquisition_release",
            "revocation_acknowledgement_order",
            "expiry_renewal_guarantees",
            "stale_holder_generation_semantics",
            "clock_uncertainty",
            "transaction_commit_bounds",
            "timeout_retry_recovery",
            "provider_conformance_results",
            "adapter_composition_evidence",
        ),
        start=50,
    ):
        payload[field] = QualificationEvidence(
            status=QualificationEvidenceStatus.VERIFIED,
            evidence_reference=reference(index),
        )
    return type(template).model_validate(payload)


def test_diagnostic_serializes_only_the_versioned_allowlist() -> None:
    diagnostic = CommitFenceDiagnostic(
        operation_id=reference(1),
        authority_reference=reference(2),
        environment_reference=reference(3),
        fence_mode=FenceDiagnosticMode.EXTERNAL_F2,
        outcome_code=FenceDiagnosticOutcome.F4_DENIED,
        reason_code=FenceDiagnosticReason.PROVIDER_NOT_VERIFIED,
        evidence_reference=None,
        diagnostic_timestamp=datetime(2026, 11, 6, tzinfo=timezone.utc),
    )

    assert set(diagnostic.to_operator_payload()) == {
        "schema_version",
        "operation_id",
        "authority_reference",
        "environment_reference",
        "fence_mode",
        "outcome_code",
        "reason_code",
        "evidence_reference",
        "diagnostic_timestamp",
    }
    assert diagnostic.to_operator_payload()["outcome_code"] == "F4_DENIED"


def test_diagnostic_allowlist_rejects_schema_subclasses() -> None:
    class ExtendedDiagnostic(CommitFenceDiagnostic):
        credential: str

    diagnostic = ExtendedDiagnostic(
        operation_id=reference(1),
        authority_reference=None,
        environment_reference=None,
        fence_mode=FenceDiagnosticMode.UNKNOWN_F4,
        outcome_code=FenceDiagnosticOutcome.F4_DENIED,
        reason_code=FenceDiagnosticReason.UNKNOWN_AUTHORITY_CLASS,
        evidence_reference=None,
        diagnostic_timestamp=datetime(2026, 11, 6, tzinfo=timezone.utc),
        credential="synthetic-secret",
    )

    with pytest.raises(TypeError, match="exact versioned schema"):
        diagnostic.to_operator_payload()


def test_diagnostic_rejects_unexpected_nested_fields() -> None:
    with pytest.raises(ValidationError):
        CommitFenceDiagnostic.model_validate(
            {
                "operation_id": reference(1),
                "authority_reference": None,
                "environment_reference": None,
                "fence_mode": "unknown_f4",
                "outcome_code": "F4_DENIED",
                "reason_code": "unknown_authority_class",
                "evidence_reference": None,
                "diagnostic_timestamp": datetime(2026, 11, 6, tzinfo=timezone.utc),
                "provider_response": {"token": "never serialize"},
            }
        )


def test_diagnostic_requires_utc_timestamp_and_opaque_references() -> None:
    base = {
        "operation_id": reference(1),
        "authority_reference": None,
        "environment_reference": None,
        "fence_mode": "unknown_f4",
        "outcome_code": "F4_DENIED",
        "reason_code": "unknown_authority_class",
        "evidence_reference": None,
        "diagnostic_timestamp": datetime(2026, 11, 6),
    }

    with pytest.raises(ValidationError):
        CommitFenceDiagnostic.model_validate(base)
    with pytest.raises(ValidationError):
        CommitFenceDiagnostic.model_validate({**base, "operation_id": "https://signed.example/x"})


def test_diagnostic_requires_a_safe_reason_for_denial() -> None:
    with pytest.raises(ValidationError):
        CommitFenceDiagnostic(
            operation_id=reference(1),
            authority_reference=None,
            environment_reference=None,
            fence_mode=FenceDiagnosticMode.UNKNOWN_F4,
            outcome_code=FenceDiagnosticOutcome.F4_DENIED,
            reason_code=None,
            evidence_reference=None,
            diagnostic_timestamp=datetime(2026, 11, 6, tzinfo=timezone.utc),
        )


def test_offline_template_keeps_provider_and_environment_unprovided() -> None:
    template = build_offline_qualification_template(record_reference=reference(10))
    payload = template.model_dump(mode="json")

    assert payload["schema_version"] == "external-commit-fence-qualification:v1"
    assert payload["qualification_status"] == "F2_NOT_QUALIFIED"
    assert payload["provider_reference"] is None
    assert payload["provider_identity"] == {
        "status": "NOT_PROVIDED",
        "evidence_reference": None,
        "valid_until": None,
    }
    assert payload["environment"] == {
        "status": "NOT_PROVIDED",
        "evidence_reference": None,
        "valid_until": None,
    }
    assert payload["parameters"]["permit_lifetime_ms"] is None
    assert len(payload["reviewer_decisions"]) == len(QualificationReviewerRole)
    assert all(
        reviewer["decision"] == "NOT_PROVIDED"
        for reviewer in payload["reviewer_decisions"]
    )
    assert payload["remaining_unsupported_scopes"]["status"] == "NOT_PROVIDED"


def test_incomplete_qualification_is_f4_denied() -> None:
    template = build_offline_qualification_template(record_reference=reference(10))

    result = evaluate_f2_qualification(template, evaluated_at=datetime(2026, 11, 6, tzinfo=timezone.utc))

    assert result.outcome_code is FenceDiagnosticOutcome.F4_DENIED
    assert result.qualification_status is QualificationProgressStatus.F4_DENIED
    assert result.reason_code is FenceDiagnosticReason.UNKNOWN_AUTHORITY_CLASS


def test_missing_provider_identity_is_f4_denied_for_external_authority_class() -> None:
    template = build_offline_qualification_template(record_reference=reference(10))
    record = type(template).model_validate(
        {
            **template.model_dump(mode="python"),
            "authority_class": QualificationAuthorityClass.EXTERNAL_F2,
        }
    )

    result = evaluate_f2_qualification(
        record, evaluated_at=datetime(2026, 11, 6, tzinfo=timezone.utc)
    )

    assert result.outcome_code is FenceDiagnosticOutcome.F4_DENIED
    assert result.qualification_status is QualificationProgressStatus.PROVIDER_NOT_VERIFIED
    assert result.reason_code is FenceDiagnosticReason.PROVIDER_NOT_VERIFIED


def test_missing_named_environment_is_f4_denied() -> None:
    record = external_record_with_identity_and_contract()

    result = evaluate_f2_qualification(
        record, evaluated_at=datetime(2026, 11, 6, tzinfo=timezone.utc)
    )

    assert result.outcome_code is FenceDiagnosticOutcome.F4_DENIED
    assert result.qualification_status is QualificationProgressStatus.ENVIRONMENT_NOT_VERIFIED
    assert result.reason_code is FenceDiagnosticReason.ENVIRONMENT_NOT_VERIFIED


def test_expired_provider_identity_evidence_is_f4_denied() -> None:
    template = build_offline_qualification_template(record_reference=reference(10))
    payload = template.model_dump(mode="python")
    payload.update(
        {
            "authority_class": QualificationAuthorityClass.EXTERNAL_F2,
            "provider_reference": reference(11),
            "provider_identity": QualificationEvidence(
                status=QualificationEvidenceStatus.VERIFIED,
                evidence_reference=reference(12),
                valid_until=datetime(2026, 11, 6, tzinfo=timezone.utc),
            ),
        }
    )
    record = type(template).model_validate(payload)

    result = evaluate_f2_qualification(
        record, evaluated_at=datetime(2026, 11, 6, tzinfo=timezone.utc)
    )

    assert result.outcome_code is FenceDiagnosticOutcome.F4_DENIED
    assert result.reason_code is FenceDiagnosticReason.EVIDENCE_EXPIRED


def test_unknown_schema_version_is_f4_denied_without_echoing_input() -> None:
    result = parse_f2_qualification(
        {"schema_version": "external-commit-fence-qualification:v999", "token": "secret"},
        evaluated_at=datetime(2026, 11, 6, tzinfo=timezone.utc),
    )

    assert result.outcome_code is FenceDiagnosticOutcome.F4_DENIED
    assert result.reason_code is FenceDiagnosticReason.UNSUPPORTED_SCHEMA_VERSION
    assert "secret" not in result.model_dump_json()


def test_qualification_json_round_trip_has_only_typed_template_fields() -> None:
    template = build_offline_qualification_template(record_reference=reference(10))
    parsed = type(template).model_validate_json(template.model_dump_json())

    assert parsed == template
    assert "provider_response" not in parsed.model_dump_json()


def test_unexpected_provider_payload_and_signed_url_cannot_become_record_fields() -> None:
    template = build_offline_qualification_template(record_reference=reference(10))
    payload = template.model_dump(mode="json")
    payload["provider_response"] = {
        "token": "Bearer synthetic-secret",
        "signed_url": "https://example.invalid/signed?token=secret",
        "content": "protected text",
    }

    result = parse_f2_qualification(
        payload, evaluated_at=datetime(2026, 11, 6, tzinfo=timezone.utc)
    )

    assert result.outcome_code is FenceDiagnosticOutcome.F4_DENIED
    assert "synthetic-secret" not in result.model_dump_json()
    assert "protected text" not in result.model_dump_json()
    assert "https://example.invalid" not in result.model_dump_json()


def test_record_rejects_unbounded_nested_reviewer_entries() -> None:
    template = build_offline_qualification_template(record_reference=reference(10))
    payload = template.model_dump(mode="json")
    payload["reviewer_decisions"] = [
        payload["reviewer_decisions"][0] for _ in range(len(QualificationReviewerRole) + 1)
    ]

    result = parse_f2_qualification(
        payload, evaluated_at=datetime(2026, 11, 6, tzinfo=timezone.utc)
    )

    assert result.outcome_code is FenceDiagnosticOutcome.F4_DENIED
    assert result.reason_code is FenceDiagnosticReason.EVIDENCE_NOT_VERIFIED


@pytest.mark.parametrize(
    "unsafe_version",
    ("sk-production-token", "https://provider.invalid/contracts/v1"),
)
def test_contract_version_rejects_credentials_and_urls(unsafe_version: str) -> None:
    template = build_offline_qualification_template(record_reference=reference(10))
    payload = template.model_dump(mode="json")
    payload["provider_contract_version"] = unsafe_version

    result = parse_f2_qualification(
        payload, evaluated_at=datetime(2026, 11, 6, tzinfo=timezone.utc)
    )

    assert result.outcome_code is FenceDiagnosticOutcome.F4_DENIED
    assert unsafe_version not in result.model_dump_json()


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("EXPIRED", FenceDiagnosticReason.EVIDENCE_EXPIRED),
        ("AMBIGUOUS", FenceDiagnosticReason.PROVIDER_RESULT_AMBIGUOUS),
        ("SYNTHETIC_ONLY", FenceDiagnosticReason.SYNTHETIC_ONLY_EVIDENCE),
        ("UNSUPPORTED", FenceDiagnosticReason.EVIDENCE_UNSUPPORTED),
    ],
)
def test_unsafe_evidence_status_is_f4_denied(status: str, reason: FenceDiagnosticReason) -> None:
    template = build_offline_qualification_template(record_reference=reference(10))
    payload = template.model_dump(mode="python")
    payload["authority_class"] = QualificationAuthorityClass.EXTERNAL_F2
    payload["provider_identity"] = {
        "status": QualificationEvidenceStatus(status),
        "evidence_reference": reference(11),
        "valid_until": datetime(2026, 11, 5, tzinfo=timezone.utc),
    }
    record = type(template).model_validate(payload)

    result = evaluate_f2_qualification(record, evaluated_at=datetime(2026, 11, 6, tzinfo=timezone.utc))

    assert result.outcome_code is FenceDiagnosticOutcome.F4_DENIED
    assert result.reason_code is reason


def test_qualification_record_is_not_a_provider_or_authorization_token() -> None:
    template = build_offline_qualification_template(record_reference=reference(10))

    assert not hasattr(template, "acquire")
    assert not hasattr(template, "permits_commit")
    assert template.qualification_status == "F2_NOT_QUALIFIED"


def test_qualification_record_cannot_claim_f2_qualified() -> None:
    template = build_offline_qualification_template(record_reference=reference(10))

    with pytest.raises(ValidationError):
        type(template).model_validate(
            {
                **template.model_dump(mode="python"),
                "qualification_status": "F2_QUALIFIED",
            }
        )


def test_complete_offline_evidence_still_cannot_qualify_or_grant_f2() -> None:
    record = complete_synthetic_evidence_record()

    result = evaluate_f2_qualification(
        record, evaluated_at=datetime(2026, 11, 6, tzinfo=timezone.utc)
    )

    assert result.qualification_status is QualificationProgressStatus.F2_NOT_QUALIFIED
    assert result.outcome_code is FenceDiagnosticOutcome.F4_DENIED
    assert result.reason_code is FenceDiagnosticReason.OFFLINE_RECORD_NOT_AUTHORITY


def test_incomplete_exact_binding_is_f4_denied() -> None:
    record = external_record_with_identity_and_contract()
    payload = record.model_dump(mode="python")
    payload.update(
        {
            "environment_reference": reference(14),
            "environment": QualificationEvidence(
                status=QualificationEvidenceStatus.VERIFIED,
                evidence_reference=reference(15),
            ),
        }
    )
    record_with_environment = type(record).model_validate(payload)

    result = evaluate_f2_qualification(
        record_with_environment, evaluated_at=datetime(2026, 11, 6, tzinfo=timezone.utc)
    )

    assert result.outcome_code is FenceDiagnosticOutcome.F4_DENIED
    assert result.reason_code is FenceDiagnosticReason.INCOMPLETE_AUTHORITY_BINDING


def test_synthetic_authority_class_is_f4_denied() -> None:
    template = build_offline_qualification_template(record_reference=reference(10))
    record = type(template).model_validate(
        {
            **template.model_dump(mode="python"),
            "authority_class": QualificationAuthorityClass.SYNTHETIC_TEST,
        }
    )

    result = evaluate_f2_qualification(
        record, evaluated_at=datetime(2026, 11, 6, tzinfo=timezone.utc)
    )

    assert result.outcome_code is FenceDiagnosticOutcome.F4_DENIED
    assert result.reason_code is FenceDiagnosticReason.SYNTHETIC_ONLY_EVIDENCE
