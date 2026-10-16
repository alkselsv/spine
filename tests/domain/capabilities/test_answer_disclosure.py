from datetime import datetime, timezone
from uuid import UUID

from spine.domain.capabilities import (
    AnswerAuthorizationOutcome,
    AnswerDisclosureRequest,
    AnswerEvaluationDecision,
    AnswerDisclosureValidation,
    AnswerDisclosureDecision,
    AnswerDisclosureStatus,
    AnswerOutcomeDecisionInput,
    AnswerPartialityStatus,
    AnswerQuestionOutcome,
    CitationValidationStatus,
    CurrentDisclosureEligibility,
    CoverageEvaluationStatus,
    DisclosureEligibilityReason,
    DisclosureEligibilityStatus,
    GroundedConflictStatus,
    RetrievalOutcome,
    SemanticSupportSummary,
    SemanticSupportSummaryStatus,
    compose_answer_disclosure,
)

from tests.domain.capabilities.test_answer_question_candidate import candidate_output, question


def uid(value: int) -> UUID:
    return UUID(int=value)


def _evaluation(candidate):
    evidence = candidate.answer.citations[0].evidence_reference
    return AnswerEvaluationDecision(
        boundary={
            "workspace_id": candidate.answer.workspace_id,
            "environment_id": candidate.answer.environment_id,
            "run_id": candidate.answer.run_id,
            "attempt_id": candidate.answer.attempt_id,
            "projection_snapshot_id": evidence.projection_snapshot_id,
            "activation_generation": evidence.activation_generation,
            "canonical_boundary_id": uid(801),
        },
        target_scope_id=uid(802),
        observed_coverage_id=uid(803),
        evaluation_lineage_reference=uid(804),
        disclosure_status=AnswerDisclosureStatus.ACCEPTED,
        coverage_status=CoverageEvaluationStatus.COMPLETE,
        partiality_status=AnswerPartialityStatus.EXHAUSTIVE,
        conflict_status=GroundedConflictStatus.NONE,
        safe_reason="complete",
        disclosable_claim_ids=tuple(claim.claim_id for claim in candidate.answer.claims),
    )


def _request(*, eligible: bool = True, cancellation_requested: bool = False):
    candidate = candidate_output()
    decision = AnswerOutcomeDecisionInput(
        question=question(),
        retrieval_outcome=RetrievalOutcome.EVIDENCE_AVAILABLE,
        authorization=AnswerAuthorizationOutcome.ALLOWED,
        candidate=candidate,
        citation_validation={
            "status": CitationValidationStatus.VALID,
            "reason_code": "valid",
            "validation_reference": uid(805),
            "citation_ids": tuple(item.citation_id for item in candidate.answer.citations),
        },
        semantic_support=SemanticSupportSummary(
            status=SemanticSupportSummaryStatus.SUPPORTED,
            validation_reference=uid(806),
            claim_ids=tuple(item.claim_id for item in candidate.answer.claims),
        ),
        evaluation=_evaluation(candidate),
        cancellation_requested=cancellation_requested,
    )
    return AnswerDisclosureRequest(
        request_id=uid(807),
        decision=decision,
        current_eligibility=CurrentDisclosureEligibility(
            status=(
                DisclosureEligibilityStatus.ELIGIBLE
                if eligible
                else DisclosureEligibilityStatus.INELIGIBLE
            ),
            validation_reference=uid(808),
            reason=(
                DisclosureEligibilityReason.CURRENT_AUTHORITY_CONFIRMED
                if eligible
                else DisclosureEligibilityReason.CURRENT_AUTHORITY_REJECTED
            ),
            workspace_id=question().scope.workspace_id,
            environment_id=question().scope.environment_id,
            run_id=question().correlation.run_id,
            attempt_id=question().correlation.attempt_id,
            validation_fact_ids=(uid(809),) if eligible else (),
        ),
    )


def test_grounded_answer_requires_all_validation_and_current_authority_facts() -> None:
    result = compose_answer_disclosure(
        _request(), accepted_at=datetime(2026, 10, 16, tzinfo=timezone.utc)
    )

    assert result.resolution.outcome is AnswerQuestionOutcome.ANSWERED
    assert result.accepted_answer is not None
    assert result.accepted_answer.validation.citation_validation_reference == uid(805)


def test_ineligible_current_authority_returns_safe_non_content_result() -> None:
    result = compose_answer_disclosure(
        _request(eligible=False), accepted_at=datetime(2026, 10, 16, tzinfo=timezone.utc)
    )

    assert result.resolution.outcome is AnswerQuestionOutcome.ABSTAINED
    assert result.accepted_answer is None
    assert result.output.answer is None
    assert "The policy applies" not in result.model_dump_json()


def test_cancellation_never_reaches_accepted_output() -> None:
    result = compose_answer_disclosure(
        _request(cancellation_requested=True),
        accepted_at=datetime(2026, 10, 16, tzinfo=timezone.utc),
    )

    assert result.resolution.outcome is AnswerQuestionOutcome.CANCELLED
    assert result.accepted_answer is None
    assert result.output.answer is None


def test_unsupported_semantics_return_safe_non_content_result() -> None:
    request = _request()
    unsupported = request.decision.semantic_support.model_copy(
        update={"status": SemanticSupportSummaryStatus.INSUFFICIENT}
    )
    request = request.model_copy(
        update={"decision": request.decision.model_copy(update={"semantic_support": unsupported})}
    )

    result = compose_answer_disclosure(
        request, accepted_at=datetime(2026, 10, 16, tzinfo=timezone.utc)
    )

    assert result.resolution.outcome is AnswerQuestionOutcome.ABSTAINED
    assert result.accepted_answer is None
    assert result.output.answer is None
    assert result.validation is not None
