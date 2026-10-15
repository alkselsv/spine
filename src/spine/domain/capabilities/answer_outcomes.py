"""Deterministic final-outcome resolution for one governed Q&A run.

This boundary consumes facts already produced by authorization, retrieval,
candidate validation, semantic support, and completeness evaluation.  It does
not call any of those services and never exposes their protected diagnostics.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from spine.domain.capabilities.answer_evaluation import (
    AnswerDisclosureStatus,
    AnswerEvaluationDecision,
    AnswerEvaluationSafeReason,
)
from spine.domain.capabilities.answer_question import (
    AnswerQuestionInput,
    AnswerQuestionOutcome,
    AnswerQuestionOutput,
    AnswerQuestionSafeReason,
    AnswerQuestionSafeReasonCode,
    AnswerQuestionScope,
    ClarificationRequest,
    LineageReference,
    RetrievalOutcome,
)
from spine.domain.capabilities.answer_question_candidate import (
    AnswerQuestionCandidateOutput,
)
from spine.domain.common import ValidatedDefinitionModel


ANSWER_OUTCOME_DECISION_INPUT_SCHEMA = "answer-outcome-decision-input:v1"
ANSWER_OUTCOME_RESOLUTION_SCHEMA = "answer-outcome-resolution:v1"
ANSWER_CLARIFICATION_LINK_SCHEMA = "answer-clarification-link:v1"


class AnswerAuthorizationOutcome(str, Enum):
    """Authorization result supplied by the canonical authorization boundary."""

    ALLOWED = "allowed"
    DENIED = "denied"


class CitationValidationStatus(str, Enum):
    VALID = "valid"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


class CitationValidationReasonCode(str, Enum):
    VALID = "valid"
    IDENTITY_INVALID = "identity_invalid"
    EVIDENCE_INVALIDATED = "evidence_invalidated"
    AUTHORIZATION_DENIED = "authorization_denied"
    AUTHORITY_STALE = "authority_stale"
    VALIDATION_UNAVAILABLE = "validation_unavailable"


class SemanticSupportSummaryStatus(str, Enum):
    """Aggregate status after all claim-level support checks for an attempt."""

    SUPPORTED = "supported"
    INSUFFICIENT = "insufficient"
    VALIDATOR_UNAVAILABLE = "validator_unavailable"
    VALIDATOR_FAILED = "validator_failed"


class _OutcomeModel(ValidatedDefinitionModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )


class CitationValidationSummary(_OutcomeModel):
    """Content-free aggregate of the citation validator's current facts."""

    schema_version: Literal["citation-validation-summary:v1"] = (
        "citation-validation-summary:v1"
    )
    status: CitationValidationStatus
    reason_code: CitationValidationReasonCode
    validation_reference: UUID
    citation_ids: tuple[UUID, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_reason(self) -> "CitationValidationSummary":
        if any(identifier.int == 0 for identifier in self.citation_ids):
            raise ValueError("citation validation identifiers must be non-zero")
        if len(set(self.citation_ids)) != len(self.citation_ids):
            raise ValueError("citation validation identifiers must be unique")
        if self.validation_reference.int == 0:
            raise ValueError("citation validation reference must be non-zero")
        expected = {
            CitationValidationStatus.VALID: CitationValidationReasonCode.VALID,
            CitationValidationStatus.UNAVAILABLE: CitationValidationReasonCode.VALIDATION_UNAVAILABLE,
        }
        if self.status in expected and self.reason_code is not expected[self.status]:
            raise ValueError("citation validation status and reason are inconsistent")
        if (
            self.status is CitationValidationStatus.REJECTED
            and self.reason_code is CitationValidationReasonCode.VALID
        ):
            raise ValueError("rejected citations cannot have a valid reason")
        return self


class SemanticSupportSummary(_OutcomeModel):
    """Content-free aggregate of claim-level semantic-support records."""

    schema_version: Literal["semantic-support-summary:v1"] = (
        "semantic-support-summary:v1"
    )
    status: SemanticSupportSummaryStatus
    validation_reference: UUID
    claim_ids: tuple[UUID, ...] = Field(min_length=1)
    mandatory: bool = True

    @model_validator(mode="after")
    def validate_ids(self) -> "SemanticSupportSummary":
        if self.validation_reference.int == 0:
            raise ValueError("semantic support validation reference must be non-zero")
        if any(identifier.int == 0 for identifier in self.claim_ids):
            raise ValueError("semantic support claim identifiers must be non-zero")
        if len(set(self.claim_ids)) != len(self.claim_ids):
            raise ValueError("semantic support claim identifiers must be unique")
        return self


class ClarificationAssessment(_OutcomeModel):
    """A safe, bounded ambiguity prompt with no source or provider details."""

    schema_version: Literal["clarification-assessment:v1"] = (
        "clarification-assessment:v1"
    )
    prompt: str = Field(min_length=1, max_length=32_768)

    @model_validator(mode="after")
    def validate_prompt(self) -> "ClarificationAssessment":
        if not self.prompt.strip():
            raise ValueError("clarification prompt must not be blank")
        return self


class AnswerOutcomeAttemptBudget(_OutcomeModel):
    """Bounded technical-attempt state supplied by the execution owner."""

    schema_version: Literal["answer-outcome-attempt-budget:v1"] = (
        "answer-outcome-attempt-budget:v1"
    )
    attempts_used: int = Field(default=1, ge=1, le=5)
    maximum_attempts: int = Field(default=1, ge=1, le=5)
    retryable: bool = False

    @model_validator(mode="after")
    def validate_budget(self) -> "AnswerOutcomeAttemptBudget":
        if self.attempts_used > self.maximum_attempts:
            raise ValueError("attempts_used cannot exceed maximum_attempts")
        return self

    @property
    def exhausted(self) -> bool:
        return self.attempts_used >= self.maximum_attempts


class AnswerOutcomeDecisionInput(_OutcomeModel):
    """All typed, non-diagnostic facts needed for one terminal decision."""

    schema_version: Literal["answer-outcome-decision-input:v1"] = (
        ANSWER_OUTCOME_DECISION_INPUT_SCHEMA
    )
    question: AnswerQuestionInput
    retrieval_outcome: RetrievalOutcome
    authorization: AnswerAuthorizationOutcome
    candidate: AnswerQuestionCandidateOutput | None = None
    citation_validation: CitationValidationSummary | None = None
    semantic_support: SemanticSupportSummary | None = None
    evaluation: AnswerEvaluationDecision | None = None
    disclosure_eligible: bool = False
    clarification: ClarificationAssessment | None = None
    cancellation_requested: bool = False
    attempt_budget: AnswerOutcomeAttemptBudget = AnswerOutcomeAttemptBudget()

    @model_validator(mode="after")
    def validate_candidate_lineage(self) -> "AnswerOutcomeDecisionInput":
        if self.candidate is not None and self.candidate.question != self.question:
            raise ValueError("candidate question does not match outcome request")
        if self.candidate is None and any(
            value is not None
            for value in (self.citation_validation, self.semantic_support, self.evaluation)
        ):
            raise ValueError("candidate validation facts require a candidate")
        return self


class AnswerClarificationRunLink(_OutcomeModel):
    """Immutable causal link from a closed clarification run to a new run."""

    schema_version: Literal["answer-clarification-link:v1"] = (
        ANSWER_CLARIFICATION_LINK_SCHEMA
    )
    previous_run_id: UUID
    follow_up_run_id: UUID
    workspace_id: UUID
    environment_id: UUID

    @model_validator(mode="after")
    def validate_link(self) -> "AnswerClarificationRunLink":
        identifiers = (
            self.previous_run_id,
            self.follow_up_run_id,
            self.workspace_id,
            self.environment_id,
        )
        if any(identifier.int == 0 for identifier in identifiers):
            raise ValueError("clarification run link identifiers must be non-zero")
        if self.previous_run_id == self.follow_up_run_id:
            raise ValueError("clarification follow-up must create a new run")
        return self


class AnswerOutcomeResolution(_OutcomeModel):
    """Stable terminal result or an explicit request to spend another attempt."""

    schema_version: Literal["answer-outcome-resolution:v1"] = (
        ANSWER_OUTCOME_RESOLUTION_SCHEMA
    )
    outcome: AnswerQuestionOutcome | None
    output: AnswerQuestionOutput | None
    retry_required: bool = False
    evaluation_reason: AnswerEvaluationSafeReason | None = None

    @model_validator(mode="after")
    def validate_resolution(self) -> "AnswerOutcomeResolution":
        if self.retry_required:
            if self.outcome is not None or self.output is not None:
                raise ValueError("retry resolution cannot be terminal")
            return self
        if self.outcome is None or self.output is None:
            raise ValueError("terminal resolution requires a typed output")
        if self.output.outcome is not self.outcome:
            raise ValueError("resolution and output outcomes must match")
        return self

    def link_follow_up(self, follow_up: AnswerQuestionInput) -> AnswerClarificationRunLink:
        if self.outcome is not AnswerQuestionOutcome.CLARIFICATION_REQUIRED:
            raise ValueError("only clarification outcomes can link a follow-up")
        if follow_up.correlation.causation_id != self._run_id_from_output():
            raise ValueError("follow-up request must causally reference the clarification run")
        if follow_up.scope != self._scope_from_output():
            raise ValueError("follow-up request has a different Q&A scope")
        if follow_up.correlation.attempt_id == self._attempt_id_from_output():
            raise ValueError("follow-up request must create a new attempt")
        return AnswerClarificationRunLink(
            previous_run_id=self._run_id_from_output(),
            follow_up_run_id=follow_up.correlation.run_id,
            workspace_id=follow_up.scope.workspace_id,
            environment_id=follow_up.scope.environment_id,
        )

    def _run_id_from_output(self) -> UUID:
        if self.output is None:
            raise ValueError("clarification resolution has no output")
        return self.output.correlation.run_id

    def _scope_from_output(self) -> AnswerQuestionScope:
        if self.output is None:
            raise ValueError("clarification resolution has no output")
        return self.output.scope

    def _attempt_id_from_output(self) -> UUID:
        if self.output is None:
            raise ValueError("clarification resolution has no output")
        return self.output.correlation.attempt_id


def resolve_answer_outcome(
    decision: AnswerOutcomeDecisionInput,
) -> AnswerOutcomeResolution:
    """Resolve one Q&A attempt without performing I/O or authorization."""

    if type(decision) is not AnswerOutcomeDecisionInput:
        raise TypeError("answer outcome decision input is invalid")
    request = decision.question

    if decision.cancellation_requested:
        return _safe_resolution(request, AnswerQuestionOutcome.CANCELLED, AnswerQuestionSafeReasonCode.CANCELLED)
    if decision.authorization is AnswerAuthorizationOutcome.DENIED:
        return _safe_resolution(
            request,
            AnswerQuestionOutcome.DENIED,
            AnswerQuestionSafeReasonCode.AUTHORIZATION_DENIED,
        )
    if decision.clarification is not None:
        return _clarification_resolution(request, decision.clarification)
    if decision.retrieval_outcome is RetrievalOutcome.INSUFFICIENT_EVIDENCE:
        return _safe_resolution(
            request,
            AnswerQuestionOutcome.ABSTAINED,
            AnswerQuestionSafeReasonCode.NO_USABLE_EVIDENCE,
        )
    if decision.retrieval_outcome in {
        RetrievalOutcome.UNAVAILABLE,
        RetrievalOutcome.FAILED,
    }:
        return _safe_resolution(
            request,
            AnswerQuestionOutcome.FAILED,
            AnswerQuestionSafeReasonCode.MANDATORY_PROCESSING_FAILED,
        )

    citation_validation = decision.citation_validation
    if citation_validation is None:
        return _safe_resolution(
            request,
            AnswerQuestionOutcome.ABSTAINED,
            AnswerQuestionSafeReasonCode.REJECTED_CANDIDATE,
        )
    if citation_validation.status is CitationValidationStatus.UNAVAILABLE:
        return _safe_resolution(
            request,
            AnswerQuestionOutcome.FAILED,
            AnswerQuestionSafeReasonCode.MANDATORY_PROCESSING_FAILED,
        )
    if citation_validation.status is CitationValidationStatus.REJECTED:
        if citation_validation.reason_code is CitationValidationReasonCode.AUTHORIZATION_DENIED:
            return _safe_resolution(
                request,
                AnswerQuestionOutcome.DENIED,
                AnswerQuestionSafeReasonCode.AUTHORIZATION_DENIED,
            )
        reason = (
            AnswerQuestionSafeReasonCode.EVIDENCE_INVALIDATED
            if citation_validation.reason_code
            in {
                CitationValidationReasonCode.EVIDENCE_INVALIDATED,
                CitationValidationReasonCode.AUTHORITY_STALE,
            }
            else AnswerQuestionSafeReasonCode.REJECTED_CANDIDATE
        )
        return _safe_resolution(request, AnswerQuestionOutcome.ABSTAINED, reason)

    semantic_support = decision.semantic_support
    if semantic_support is None:
        return _safe_resolution(
            request,
            AnswerQuestionOutcome.ABSTAINED,
            AnswerQuestionSafeReasonCode.REJECTED_CANDIDATE,
        )
    if semantic_support.status in {
        SemanticSupportSummaryStatus.VALIDATOR_UNAVAILABLE,
        SemanticSupportSummaryStatus.VALIDATOR_FAILED,
    }:
        if semantic_support.mandatory:
            return _safe_resolution(
                request,
                AnswerQuestionOutcome.FAILED,
                AnswerQuestionSafeReasonCode.MANDATORY_PROCESSING_FAILED,
            )
        return _safe_resolution(
            request,
            AnswerQuestionOutcome.ABSTAINED,
            AnswerQuestionSafeReasonCode.INSUFFICIENT_SUPPORT,
        )
    if semantic_support.status is SemanticSupportSummaryStatus.INSUFFICIENT:
        if decision.attempt_budget.retryable and not decision.attempt_budget.exhausted:
            return AnswerOutcomeResolution(
                outcome=None,
                output=None,
                retry_required=True,
            )
        return _safe_resolution(
            request,
            AnswerQuestionOutcome.ABSTAINED,
            AnswerQuestionSafeReasonCode.INSUFFICIENT_SUPPORT,
        )

    evaluation = decision.evaluation
    candidate = decision.candidate
    if evaluation is None or candidate is None:
        return _safe_resolution(
            request,
            AnswerQuestionOutcome.ABSTAINED,
            AnswerQuestionSafeReasonCode.REJECTED_CANDIDATE,
        )
    if not decision.disclosure_eligible:
        return _safe_resolution(
            request,
            AnswerQuestionOutcome.ABSTAINED,
            AnswerQuestionSafeReasonCode.EVIDENCE_INVALIDATED,
        )
    if not _validation_facts_match_candidate(
        candidate=candidate,
        citation_validation=citation_validation,
        semantic_support=semantic_support,
        evaluation=evaluation,
    ):
        return _safe_resolution(
            request,
            AnswerQuestionOutcome.ABSTAINED,
            AnswerQuestionSafeReasonCode.REJECTED_CANDIDATE,
        )
    if evaluation.disclosure_status is not AnswerDisclosureStatus.ACCEPTED:
        return _safe_resolution(
            request,
            AnswerQuestionOutcome.ABSTAINED,
            _safe_reason_for_rejected_evaluation(evaluation),
        )
    if set(evaluation.disclosable_claim_ids) != {
        claim.claim_id for claim in candidate.answer.claims
    }:
        return _safe_resolution(
            request,
            AnswerQuestionOutcome.ABSTAINED,
            AnswerQuestionSafeReasonCode.UNRESOLVED_CONFLICT,
        )
    output = AnswerQuestionOutput(
        correlation=request.correlation,
        scope=request.scope,
        outcome=AnswerQuestionOutcome.ANSWERED,
        answer=candidate.answer,
        lineage=(
            LineageReference(
                kind="context_bundle",
                identifier=candidate.context_bundle.bundle.bundle_id,
                schema_version="context-bundle:v1",
            ),
        ),
    )
    return AnswerOutcomeResolution(
        outcome=AnswerQuestionOutcome.ANSWERED,
        output=output,
        evaluation_reason=evaluation.safe_reason,
    )


def _validation_facts_match_candidate(
    *,
    candidate: AnswerQuestionCandidateOutput,
    citation_validation: CitationValidationSummary,
    semantic_support: SemanticSupportSummary,
    evaluation: AnswerEvaluationDecision,
) -> bool:
    answer = candidate.answer
    citation_ids = {citation.citation_id for citation in answer.citations}
    claim_ids = {claim.claim_id for claim in answer.claims}
    if set(citation_validation.citation_ids) != citation_ids:
        return False
    if set(semantic_support.claim_ids) != claim_ids:
        return False
    if evaluation.boundary.workspace_id != answer.workspace_id:
        return False
    if evaluation.boundary.environment_id != answer.environment_id:
        return False
    if evaluation.boundary.run_id != answer.run_id or evaluation.boundary.attempt_id != answer.attempt_id:
        return False
    return all(
        citation.evidence_reference.projection_snapshot_id
        == evaluation.boundary.projection_snapshot_id
        and citation.evidence_reference.activation_generation
        == evaluation.boundary.activation_generation
        for citation in answer.citations
    )


def _safe_reason_for_rejected_evaluation(
    evaluation: AnswerEvaluationDecision,
) -> AnswerQuestionSafeReasonCode:
    return {
        AnswerEvaluationSafeReason.COVERAGE_NOT_PROVEN: AnswerQuestionSafeReasonCode.INCOMPLETE_AUTHORIZED_COVERAGE,
        AnswerEvaluationSafeReason.INCOMPLETE_AUTHORIZED_COVERAGE: AnswerQuestionSafeReasonCode.INCOMPLETE_AUTHORIZED_COVERAGE,
        AnswerEvaluationSafeReason.GROUNDED_CONFLICT: AnswerQuestionSafeReasonCode.UNRESOLVED_CONFLICT,
    }.get(evaluation.safe_reason, AnswerQuestionSafeReasonCode.REJECTED_CANDIDATE)


def _clarification_resolution(
    request: AnswerQuestionInput,
    clarification: ClarificationAssessment,
) -> AnswerOutcomeResolution:
    return AnswerOutcomeResolution(
        outcome=AnswerQuestionOutcome.CLARIFICATION_REQUIRED,
        output=AnswerQuestionOutput(
            correlation=request.correlation,
            scope=request.scope,
            outcome=AnswerQuestionOutcome.CLARIFICATION_REQUIRED,
            clarification=ClarificationRequest(prompt=clarification.prompt),
            reason=AnswerQuestionSafeReason(
                code=AnswerQuestionSafeReasonCode.AMBIGUOUS_QUESTION
            ),
        ),
    )


def _safe_resolution(
    request: AnswerQuestionInput,
    outcome: AnswerQuestionOutcome,
    reason: AnswerQuestionSafeReasonCode,
) -> AnswerOutcomeResolution:
    return AnswerOutcomeResolution(
        outcome=outcome,
        output=AnswerQuestionOutput(
            correlation=request.correlation,
            scope=request.scope,
            outcome=outcome,
            reason=AnswerQuestionSafeReason(code=reason),
        ),
    )


__all__ = [
    "ANSWER_CLARIFICATION_LINK_SCHEMA",
    "ANSWER_OUTCOME_DECISION_INPUT_SCHEMA",
    "ANSWER_OUTCOME_RESOLUTION_SCHEMA",
    "AnswerAuthorizationOutcome",
    "AnswerClarificationRunLink",
    "AnswerOutcomeAttemptBudget",
    "AnswerOutcomeDecisionInput",
    "AnswerOutcomeResolution",
    "CitationValidationReasonCode",
    "CitationValidationStatus",
    "CitationValidationSummary",
    "ClarificationAssessment",
    "SemanticSupportSummary",
    "SemanticSupportSummaryStatus",
    "resolve_answer_outcome",
]
