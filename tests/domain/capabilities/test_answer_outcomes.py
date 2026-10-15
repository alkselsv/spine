from __future__ import annotations

from uuid import UUID

import pytest

from spine.domain.capabilities import (
    AnswerAuthorizationOutcome,
    AnswerEvaluationDecision,
    AnswerEvaluationSafeReason,
    AnswerOutcomeAttemptBudget,
    AnswerOutcomeDecisionInput,
    AnswerOutcomeResolution,
    AnswerQuestionOutcome,
    AnswerQuestionSafeReasonCode,
    AnswerQuestionCandidateOutput,
    CitationValidationReasonCode,
    CitationValidationStatus,
    CitationValidationSummary,
    ClarificationAssessment,
    RetrievalOutcome,
    SemanticSupportSummary,
    SemanticSupportSummaryStatus,
    resolve_answer_outcome,
)
from spine.domain.capabilities.answer_evaluation import AttemptProjectionBoundary

from tests.domain.capabilities.test_answer_question_candidate import (
    candidate_output,
    question,
)


FOLLOW_UP_RUN = UUID(int=1003)


def _candidate() -> AnswerQuestionCandidateOutput:
    return candidate_output()


def _evaluation() -> AnswerEvaluationDecision:
    candidate = _candidate().answer
    citation = candidate.citations[0]
    return AnswerEvaluationDecision(
        boundary=AttemptProjectionBoundary(
            workspace_id=candidate.workspace_id,
            environment_id=candidate.environment_id,
            run_id=candidate.run_id,
            attempt_id=candidate.attempt_id,
            projection_snapshot_id=citation.evidence_reference.projection_snapshot_id,
            activation_generation=citation.evidence_reference.activation_generation,
            canonical_boundary_id=UUID(int=800),
        ),
        target_scope_id=UUID(int=801),
        observed_coverage_id=UUID(int=802),
        evaluation_lineage_reference=UUID(int=803),
        disclosure_status="accepted",
        coverage_status="complete",
        partiality_status="exhaustive",
        conflict_status="none",
        safe_reason=AnswerEvaluationSafeReason.COMPLETE,
        disclosable_claim_ids=tuple(claim.claim_id for claim in candidate.claims),
    )


def _citation_validation(
    status: CitationValidationStatus = CitationValidationStatus.VALID,
    reason: CitationValidationReasonCode = CitationValidationReasonCode.VALID,
) -> CitationValidationSummary:
    candidate = _candidate().answer
    return CitationValidationSummary(
        status=status,
        reason_code=reason,
        validation_reference=UUID(int=804),
        citation_ids=tuple(item.citation_id for item in candidate.citations),
    )


def _semantic_support(
    status: SemanticSupportSummaryStatus = SemanticSupportSummaryStatus.SUPPORTED,
) -> SemanticSupportSummary:
    candidate = _candidate().answer
    return SemanticSupportSummary(
        status=status,
        validation_reference=UUID(int=805),
        claim_ids=tuple(claim.claim_id for claim in candidate.claims),
    )


def _decision(**overrides: object) -> AnswerOutcomeDecisionInput:
    candidate = _candidate()
    values: dict[str, object] = {
        "question": question(),
        "retrieval_outcome": RetrievalOutcome.EVIDENCE_AVAILABLE,
        "authorization": AnswerAuthorizationOutcome.ALLOWED,
        "candidate": candidate,
        "citation_validation": _citation_validation(),
        "semantic_support": _semantic_support(),
        "evaluation": _evaluation(),
        "disclosure_eligible": True,
        "attempt_budget": AnswerOutcomeAttemptBudget(
            attempts_used=2,
            maximum_attempts=2,
            retryable=False,
        ),
    }
    values.update(overrides)
    return AnswerOutcomeDecisionInput(**values)


def test_resolver_maps_accepted_grounded_candidate_to_answered() -> None:
    result = resolve_answer_outcome(_decision())

    assert isinstance(result, AnswerOutcomeResolution)
    assert result.outcome is AnswerQuestionOutcome.ANSWERED
    assert result.output is not None
    assert result.output.outcome is AnswerQuestionOutcome.ANSWERED
    assert result.output.answer == _candidate().answer
    assert result.evaluation_reason is AnswerEvaluationSafeReason.COMPLETE


def test_resolver_accepts_only_an_explicitly_authorized_partial_evaluation() -> None:
    evaluation = _evaluation().model_copy(
        update={
            "coverage_status": "insufficient",
            "partiality_status": "partial",
            "safe_reason": AnswerEvaluationSafeReason.PARTIAL_AUTHORIZED,
        }
    )

    result = resolve_answer_outcome(_decision(evaluation=evaluation))

    assert result.outcome is AnswerQuestionOutcome.ANSWERED
    assert result.evaluation_reason is AnswerEvaluationSafeReason.PARTIAL_AUTHORIZED


def test_resolver_preserves_grounded_conflict_as_internal_evaluation_fact() -> None:
    evaluation = _evaluation().model_copy(
        update={
            "conflict_status": "presented",
            "safe_reason": AnswerEvaluationSafeReason.GROUNDED_CONFLICT,
        }
    )

    result = resolve_answer_outcome(_decision(evaluation=evaluation))

    assert result.outcome is AnswerQuestionOutcome.ANSWERED
    assert result.evaluation_reason is AnswerEvaluationSafeReason.GROUNDED_CONFLICT


@pytest.mark.parametrize(
    ("overrides", "reason"),
    (
        (
            {"retrieval_outcome": RetrievalOutcome.INSUFFICIENT_EVIDENCE},
            AnswerQuestionSafeReasonCode.NO_USABLE_EVIDENCE,
        ),
        (
            {
                "semantic_support": _semantic_support(
                    SemanticSupportSummaryStatus.INSUFFICIENT
                )
            },
            AnswerQuestionSafeReasonCode.INSUFFICIENT_SUPPORT,
        ),
        (
            {"authorization": AnswerAuthorizationOutcome.DENIED},
            AnswerQuestionSafeReasonCode.AUTHORIZATION_DENIED,
        ),
        (
            {
                "citation_validation": _citation_validation(
                    CitationValidationStatus.REJECTED,
                    CitationValidationReasonCode.EVIDENCE_INVALIDATED,
                )
            },
            AnswerQuestionSafeReasonCode.EVIDENCE_INVALIDATED,
        ),
    ),
)
def test_resolver_maps_non_answer_facts_to_safe_terminal_outcomes(
    overrides: dict[str, object], reason: AnswerQuestionSafeReasonCode
) -> None:
    result = resolve_answer_outcome(_decision(**overrides))

    assert result.outcome in {
        AnswerQuestionOutcome.ABSTAINED,
        AnswerQuestionOutcome.DENIED,
    }
    assert result.output is not None
    assert result.output.reason is not None
    assert result.output.reason.code is reason
    assert result.output.answer is None


def test_resolver_maps_mandatory_validator_unavailability_to_failed() -> None:
    result = resolve_answer_outcome(
        _decision(
            semantic_support=_semantic_support(
                SemanticSupportSummaryStatus.VALIDATOR_UNAVAILABLE
            )
        )
    )

    assert result.outcome is AnswerQuestionOutcome.FAILED
    assert result.output is not None
    assert result.output.reason.code is AnswerQuestionSafeReasonCode.MANDATORY_PROCESSING_FAILED


def test_resolver_maps_cancellation_before_other_candidate_facts() -> None:
    result = resolve_answer_outcome(_decision(cancellation_requested=True))

    assert result.outcome is AnswerQuestionOutcome.CANCELLED
    assert result.output is not None
    assert result.output.reason.code is AnswerQuestionSafeReasonCode.CANCELLED


def test_resolver_requires_bounded_attempts_before_abstaining_for_support() -> None:
    result = resolve_answer_outcome(
        _decision(
            semantic_support=_semantic_support(
                SemanticSupportSummaryStatus.INSUFFICIENT
            ),
            attempt_budget=AnswerOutcomeAttemptBudget(
                attempts_used=1,
                maximum_attempts=2,
                retryable=True,
            ),
        )
    )

    assert result.retry_required is True
    assert result.outcome is None
    assert result.output is None

    exhausted = resolve_answer_outcome(
        _decision(
            semantic_support=_semantic_support(
                SemanticSupportSummaryStatus.INSUFFICIENT
            ),
            attempt_budget=AnswerOutcomeAttemptBudget(
                attempts_used=2,
                maximum_attempts=2,
                retryable=True,
            ),
        )
    )
    assert exhausted.outcome is AnswerQuestionOutcome.ABSTAINED
    assert exhausted.output.reason.code is AnswerQuestionSafeReasonCode.INSUFFICIENT_SUPPORT


def test_safe_ambiguity_closes_current_outcome_without_answer_content() -> None:
    result = resolve_answer_outcome(
        _decision(
            clarification=ClarificationAssessment(
                prompt="Which policy version should I use?"
            )
        )
    )

    assert result.outcome is AnswerQuestionOutcome.CLARIFICATION_REQUIRED
    assert result.output is not None
    assert result.output.clarification is not None
    assert result.output.answer is None
    assert result.output.reason.code is AnswerQuestionSafeReasonCode.AMBIGUOUS_QUESTION


def test_clarification_follow_up_links_new_run_without_mutating_old_request() -> None:
    resolution = resolve_answer_outcome(
        _decision(
            clarification=ClarificationAssessment(prompt="Which policy version?")
        )
    )
    follow_up = question().model_copy(
        update={
            "subject": {"text": "Use the 2026 policy."},
            "correlation": {
                "run_id": FOLLOW_UP_RUN,
                "attempt_id": UUID(int=1004),
                "trace_id": UUID(int=1005),
                "causation_id": question().correlation.run_id,
            },
        }
    )

    link = resolution.link_follow_up(follow_up)

    assert link.previous_run_id == question().correlation.run_id
    assert link.follow_up_run_id == FOLLOW_UP_RUN
    assert follow_up.correlation.causation_id == question().correlation.run_id
    assert resolution.output.clarification.prompt == "Which policy version?"


def test_resolver_never_answers_when_disclosure_is_ineligible() -> None:
    result = resolve_answer_outcome(_decision(disclosure_eligible=False))

    assert result.outcome is AnswerQuestionOutcome.ABSTAINED
    assert result.output.reason.code is AnswerQuestionSafeReasonCode.EVIDENCE_INVALIDATED


def test_resolver_rejects_raw_diagnostic_details_at_the_contract_boundary() -> None:
    with pytest.raises(ValueError):
        AnswerOutcomeDecisionInput(
            **_decision().model_dump(mode="python"),
            diagnostic="provider secret: customer@example.com",
        )
