"""Application adapter for the pure answer-outcome decision boundary."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

from spine.application.citation_validation import (
    CitationValidationOutcome,
    CitationValidationReasonCode as ApplicationCitationReasonCode,
    RejectedCitation,
    ValidCitation,
)
from spine.domain.capabilities.answer_outcomes import (
    AnswerAuthorizationOutcome,
    AnswerOutcomeAttemptBudget,
    AnswerOutcomeDecisionInput,
    AnswerOutcomeResolution,
    CitationValidationReasonCode,
    CitationValidationStatus,
    CitationValidationSummary,
    ClarificationAssessment,
    SemanticSupportSummary,
    SemanticSupportSummaryStatus,
    resolve_answer_outcome as resolve_domain_answer_outcome,
)
from spine.domain.capabilities.answer_question import (
    AnswerQuestionInput,
    RetrievalOutcome,
)
from spine.domain.capabilities.answer_question_candidate import (
    AnswerQuestionCandidateOutput,
)
from spine.domain.capabilities.answer_evaluation import AnswerEvaluationDecision
from spine.domain.capabilities.answer_evaluation import (
    GroundedConflict,
    GroundedClaimAssessment,
    ObservedCoverage,
    PartialFallbackAuthorization,
    TargetCompletenessScope,
)
from spine.domain.capabilities.semantic_support import (
    SemanticSupportOutcome,
    SemanticSupportResult,
)


@dataclass(frozen=True, slots=True)
class AnswerOutcomeFacts:
    """Typed outputs from the already-composed Q&A validation stages."""

    question: AnswerQuestionInput
    retrieval_outcome: RetrievalOutcome
    authorization: AnswerAuthorizationOutcome
    candidate: AnswerQuestionCandidateOutput | None = None
    citation_results: tuple[CitationValidationOutcome, ...] = ()
    semantic_results: tuple[SemanticSupportResult, ...] = ()
    evaluation: AnswerEvaluationDecision | None = None
    target_scope: TargetCompletenessScope | None = None
    observed_coverage: ObservedCoverage | None = None
    partial_fallback: PartialFallbackAuthorization | None = None
    claim_assessments: tuple[GroundedClaimAssessment, ...] = ()
    conflicts: tuple[GroundedConflict, ...] = ()
    disclosure_eligible: bool = False
    clarification: ClarificationAssessment | None = None
    cancellation_requested: bool = False
    attempt_budget: AnswerOutcomeAttemptBudget = AnswerOutcomeAttemptBudget()


class AnswerOutcomeResolver:
    """Map typed validator/evaluator results to one safe final outcome."""

    def resolve(self, facts: AnswerOutcomeFacts) -> AnswerOutcomeResolution:
        return resolve_domain_answer_outcome(build_decision_input(facts))


def resolve_answer_outcome(facts: AnswerOutcomeFacts) -> AnswerOutcomeResolution:
    """Application entry point used by workflow composition roots."""

    return AnswerOutcomeResolver().resolve(facts)


def build_decision_input(facts: AnswerOutcomeFacts) -> AnswerOutcomeDecisionInput:
    """Convert concrete validator records into the domain-safe aggregate."""

    if type(facts) is not AnswerOutcomeFacts:
        raise TypeError("answer outcome facts are invalid")
    if facts.candidate is None and (facts.citation_results or facts.semantic_results):
        raise ValueError("validation results require a candidate")
    citation_validation = _citation_summary(facts)
    semantic_support = _semantic_summary(facts)
    return AnswerOutcomeDecisionInput(
        question=facts.question,
        retrieval_outcome=facts.retrieval_outcome,
        authorization=facts.authorization,
        candidate=facts.candidate,
        citation_validation=citation_validation,
        semantic_support=semantic_support,
        evaluation=facts.evaluation,
        disclosure_eligible=facts.disclosure_eligible,
        clarification=facts.clarification,
        cancellation_requested=facts.cancellation_requested,
        attempt_budget=facts.attempt_budget,
    )


def _citation_summary(facts: AnswerOutcomeFacts) -> CitationValidationSummary | None:
    if facts.candidate is None or not facts.citation_results:
        return None
    expected = tuple(citation.citation_id for citation in facts.candidate.answer.citations)
    if len(facts.citation_results) != len(expected):
        raise ValueError("citation validation results are incomplete")
    for expected_id, result in zip(expected, facts.citation_results, strict=True):
        if isinstance(result, ValidCitation) and result.facts.citation_id != expected_id:
            raise ValueError("citation validation result identifies another citation")
        if isinstance(result, ValidCitation):
            bundle = facts.candidate.context_bundle.bundle
            if (
                result.facts.context_bundle_id != bundle.bundle_id
                or result.facts.context_bundle_version != bundle.bundle_version
                or result.facts.context_bundle_digest != bundle.bundle_digest
            ):
                raise ValueError("citation validation result has another bundle lineage")
    if all(isinstance(result, ValidCitation) for result in facts.citation_results):
        status = CitationValidationStatus.VALID
        reason = CitationValidationReasonCode.VALID
    else:
        rejected = tuple(
            result for result in facts.citation_results if isinstance(result, RejectedCitation)
        )
        if not rejected or any(
            not isinstance(result, (ValidCitation, RejectedCitation))
            for result in facts.citation_results
        ):
            raise ValueError("citation validation results have an invalid shape")
        reason = _rejected_citation_reason(rejected)
        status = (
            CitationValidationStatus.UNAVAILABLE
            if reason is CitationValidationReasonCode.VALIDATION_UNAVAILABLE
            else CitationValidationStatus.REJECTED
        )
    reference = uuid5(
        NAMESPACE_URL,
        "spine:answer-citation-summary:v1:" + ":".join(
            str(_validation_reference(result)) for result in facts.citation_results
        ),
    )
    return CitationValidationSummary(
        status=status,
        reason_code=reason,
        validation_reference=reference,
        citation_ids=expected,
    )


def _semantic_summary(facts: AnswerOutcomeFacts) -> SemanticSupportSummary | None:
    if facts.candidate is None or not facts.semantic_results:
        return None
    expected = {claim.claim_id for claim in facts.candidate.answer.claims}
    actual = {result.claim_id for result in facts.semantic_results}
    if actual != expected or len(actual) != len(facts.semantic_results):
        raise ValueError("semantic support results are incomplete")
    expected_citations = {citation.citation_id for citation in facts.candidate.answer.citations}
    for result in facts.semantic_results:
        if (
            result.answer_id != facts.candidate.answer.answer_id
            or result.scope.workspace_id != facts.question.scope.workspace_id
            or result.scope.environment_id != facts.question.scope.environment_id
            or result.correlation.run_id != facts.question.correlation.run_id
            or result.correlation.attempt_id != facts.question.correlation.attempt_id
            or result.context_bundle != facts.candidate.context_bundle
            or not set(result.citation_ids) <= expected_citations
        ):
            raise ValueError("semantic support result has incompatible answer lineage")
    outcomes = {result.outcome for result in facts.semantic_results}
    if SemanticSupportOutcome.VALIDATOR_FAILED in outcomes:
        status = SemanticSupportSummaryStatus.VALIDATOR_FAILED
    elif SemanticSupportOutcome.VALIDATOR_UNAVAILABLE in outcomes:
        status = SemanticSupportSummaryStatus.VALIDATOR_UNAVAILABLE
    elif outcomes - {SemanticSupportOutcome.SUPPORTED}:
        status = SemanticSupportSummaryStatus.INSUFFICIENT
    else:
        status = SemanticSupportSummaryStatus.SUPPORTED
    reference = uuid5(
        NAMESPACE_URL,
        "spine:answer-semantic-summary:v1:" + ":".join(
            str(result.validation_record_id)
            for result in facts.semantic_results
        ),
    )
    return SemanticSupportSummary(
        status=status,
        validation_reference=reference,
        claim_ids=tuple(sorted(actual, key=lambda identifier: identifier.int)),
        mandatory=any(result.policy.mandatory for result in facts.semantic_results),
    )


def _validation_reference(result: CitationValidationOutcome) -> UUID:
    if isinstance(result, ValidCitation):
        return result.validation_reference
    if isinstance(result, RejectedCitation):
        return result.validation_reference
    raise ValueError("citation validation result is invalid")


def _rejected_citation_reason(
    results: tuple[RejectedCitation, ...],
) -> CitationValidationReasonCode:
    reasons = {result.reason_code for result in results}
    if ApplicationCitationReasonCode.VALIDATION_UNAVAILABLE in reasons:
        return CitationValidationReasonCode.VALIDATION_UNAVAILABLE
    if ApplicationCitationReasonCode.AUTHORIZATION_DENIED in reasons:
        return CitationValidationReasonCode.AUTHORIZATION_DENIED
    if ApplicationCitationReasonCode.EVIDENCE_INVALIDATED in reasons:
        return CitationValidationReasonCode.EVIDENCE_INVALIDATED
    if ApplicationCitationReasonCode.AUTHORITY_STALE in reasons:
        return CitationValidationReasonCode.AUTHORITY_STALE
    return CitationValidationReasonCode.IDENTITY_INVALID


__all__ = [
    "AnswerOutcomeFacts",
    "AnswerOutcomeResolver",
    "build_decision_input",
    "resolve_answer_outcome",
]
