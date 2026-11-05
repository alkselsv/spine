from __future__ import annotations

from uuid import UUID

from spine.application.retention import ApprovedRetentionTerms, RetentionStartingEvent
from spine.domain.sources.retention import RetentionArtifactClass
from spine.infrastructure.private_asr.retention import (
    PostgreSQLPrivateASRRetentionResolver,
    retention_consumer_state_digest,
)
from tests.application.test_retention_applicability import _binding


def _uuid(value: int) -> UUID:
    return UUID(int=value)


def _terms(**updates: object) -> ApprovedRetentionTerms:
    values: dict[str, object] = {
        "retention_policy_id": _uuid(50),
        "retention_policy_version_id": _uuid(51),
        "retention_policy_digest": "2" * 64,
        "artifact_class": RetentionArtifactClass.PRIVATE_ASR_DETAILED_REPORT,
        "terms_id": _uuid(60),
        "terms_version": "terms:v1",
        "terms_digest": "3" * 64,
        "starting_event": RetentionStartingEvent.REPORT_GENERATED,
        "duration_seconds": 300,
        "approval_evidence_reference": _uuid(61),
        "resolved_exception_references": (_uuid(62),),
        "resolved_legal_hold_references": (_uuid(63),),
        "active_legal_hold_references": (),
        "deletion_obligation_reference": _uuid(64),
        "backup_obligation_reference": _uuid(65),
        "approved": True,
        "revoked": False,
    }
    values.update(updates)
    return ApprovedRetentionTerms.model_validate(values)


def test_retention_commit_state_digest_binds_terms_holds_and_operation() -> None:
    applicability = _binding(
        applicability_id=70,
        source_object_id=_uuid(71),
    )
    terms = _terms()
    original = retention_consumer_state_digest(
        binding=applicability,
        terms=terms,
        artifact_class=RetentionArtifactClass.PRIVATE_ASR_DETAILED_REPORT,
        operation="run_private_asr_evaluation",
    )

    assert original != retention_consumer_state_digest(
        binding=applicability,
        terms=terms.model_copy(update={"terms_digest": "4" * 64}),
        artifact_class=RetentionArtifactClass.PRIVATE_ASR_DETAILED_REPORT,
        operation="run_private_asr_evaluation",
    )
    assert original != retention_consumer_state_digest(
        binding=applicability,
        terms=terms.model_copy(update={"active_legal_hold_references": (_uuid(63),)}),
        artifact_class=RetentionArtifactClass.PRIVATE_ASR_DETAILED_REPORT,
        operation="run_private_asr_evaluation",
    )
    assert original != retention_consumer_state_digest(
        binding=applicability,
        terms=terms,
        artifact_class=RetentionArtifactClass.PRIVATE_ASR_ANNOTATION,
        operation="run_private_asr_evaluation",
    )
    assert original != retention_consumer_state_digest(
        binding=applicability,
        terms=terms,
        artifact_class=RetentionArtifactClass.PRIVATE_ASR_DETAILED_REPORT,
        operation="different_operation",
    )


def test_retention_resolver_requires_explicit_owner_or_synthetic_test_source() -> None:
    class Source:
        pass

    approvals = Source()
    terms = Source()
    resolver = PostgreSQLPrivateASRRetentionResolver(approvals=approvals, terms=terms)
    assert resolver.supports_f1_commit_fence() is False

    approvals.retention_fence_source = "postgresql"
    terms.retention_fence_source = "postgresql"
    assert resolver.supports_f1_commit_fence() is True

    synthetic = PostgreSQLPrivateASRRetentionResolver(
        approvals=Source(), terms=Source(), test_only_allow_synthetic_sources=True
    )
    assert synthetic.supports_f1_commit_fence() is True
