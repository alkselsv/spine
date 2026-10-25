from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.capabilities import (
    ApprovedPrecedenceRule,
    AnswerClaim,
    AnswerClaimKind,
    AnswerClaimLocation,
    AnswerContent,
    AnswerDisclosureStatus,
    AnswerEvaluationRequest,
    AnswerPartialityStatus,
    AttemptProjectionBoundary,
    Citation,
    ClaimEvidenceRelationship,
    ClaimEvidenceRelationshipKind,
    ClaimAuthorizationStatus,
    CanonicalViewMember,
    ClaimEvidenceValidation,
    ClaimGroundingStatus,
    CoverageMemberStatus,
    DerivationInput,
    DeterministicDerivation,
    GroundedConflict,
    GroundedConflictStatus,
    GroundedClaimAssessment,
    ObservedCoverage,
    ObservedCoverageMember,
    MissingCoverageImpact,
    PartialFallbackAuthorization,
    PartialFallbackAuthority,
    PartialClaimUtility,
    TargetCompletenessScope,
    TargetScopeMember,
    evaluate_answer_candidate as _evaluate_answer_candidate,
)
from spine.domain.context_bundles import (
    ContextBundleEvidenceReference,
    ContextBundleLineageRef,
    ContextBundleRef,
)


WORKSPACE = UUID(int=1)
ENVIRONMENT = UUID(int=2)
RUN = UUID(int=3)
ATTEMPT = UUID(int=4)
SNAPSHOT = UUID(int=5)
BOUNDARY = UUID(int=6)
ANSWER = UUID(int=7)
CLAIM = UUID(int=8)
CITATION = UUID(int=9)
SOURCE = UUID(int=10)
REVISION = UUID(int=11)
TARGET = UUID(int=12)
CLAIM_B = UUID(int=13)
CITATION_B = UUID(int=14)
EVALUATION_LINEAGE = UUID(int=15)
CLAIM_C = UUID(int=16)
CITATION_C = UUID(int=17)


def evaluate_answer_candidate(**values: Any) -> Any:
    values.setdefault("evaluation_lineage_reference", EVALUATION_LINEAGE)
    return _evaluate_answer_candidate(AnswerEvaluationRequest(**values))


def _boundary() -> AttemptProjectionBoundary:
    return AttemptProjectionBoundary(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        run_id=RUN,
        attempt_id=ATTEMPT,
        projection_snapshot_id=SNAPSHOT,
        activation_generation=3,
        canonical_boundary_id=BOUNDARY,
    )


def _answer() -> AnswerContent:
    citation = Citation(
        citation_id=CITATION,
        answer_id=ANSWER,
        context_bundle=ContextBundleLineageRef(
            bundle=ContextBundleRef(
                bundle_id=UUID(int=20),
                bundle_version=1,
                bundle_digest="a" * 64,
            ),
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            run_id=RUN,
            attempt_id=ATTEMPT,
        ),
        evidence_reference=ContextBundleEvidenceReference(
            schema_version="context-evidence-reference:v2",
            evidence_id="evidence-1",
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            projection_snapshot_id=SNAPSHOT,
            activation_generation=3,
            source_object_id=SOURCE,
            source_revision_id=REVISION,
            original_sha256="b" * 64,
            revision_digest="c" * 64,
            processing_receipt_id=UUID(int=21),
            projected_kind="chunk",
            locator_contract_version="locator:v1",
            locator={"kind": "page", "value": "1"},
            locator_digest="d" * 64,
            evidence_digest="e" * 64,
        ),
    )
    return AnswerContent(
        answer_id=ANSWER,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        run_id=RUN,
        attempt_id=ATTEMPT,
        text="Alpha",
        claims=(
            AnswerClaim(
                claim_id=CLAIM,
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                run_id=RUN,
                attempt_id=ATTEMPT,
                text="Alpha",
                location=AnswerClaimLocation(
                    answer_id=ANSWER,
                    start_offset=0,
                    end_offset=5,
                ),
            ),
        ),
        citations=(citation,),
        relationships=(
            ClaimEvidenceRelationship(
                relationship_id=UUID(int=22),
                answer_id=ANSWER,
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                run_id=RUN,
                attempt_id=ATTEMPT,
                claim_id=CLAIM,
                kind=ClaimEvidenceRelationshipKind.INDEPENDENT,
                citation_ids=(CITATION,),
            ),
        ),
    )


def _conflicting_answer() -> AnswerContent:
    first = _answer()
    second_citation = first.citations[0].model_copy(
        update={
            "citation_id": CITATION_B,
            "evidence_reference": first.citations[0].evidence_reference.model_copy(
                update={
                    "evidence_id": "evidence-2",
                    "evidence_digest": "1" * 64,
                }
            ),
        }
    )
    return first.model_copy(
        update={
            "text": "Alpha Beta",
            "claims": (
                first.claims[0],
                first.claims[0].model_copy(
                    update={
                        "claim_id": CLAIM_B,
                        "text": "Beta",
                        "location": AnswerClaimLocation(
                            answer_id=ANSWER,
                            start_offset=6,
                            end_offset=10,
                        ),
                    }
                ),
            ),
            "citations": (first.citations[0], second_citation),
            "relationships": (
                first.relationships[0],
                first.relationships[0].model_copy(
                    update={
                        "relationship_id": UUID(int=23),
                        "claim_id": CLAIM_B,
                        "citation_ids": (CITATION_B,),
                    }
                ),
                ClaimEvidenceRelationship(
                    relationship_id=UUID(int=24),
                    answer_id=ANSWER,
                    workspace_id=WORKSPACE,
                    environment_id=ENVIRONMENT,
                    run_id=RUN,
                    attempt_id=ATTEMPT,
                    claim_id=CLAIM,
                    kind=ClaimEvidenceRelationshipKind.CONTRADICTION,
                    citation_ids=(),
                    related_claim_id=CLAIM_B,
                ),
            ),
        }
    )


def _overlapping_conflict_answer() -> AnswerContent:
    first = _conflicting_answer()
    third_citation = first.citations[1].model_copy(
        update={
            "citation_id": CITATION_C,
            "evidence_reference": first.citations[1].evidence_reference.model_copy(
                update={
                    "evidence_id": "evidence-3",
                    "evidence_digest": "3" * 64,
                }
            ),
        }
    )
    third_claim = first.claims[1].model_copy(
        update={
            "claim_id": CLAIM_C,
            "text": "Gamma",
            "location": AnswerClaimLocation(
                answer_id=ANSWER,
                start_offset=11,
                end_offset=16,
            ),
        }
    )
    return first.model_copy(
        update={
            "text": "Alpha Beta Gamma",
            "claims": (*first.claims, third_claim),
            "citations": (*first.citations, third_citation),
            "relationships": (
                *first.relationships,
                first.relationships[1].model_copy(
                    update={
                        "relationship_id": UUID(int=25),
                        "claim_id": CLAIM_C,
                        "citation_ids": (CITATION_C,),
                    }
                ),
                ClaimEvidenceRelationship(
                    relationship_id=UUID(int=26),
                    answer_id=ANSWER,
                    workspace_id=WORKSPACE,
                    environment_id=ENVIRONMENT,
                    run_id=RUN,
                    attempt_id=ATTEMPT,
                    claim_id=CLAIM_B,
                    kind=ClaimEvidenceRelationshipKind.CONTRADICTION,
                    citation_ids=(),
                    related_claim_id=CLAIM_C,
                ),
            ),
        }
    )


def _target() -> TargetCompletenessScope:
    return TargetCompletenessScope(
        target_scope_id=TARGET,
        boundary=_boundary(),
        derivation_reference=UUID(int=30),
        canonical_view_reference=UUID(int=32),
        canonical_view_digest="e" * 64,
        canonical_view_generation=1,
        canonical_view_members=(
            CanonicalViewMember(
                source_object_id=SOURCE,
                source_revision_id=REVISION,
                revision_kind="content",
                accepted_generation=1,
                cas_version=1,
            ),
        ),
        source_constraint_digest="f" * 64,
        members=(
            TargetScopeMember(
                source_object_id=SOURCE,
                source_revision_id=REVISION,
                authorization_decision_reference=UUID(int=31),
            ),
        ),
    )


def _coverage() -> ObservedCoverage:
    return ObservedCoverage(
        observed_coverage_id=UUID(int=40),
        target_scope_id=TARGET,
        boundary=_boundary(),
        proof_reference=UUID(int=41),
        members=(
            ObservedCoverageMember(
                source_object_id=SOURCE,
                source_revision_id=REVISION,
                status=CoverageMemberStatus.REPRESENTED,
                proof_reference=UUID(int=42),
            ),
        ),
    )


def test_proven_exhaustive_coverage_produces_complete_answer() -> None:
    result = evaluate_answer_candidate(
        answer=_answer(),
        target_scope=_target(),
        observed_coverage=_coverage(),
        claim_assessments=(_grounded_assessment(),),
    )

    assert result.disclosure_status is AnswerDisclosureStatus.ACCEPTED
    assert result.partiality_status is AnswerPartialityStatus.EXHAUSTIVE
    assert result.coverage_status.value == "complete"
    assert result.disclosable_claim_ids == (CLAIM,)


def test_incomplete_coverage_with_current_fallback_produces_partial_answer() -> None:
    incomplete = _coverage().model_copy(
        update={
            "members": (
                _coverage().members[0].model_copy(
                    update={
                        "status": CoverageMemberStatus.PROJECTION_GAP,
                        "proof_reference": None,
                    }
                ),
            )
        }
    )

    result = evaluate_answer_candidate(
        answer=_answer(),
        target_scope=_target(),
        observed_coverage=incomplete,
        claim_assessments=(_grounded_assessment(),),
        partial_fallback=PartialFallbackAuthorization(
            boundary=_boundary(),
            authority=PartialFallbackAuthority.CURRENT_REQUEST,
            authorization_reference=UUID(int=51),
            policy_version="partial-fallback:v1",
            applicability_reference=UUID(int=52),
        ),
    )

    assert result.disclosure_status is AnswerDisclosureStatus.ACCEPTED
    assert result.partiality_status is AnswerPartialityStatus.PARTIAL
    assert result.coverage_status.value == "insufficient"
    assert result.disclosable_claim_ids == (CLAIM,)


def test_current_grounded_conflict_is_presented_without_selecting_a_winner() -> None:
    assessments = tuple(
        _grounded_assessment(claim_id=claim_id)
        for claim_id in (CLAIM, CLAIM_B)
    )

    result = evaluate_answer_candidate(
        answer=_conflicting_answer(),
        target_scope=_target(),
        observed_coverage=_coverage(),
        claim_assessments=assessments,
        conflicts=(
            GroundedConflict(
                conflict_id=UUID(int=60),
                boundary=_boundary(),
                claim_ids=(CLAIM, CLAIM_B),
                relationship_ids=(UUID(int=24),),
                detection_reference=UUID(int=61),
                detection_policy_version="conflict-policy:v1",
            ),
        ),
    )

    assert result.disclosure_status is AnswerDisclosureStatus.ACCEPTED
    assert result.conflict_status is GroundedConflictStatus.PRESENTED
    assert result.disclosable_claim_ids == (CLAIM, CLAIM_B)


def test_approved_deterministic_precedence_selects_the_winning_claim() -> None:
    conflict = GroundedConflict(
        conflict_id=UUID(int=60),
        boundary=_boundary(),
        claim_ids=(CLAIM, CLAIM_B),
        relationship_ids=(UUID(int=24),),
        detection_reference=UUID(int=61),
        detection_policy_version="conflict-policy:v1",
    )
    assessments = tuple(
        _grounded_assessment(claim_id=claim_id)
        for claim_id in (CLAIM, CLAIM_B)
    )

    result = evaluate_answer_candidate(
        answer=_conflicting_answer(),
        target_scope=_target(),
        observed_coverage=_coverage(),
        claim_assessments=assessments,
        conflicts=(conflict,),
        precedence_rules=(
            ApprovedPrecedenceRule(
                precedence_rule_id=UUID(int=62),
                boundary=_boundary(),
                conflict_id=conflict.conflict_id,
                winning_claim_id=CLAIM_B,
                rule_version="source-precedence:v2",
                approval_reference=UUID(int=63),
            ),
        ),
    )

    assert result.disclosure_status is AnswerDisclosureStatus.ACCEPTED
    assert result.conflict_status is GroundedConflictStatus.RESOLVED_BY_PRECEDENCE
    assert result.disclosable_claim_ids == (CLAIM_B,)


def test_derived_claim_requires_grounded_inputs_and_transformation_lineage() -> None:
    factual = _answer()
    derived = factual.model_copy(
        update={
            "claims": (
                factual.claims[0].model_copy(update={"kind": AnswerClaimKind.DERIVED}),
            ),
            "relationships": (
                factual.relationships[0].model_copy(
                    update={"kind": ClaimEvidenceRelationshipKind.DERIVATION_INPUT}
                ),
            ),
        }
    )

    result = evaluate_answer_candidate(
        answer=derived,
        target_scope=_target(),
        observed_coverage=_coverage(),
        claim_assessments=(_grounded_assessment(),),
        derivations=(
            DeterministicDerivation(
                claim_id=CLAIM,
                boundary=_boundary(),
                transformation_name="sum_invoice_amounts",
                transformation_version="sum:v1",
                transformation_digest="2" * 64,
                execution_reference=UUID(int=70),
                inputs=(
                    DerivationInput(
                        citation_id=CITATION,
                        grounding_validation_reference=UUID(int=79),
                    ),
                ),
            ),
        ),
    )

    assert result.disclosure_status is AnswerDisclosureStatus.ACCEPTED
    assert result.disclosable_claim_ids == (CLAIM,)


def test_derived_claim_rejects_citation_validation_as_grounding_lineage() -> None:
    factual = _answer()
    derived = factual.model_copy(
        update={
            "claims": (
                factual.claims[0].model_copy(update={"kind": AnswerClaimKind.DERIVED}),
            ),
            "relationships": (
                factual.relationships[0].model_copy(
                    update={"kind": ClaimEvidenceRelationshipKind.DERIVATION_INPUT}
                ),
            ),
        }
    )

    result = evaluate_answer_candidate(
        answer=derived,
        target_scope=_target(),
        observed_coverage=_coverage(),
        claim_assessments=(_grounded_assessment(),),
        derivations=(
            DeterministicDerivation(
                claim_id=CLAIM,
                boundary=_boundary(),
                transformation_name="sum_invoice_amounts",
                transformation_version="sum:v1",
                transformation_digest="2" * 64,
                execution_reference=UUID(int=70),
                inputs=(
                    DerivationInput(
                        citation_id=CITATION,
                        grounding_validation_reference=UUID(int=59),
                    ),
                ),
            ),
        ),
    )

    assert result.disclosure_status is AnswerDisclosureStatus.REJECTED
    assert result.disclosable_claim_ids == ()


def _grounded_assessment(**overrides: object) -> GroundedClaimAssessment:
    claim_id = overrides.get("claim_id", CLAIM)
    citation_id = {
        CLAIM: CITATION,
        CLAIM_B: CITATION_B,
        CLAIM_C: CITATION_C,
    }[claim_id]
    values: dict[str, object] = {
        "boundary": _boundary(),
        "claim_id": CLAIM,
        "grounding_status": ClaimGroundingStatus.GROUNDED,
        "authorization_status": ClaimAuthorizationStatus.CURRENT,
        "partial_utility": PartialClaimUtility.INDEPENDENTLY_USEFUL,
        "missing_coverage_impact": MissingCoverageImpact.IMMATERIAL,
        "evidence_validations": (
            ClaimEvidenceValidation(
                citation_id=citation_id,
                citation_validation_reference=UUID(int=50 + citation_id.int),
                semantic_support_validation_reference=UUID(int=70 + citation_id.int),
                authorization_validation_reference=UUID(int=90 + citation_id.int),
            ),
        ),
        "partial_validation_reference": UUID(int=110 + citation_id.int),
    }
    values.update(overrides)
    return GroundedClaimAssessment(**values)


@pytest.mark.parametrize(
    "status",
    (
        CoverageMemberStatus.UNCOVERED,
        CoverageMemberStatus.PROJECTION_GAP,
        CoverageMemberStatus.LIFECYCLE_GAP,
        CoverageMemberStatus.STALE,
        CoverageMemberStatus.UNKNOWN,
    ),
)
def test_explicit_coverage_gap_without_fallback_rejects_answer(
    status: CoverageMemberStatus,
) -> None:
    coverage = _coverage().model_copy(
        update={
            "members": (
                _coverage().members[0].model_copy(
                    update={"status": status, "proof_reference": None}
                ),
            )
        }
    )

    result = evaluate_answer_candidate(
        answer=_answer(),
        target_scope=_target(),
        observed_coverage=coverage,
        claim_assessments=(_grounded_assessment(),),
    )

    assert result.disclosure_status is AnswerDisclosureStatus.REJECTED
    assert result.coverage_status.value == "insufficient"
    assert result.safe_reason.value == "incomplete_authorized_coverage"


def test_represented_scope_without_explicit_proof_is_unproven() -> None:
    coverage = _coverage().model_copy(update={"proof_reference": None})

    result = evaluate_answer_candidate(
        answer=_answer(),
        target_scope=_target(),
        observed_coverage=coverage,
        claim_assessments=(_grounded_assessment(),),
    )

    assert result.disclosure_status is AnswerDisclosureStatus.REJECTED
    assert result.coverage_status.value == "unproven"
    assert result.safe_reason.value == "coverage_not_proven"


def test_partial_fallback_rejects_claim_that_missing_coverage_could_invalidate() -> None:
    incomplete = _coverage().model_copy(
        update={
            "members": (
                _coverage().members[0].model_copy(
                    update={
                        "status": CoverageMemberStatus.UNCOVERED,
                        "proof_reference": None,
                    }
                ),
            )
        }
    )

    result = evaluate_answer_candidate(
        answer=_answer(),
        target_scope=_target(),
        observed_coverage=incomplete,
        claim_assessments=(
            _grounded_assessment(
                missing_coverage_impact=MissingCoverageImpact.MATERIAL
            ),
        ),
        partial_fallback=PartialFallbackAuthorization(
            boundary=_boundary(),
            authority=PartialFallbackAuthority.CURRENT_REQUEST,
            authorization_reference=UUID(int=51),
            policy_version="partial-fallback:v1",
            applicability_reference=UUID(int=52),
        ),
    )

    assert result.disclosure_status is AnswerDisclosureStatus.REJECTED
    assert result.disclosable_claim_ids == ()


def test_stale_partial_authorization_from_another_attempt_is_rejected() -> None:
    incomplete = _coverage().model_copy(
        update={
            "members": (
                _coverage().members[0].model_copy(
                    update={
                        "status": CoverageMemberStatus.STALE,
                        "proof_reference": None,
                    }
                ),
            )
        }
    )
    stale_boundary = _boundary().model_copy(update={"attempt_id": UUID(int=999)})

    result = evaluate_answer_candidate(
        answer=_answer(),
        target_scope=_target(),
        observed_coverage=incomplete,
        claim_assessments=(_grounded_assessment(),),
        partial_fallback=PartialFallbackAuthorization(
            boundary=stale_boundary,
            authority=PartialFallbackAuthority.ACCEPTED_PREFERENCE,
            authorization_reference=UUID(int=51),
            policy_version="partial-fallback:v1",
            applicability_reference=UUID(int=52),
        ),
    )

    assert result.disclosure_status is AnswerDisclosureStatus.REJECTED
    assert result.coverage_status.value == "insufficient"


def test_mismatched_projection_boundary_cannot_prove_coverage() -> None:
    other_snapshot = _boundary().model_copy(
        update={"projection_snapshot_id": UUID(int=999)}
    )

    result = evaluate_answer_candidate(
        answer=_answer(),
        target_scope=_target(),
        observed_coverage=_coverage().model_copy(update={"boundary": other_snapshot}),
        claim_assessments=(_grounded_assessment(),),
    )

    assert result.disclosure_status is AnswerDisclosureStatus.REJECTED
    assert result.coverage_status.value == "unproven"


def test_out_of_scope_source_is_not_counted_or_identified_in_safe_result() -> None:
    hidden_source = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
    hidden_member = _coverage().members[0].model_copy(
        update={
            "source_object_id": hidden_source,
            "source_revision_id": UUID(int=998),
            "proof_reference": UUID(int=997),
        }
    )
    coverage = _coverage().model_copy(
        update={"members": (*_coverage().members, hidden_member)}
    )

    result = evaluate_answer_candidate(
        answer=_answer(),
        target_scope=_target(),
        observed_coverage=coverage,
        claim_assessments=(_grounded_assessment(),),
    )

    public_result = result.model_dump_json()
    assert result.coverage_status.value == "insufficient"
    assert str(hidden_source) not in public_result
    assert "source" not in public_result
    assert "count" not in public_result


def test_scope_and_coverage_records_are_versioned_and_immutable() -> None:
    target = _target()
    coverage = _coverage()

    assert target.schema_version == "target-completeness-scope:v1"
    assert coverage.schema_version == "observed-coverage:v1"
    with pytest.raises(ValidationError):
        target.target_scope_id = UUID(int=999)  # type: ignore[misc]
    with pytest.raises(ValidationError):
        coverage.proof_reference = None  # type: ignore[misc]


def test_answer_from_another_workspace_cannot_cross_the_evaluation_boundary() -> None:
    other_boundary = _boundary().model_copy(update={"workspace_id": UUID(int=999)})
    target = _target().model_copy(update={"boundary": other_boundary})
    coverage = _coverage().model_copy(update={"boundary": other_boundary})

    result = evaluate_answer_candidate(
        answer=_answer(),
        target_scope=target,
        observed_coverage=coverage,
        claim_assessments=(_grounded_assessment(),),
    )

    assert result.disclosure_status is AnswerDisclosureStatus.REJECTED
    assert result.disclosable_claim_ids == ()


def test_derived_claim_without_transformation_lineage_is_rejected() -> None:
    factual = _answer()
    derived = factual.model_copy(
        update={
            "claims": (
                factual.claims[0].model_copy(update={"kind": AnswerClaimKind.DERIVED}),
            ),
            "relationships": (
                factual.relationships[0].model_copy(
                    update={"kind": ClaimEvidenceRelationshipKind.DERIVATION_INPUT}
                ),
            ),
        }
    )

    result = evaluate_answer_candidate(
        answer=derived,
        target_scope=_target(),
        observed_coverage=_coverage(),
        claim_assessments=(_grounded_assessment(),),
    )

    assert result.disclosure_status is AnswerDisclosureStatus.REJECTED
    assert result.disclosable_claim_ids == ()


def test_contradiction_relationship_without_detected_conflict_is_rejected() -> None:
    result = evaluate_answer_candidate(
        answer=_conflicting_answer(),
        target_scope=_target(),
        observed_coverage=_coverage(),
        claim_assessments=tuple(
            _grounded_assessment(claim_id=claim_id)
            for claim_id in (CLAIM, CLAIM_B)
        ),
    )

    assert result.disclosure_status is AnswerDisclosureStatus.REJECTED


def test_authoritative_detected_conflict_does_not_require_candidate_relationship() -> None:
    answer = _conflicting_answer()
    answer_without_conflict = answer.model_copy(
        update={"relationships": answer.relationships[:2]}
    )

    result = evaluate_answer_candidate(
        answer=answer_without_conflict,
        target_scope=_target(),
        observed_coverage=_coverage(),
        claim_assessments=tuple(
            _grounded_assessment(claim_id=claim_id)
            for claim_id in (CLAIM, CLAIM_B)
        ),
        conflicts=(
            GroundedConflict(
                conflict_id=UUID(int=60),
                boundary=_boundary(),
                claim_ids=(CLAIM, CLAIM_B),
                relationship_ids=(UUID(int=24),),
                detection_reference=UUID(int=61),
                detection_policy_version="conflict-policy:v1",
            ),
        ),
    )

    assert result.disclosure_status is AnswerDisclosureStatus.ACCEPTED
    assert result.conflict_status is GroundedConflictStatus.PRESENTED


def test_stale_claim_grounding_assessment_is_rejected() -> None:
    stale = _grounded_assessment().model_copy(
        update={
            "boundary": _boundary().model_copy(update={"attempt_id": UUID(int=999)})
        }
    )

    result = evaluate_answer_candidate(
        answer=_answer(),
        target_scope=_target(),
        observed_coverage=_coverage(),
        claim_assessments=(stale,),
    )

    assert result.disclosure_status is AnswerDisclosureStatus.REJECTED


def test_overlapping_conflict_precedence_is_rejected_fail_closed() -> None:
    first = GroundedConflict(
        conflict_id=UUID(int=60),
        boundary=_boundary(),
        claim_ids=(CLAIM, CLAIM_B),
        relationship_ids=(UUID(int=24),),
        detection_reference=UUID(int=61),
        detection_policy_version="conflict-policy:v1",
    )
    duplicate = first.model_copy(
        update={"conflict_id": UUID(int=62), "detection_reference": UUID(int=63)}
    )

    result = evaluate_answer_candidate(
        answer=_conflicting_answer(),
        target_scope=_target(),
        observed_coverage=_coverage(),
        claim_assessments=tuple(
            _grounded_assessment(claim_id=claim_id)
            for claim_id in (CLAIM, CLAIM_B)
        ),
        conflicts=(first, duplicate),
        precedence_rules=(
            ApprovedPrecedenceRule(
                precedence_rule_id=UUID(int=64),
                boundary=_boundary(),
                conflict_id=first.conflict_id,
                winning_claim_id=CLAIM,
                rule_version="source-precedence:v2",
                approval_reference=UUID(int=65),
            ),
            ApprovedPrecedenceRule(
                precedence_rule_id=UUID(int=66),
                boundary=_boundary(),
                conflict_id=duplicate.conflict_id,
                winning_claim_id=CLAIM_B,
                rule_version="source-precedence:v2",
                approval_reference=UUID(int=67),
            ),
        ),
    )

    assert result.disclosure_status is AnswerDisclosureStatus.REJECTED


def test_partial_grounded_conflict_preserves_independent_semantics() -> None:
    incomplete = _coverage().model_copy(
        update={
            "members": (
                _coverage().members[0].model_copy(
                    update={
                        "status": CoverageMemberStatus.PROJECTION_GAP,
                        "proof_reference": None,
                    }
                ),
            )
        }
    )

    result = evaluate_answer_candidate(
        answer=_conflicting_answer(),
        target_scope=_target(),
        observed_coverage=incomplete,
        claim_assessments=tuple(
            _grounded_assessment(claim_id=claim_id)
            for claim_id in (CLAIM, CLAIM_B)
        ),
        partial_fallback=PartialFallbackAuthorization(
            boundary=_boundary(),
            authority=PartialFallbackAuthority.CURRENT_REQUEST,
            authorization_reference=UUID(int=51),
            policy_version="partial-fallback:v1",
            applicability_reference=UUID(int=52),
        ),
        conflicts=(
            GroundedConflict(
                conflict_id=UUID(int=60),
                boundary=_boundary(),
                claim_ids=(CLAIM, CLAIM_B),
                relationship_ids=(UUID(int=24),),
                detection_reference=UUID(int=61),
                detection_policy_version="conflict-policy:v1",
            ),
        ),
    )

    assert result.disclosure_status is AnswerDisclosureStatus.ACCEPTED
    assert result.partiality_status is AnswerPartialityStatus.PARTIAL
    assert result.conflict_status is GroundedConflictStatus.PRESENTED
    assert result.evaluation_lineage_reference == EVALUATION_LINEAGE


def test_overlapping_unresolved_conflict_edges_are_presented() -> None:
    result = evaluate_answer_candidate(
        answer=_overlapping_conflict_answer(),
        target_scope=_target(),
        observed_coverage=_coverage(),
        claim_assessments=tuple(
            _grounded_assessment(claim_id=claim_id)
            for claim_id in (CLAIM, CLAIM_B, CLAIM_C)
        ),
        conflicts=(
            GroundedConflict(
                conflict_id=UUID(int=60),
                boundary=_boundary(),
                claim_ids=(CLAIM, CLAIM_B),
                relationship_ids=(UUID(int=24),),
                detection_reference=UUID(int=61),
                detection_policy_version="conflict-policy:v1",
            ),
            GroundedConflict(
                conflict_id=UUID(int=62),
                boundary=_boundary(),
                claim_ids=(CLAIM_B, CLAIM_C),
                relationship_ids=(UUID(int=26),),
                detection_reference=UUID(int=63),
                detection_policy_version="conflict-policy:v1",
            ),
        ),
    )

    assert result.disclosure_status is AnswerDisclosureStatus.ACCEPTED
    assert result.conflict_status is GroundedConflictStatus.PRESENTED
    assert result.disclosable_claim_ids == (CLAIM, CLAIM_B, CLAIM_C)
