from uuid import UUID

from spine.domain.capabilities import (
    AnswerDisclosureStatus,
    AnswerPartialityStatus,
    CoverageEvaluationStatus,
    CoverageMemberStatus,
    GroundedConflict,
    GroundedConflictStatus,
    PartialFallbackAuthorization,
    PartialFallbackAuthority,
)
from spine.infrastructure.qa_agent_runtime import (
    GROUNDED_CONFLICT_POLICY_DIGEST,
    GROUNDED_CONFLICT_POLICY_VERSION,
    MISSING_COVERAGE_IMPACT_POLICY_DIGEST,
    MISSING_COVERAGE_IMPACT_POLICY_VERSION,
)
from tests.domain.capabilities.test_answer_evaluation import (
    CLAIM,
    CLAIM_B,
    _answer,
    _boundary,
    _conflicting_answer,
    _coverage,
    _grounded_assessment,
    _target,
    evaluate_answer_candidate,
)


def test_grounded_conflict_policy_is_versioned_and_prompt_pinned() -> None:
    assert GROUNDED_CONFLICT_POLICY_VERSION == "qa.grounded-conflict:r1"
    assert (
        GROUNDED_CONFLICT_POLICY_DIGEST
        == "1bc231665b24df40be6b3e327d4200e686c519f36569611e953fa0794967c2d5"
    )


def test_missing_coverage_impact_policy_is_versioned_and_prompt_pinned() -> None:
    assert MISSING_COVERAGE_IMPACT_POLICY_VERSION == "qa.missing-coverage-impact:r1"
    assert (
        MISSING_COVERAGE_IMPACT_POLICY_DIGEST
        == "c54370651d3ce7b1c990178e76ffe4fe1dbe38cb5fa364e3e8c91032ac72d029"
    )


def test_complete_coverage_qualifies_as_exhaustive() -> None:
    result = evaluate_answer_candidate(
        answer=_answer(),
        target_scope=_target(),
        observed_coverage=_coverage(),
        claim_assessments=(_grounded_assessment(),),
    )

    assert result.disclosure_status is AnswerDisclosureStatus.ACCEPTED
    assert result.coverage_status is CoverageEvaluationStatus.COMPLETE
    assert result.partiality_status is AnswerPartialityStatus.EXHAUSTIVE


def test_partial_grounded_conflict_requires_explicit_fallback_and_keeps_both_states() -> None:
    coverage = _coverage().model_copy(
        update={
            "members": tuple(
                member.model_copy(
                    update={
                        "status": CoverageMemberStatus.PROJECTION_GAP,
                        "proof_reference": None,
                    }
                )
                for member in _coverage().members
            )
        }
    )
    conflict = GroundedConflict(
        conflict_id=UUID(int=60),
        boundary=_boundary(),
        claim_ids=(CLAIM, CLAIM_B),
        relationship_ids=(UUID(int=24),),
        detection_reference=UUID(int=61),
        detection_policy_version="grounded-conflict-detector:v1",
    )

    without_fallback = evaluate_answer_candidate(
        answer=_conflicting_answer(),
        target_scope=_target(),
        observed_coverage=coverage,
        claim_assessments=tuple(
            _grounded_assessment(claim_id=claim_id)
            for claim_id in (CLAIM, CLAIM_B)
        ),
        conflicts=(conflict,),
    )
    with_fallback = evaluate_answer_candidate(
        answer=_conflicting_answer(),
        target_scope=_target(),
        observed_coverage=coverage,
        claim_assessments=tuple(
            _grounded_assessment(claim_id=claim_id)
            for claim_id in (CLAIM, CLAIM_B)
        ),
        partial_fallback=PartialFallbackAuthorization(
            boundary=_boundary(),
            authority=PartialFallbackAuthority.CURRENT_REQUEST,
            authorization_reference=UUID(int=62),
            policy_version="qa.ask-admission-request:v3",
            applicability_reference=UUID(int=63),
        ),
        conflicts=(conflict,),
    )

    assert without_fallback.disclosure_status is AnswerDisclosureStatus.REJECTED
    assert with_fallback.disclosure_status is AnswerDisclosureStatus.ACCEPTED
    assert with_fallback.partiality_status is AnswerPartialityStatus.PARTIAL
    assert with_fallback.conflict_status is GroundedConflictStatus.PRESENTED
