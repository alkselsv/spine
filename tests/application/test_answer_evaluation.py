from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.answer_evaluation import evaluate_answer_candidate
from spine.domain.capabilities import (
    ApprovedPrecedenceRule,
    AnswerClaim,
    AnswerClaimKind,
    AnswerClaimLocation,
    AnswerContent,
    AttemptProjectionBoundary,
    Citation,
    ClaimEvidenceRelationship,
    ClaimEvidenceRelationshipKind,
    CoverageMemberStatus,
    DerivationInput,
    DeterministicDerivation,
    GroundedConflict,
    GroundedClaimAssessment,
    ObservedCoverage,
    ObservedCoverageMember,
    PartialFallbackAuthorization,
    PartialFallbackAuthority,
    TargetCompletenessScope,
    TargetScopeMember,
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


def _target() -> TargetCompletenessScope:
    return TargetCompletenessScope(
        target_scope_id=TARGET,
        boundary=_boundary(),
        derivation_reference=UUID(int=30),
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
        claim_assessments=(
            GroundedClaimAssessment(
                claim_id=CLAIM,
                grounded=True,
                current_authorized=True,
                independently_useful=True,
                stable_under_incomplete_coverage=True,
                validation_references=(UUID(int=50),),
            ),
        ),
    )

    assert result.answer_status.value == "complete"
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
        claim_assessments=(
            GroundedClaimAssessment(
                claim_id=CLAIM,
                grounded=True,
                current_authorized=True,
                independently_useful=True,
                stable_under_incomplete_coverage=True,
                validation_references=(UUID(int=50),),
            ),
        ),
        partial_fallback=PartialFallbackAuthorization(
            boundary=_boundary(),
            authority=PartialFallbackAuthority.CURRENT_REQUEST,
            authorization_reference=UUID(int=51),
        ),
    )

    assert result.answer_status.value == "partial"
    assert result.coverage_status.value == "insufficient"
    assert result.disclosable_claim_ids == (CLAIM,)


def test_current_grounded_conflict_is_presented_without_selecting_a_winner() -> None:
    assessments = tuple(
        GroundedClaimAssessment(
            claim_id=claim_id,
            grounded=True,
            current_authorized=True,
            independently_useful=True,
            stable_under_incomplete_coverage=True,
            validation_references=(UUID(int=50 + index),),
        )
        for index, claim_id in enumerate((CLAIM, CLAIM_B))
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
                detection_reference=UUID(int=61),
                detection_policy_version="conflict-policy:v1",
            ),
        ),
    )

    assert result.answer_status.value == "grounded_conflict"
    assert result.disclosable_claim_ids == (CLAIM, CLAIM_B)


def test_approved_deterministic_precedence_selects_the_winning_claim() -> None:
    conflict = GroundedConflict(
        conflict_id=UUID(int=60),
        boundary=_boundary(),
        claim_ids=(CLAIM, CLAIM_B),
        detection_reference=UUID(int=61),
        detection_policy_version="conflict-policy:v1",
    )
    assessments = tuple(
        GroundedClaimAssessment(
            claim_id=claim_id,
            grounded=True,
            current_authorized=True,
            independently_useful=True,
            stable_under_incomplete_coverage=True,
            validation_references=(UUID(int=50 + index),),
        )
        for index, claim_id in enumerate((CLAIM, CLAIM_B))
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

    assert result.answer_status.value == "complete"
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
        claim_assessments=(
            GroundedClaimAssessment(
                claim_id=CLAIM,
                grounded=True,
                current_authorized=True,
                independently_useful=True,
                stable_under_incomplete_coverage=True,
                validation_references=(UUID(int=50),),
            ),
        ),
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
                        grounding_validation_reference=UUID(int=71),
                    ),
                ),
            ),
        ),
    )

    assert result.answer_status.value == "complete"
    assert result.disclosable_claim_ids == (CLAIM,)


def _grounded_assessment(**overrides: object) -> GroundedClaimAssessment:
    values: dict[str, object] = {
        "claim_id": CLAIM,
        "grounded": True,
        "current_authorized": True,
        "independently_useful": True,
        "stable_under_incomplete_coverage": True,
        "validation_references": (UUID(int=50),),
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

    assert result.answer_status.value == "rejected"
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

    assert result.answer_status.value == "rejected"
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
            _grounded_assessment(stable_under_incomplete_coverage=False),
        ),
        partial_fallback=PartialFallbackAuthorization(
            boundary=_boundary(),
            authority=PartialFallbackAuthority.CURRENT_REQUEST,
            authorization_reference=UUID(int=51),
        ),
    )

    assert result.answer_status.value == "rejected"
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
        ),
    )

    assert result.answer_status.value == "rejected"
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

    assert result.answer_status.value == "rejected"
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

    assert result.answer_status.value == "rejected"
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

    assert result.answer_status.value == "rejected"
    assert result.disclosable_claim_ids == ()
