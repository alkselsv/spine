"""Pure composition contracts for the final Q&A disclosure gate.

The candidate is an internal, untrusted value.  Only ``AcceptedGroundedAnswer``
may cross the accepted-output port; every other branch contains a safe terminal
``AnswerQuestionOutput`` and never the candidate content.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from spine.domain.capabilities.answer_outcomes import (
    AnswerOutcomeDecisionInput,
    AnswerOutcomeResolution,
    resolve_answer_outcome,
)
from spine.domain.capabilities.answer_question import (
    AnswerContent,
    AnswerQuestionOutput,
    AnswerQuestionOutcome,
    AnswerQuestionSafeReason,
    AnswerQuestionSafeReasonCode,
    LineageReference,
)
from spine.domain.common import DefinitionModel


ANSWER_DISCLOSURE_REQUEST_SCHEMA = "answer-disclosure-request:v1"
ANSWER_DISCLOSURE_DECISION_SCHEMA = "answer-disclosure-decision:v1"
ACCEPTED_GROUNDED_ANSWER_SCHEMA = "accepted-grounded-answer:v1"
ANSWER_DISCLOSURE_RECORD_SCHEMA = "answer-disclosure-record:v1"
_DIGEST = r"^[0-9a-f]{64}$"


def _id(value: UUID, field_name: str) -> UUID:
    if value.int == 0:
        raise ValueError(f"{field_name} must be non-zero")
    return value


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


class DisclosureEligibilityStatus(str, Enum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    UNAVAILABLE = "unavailable"


class DisclosureEligibilityReason(str, Enum):
    CURRENT_AUTHORITY_CONFIRMED = "current_authority_confirmed"
    CURRENT_AUTHORITY_REJECTED = "current_authority_rejected"
    CURRENT_AUTHORITY_UNAVAILABLE = "current_authority_unavailable"


class CurrentDisclosureEligibility(DefinitionModel):
    """Typed current-authority result required before answer disclosure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["current-disclosure-eligibility:v1"] = (
        "current-disclosure-eligibility:v1"
    )
    status: DisclosureEligibilityStatus
    validation_reference: UUID
    reason: DisclosureEligibilityReason
    dependency_ids: tuple[UUID, ...] = ()

    @property
    def can_disclose(self) -> bool:
        return self.status is DisclosureEligibilityStatus.ELIGIBLE

    @model_validator(mode="after")
    def validate_result(self) -> "CurrentDisclosureEligibility":
        _id(self.validation_reference, "validation_reference")
        if any(identifier.int == 0 for identifier in self.dependency_ids):
            raise ValueError("disclosure dependency IDs must be non-zero")
        if len(set(self.dependency_ids)) != len(self.dependency_ids):
            raise ValueError("disclosure dependency IDs must be unique")
        expected = {
            DisclosureEligibilityStatus.ELIGIBLE: DisclosureEligibilityReason.CURRENT_AUTHORITY_CONFIRMED,
            DisclosureEligibilityStatus.INELIGIBLE: DisclosureEligibilityReason.CURRENT_AUTHORITY_REJECTED,
            DisclosureEligibilityStatus.UNAVAILABLE: DisclosureEligibilityReason.CURRENT_AUTHORITY_UNAVAILABLE,
        }
        if self.reason is not expected[self.status]:
            raise ValueError("disclosure eligibility status and reason are inconsistent")
        return self


class AnswerDisclosureValidation(DefinitionModel):
    """Immutable references proving every mandatory validation stage ran."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["answer-disclosure-validation:v1"] = (
        "answer-disclosure-validation:v1"
    )
    citation_validation_reference: UUID
    semantic_support_validation_reference: UUID
    evaluation_lineage_reference: UUID
    disclosure_eligibility_reference: UUID
    context_bundle_id: UUID
    context_bundle_version: int = Field(ge=1)
    context_bundle_digest: str = Field(pattern=_DIGEST)
    claim_ids: tuple[UUID, ...] = Field(min_length=1)
    citation_ids: tuple[UUID, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_references(self) -> "AnswerDisclosureValidation":
        references = (
            self.citation_validation_reference,
            self.semantic_support_validation_reference,
            self.evaluation_lineage_reference,
            self.disclosure_eligibility_reference,
            self.context_bundle_id,
        )
        if any(reference.int == 0 for reference in references):
            raise ValueError("disclosure validation references must be non-zero")
        if any(identifier.int == 0 for identifier in (*self.claim_ids, *self.citation_ids)):
            raise ValueError("disclosure validation identifiers must be non-zero")
        if len(set(self.claim_ids)) != len(self.claim_ids):
            raise ValueError("disclosure claim IDs must be unique")
        if len(set(self.citation_ids)) != len(self.citation_ids):
            raise ValueError("disclosure citation IDs must be unique")
        return self


class AcceptedGroundedAnswer(DefinitionModel):
    """The only content-bearing value allowed past the final gate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["accepted-grounded-answer:v1"] = ACCEPTED_GROUNDED_ANSWER_SCHEMA
    answer: AnswerContent
    validation: AnswerDisclosureValidation
    accepted_at: datetime

    @model_validator(mode="after")
    def validate_answer(self) -> "AcceptedGroundedAnswer":
        _aware(self.accepted_at, "accepted_at")
        claim_ids = tuple(claim.claim_id for claim in self.answer.claims)
        citation_ids = tuple(citation.citation_id for citation in self.answer.citations)
        if claim_ids != self.validation.claim_ids or citation_ids != self.validation.citation_ids:
            raise ValueError("accepted answer validation lineage is incomplete")
        if any(
            (
                citation.context_bundle.bundle.bundle_id != self.validation.context_bundle_id
                or citation.context_bundle.bundle.bundle_version
                != self.validation.context_bundle_version
                or citation.context_bundle.bundle.bundle_digest
                != self.validation.context_bundle_digest
            )
            for citation in self.answer.citations
        ):
            raise ValueError("accepted answer uses a different context bundle")
        return self


class AnswerDisclosureRequest(DefinitionModel):
    """All typed facts the final gate is allowed to consume."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["answer-disclosure-request:v1"] = ANSWER_DISCLOSURE_REQUEST_SCHEMA
    request_id: UUID
    decision: AnswerOutcomeDecisionInput
    current_eligibility: CurrentDisclosureEligibility | None = None

    @property
    def command_digest(self) -> str:
        payload = self.model_copy(
            update={
                "decision": self.decision.model_copy(update={"disclosure_eligible": False})
            }
        ).model_dump(mode="json")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @model_validator(mode="after")
    def validate_candidate(self) -> "AnswerDisclosureRequest":
        _id(self.request_id, "request_id")
        if self.decision.candidate is not None:
            candidate = self.decision.candidate
            if candidate.question != self.decision.question:
                raise ValueError("candidate question does not match disclosure request")
        return self


class AnswerDisclosureDecision(DefinitionModel):
    """Result of pure gate composition; safe branches never carry a candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["answer-disclosure-decision:v1"] = ANSWER_DISCLOSURE_DECISION_SCHEMA
    resolution: AnswerOutcomeResolution
    accepted_answer: AcceptedGroundedAnswer | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "AnswerDisclosureDecision":
        if self.accepted_answer is not None:
            if self.resolution.outcome is not AnswerQuestionOutcome.ANSWERED:
                raise ValueError("only answered resolution can carry accepted content")
            if self.resolution.output is None or self.resolution.output.answer != self.accepted_answer.answer:
                raise ValueError("accepted answer does not match terminal resolution")
        elif self.resolution.outcome is AnswerQuestionOutcome.ANSWERED:
            raise ValueError("answered resolution requires accepted content")
        return self

    @property
    def output(self) -> AnswerQuestionOutput:
        if self.resolution.output is None:
            raise ValueError("disclosure decision is not terminal")
        return self.resolution.output


class AnswerDisclosureRecord(DefinitionModel):
    """Immutable persisted gate result without rejected candidate content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["answer-disclosure-record:v1"] = ANSWER_DISCLOSURE_RECORD_SCHEMA
    request_id: UUID
    request_digest: str = Field(pattern=_DIGEST)
    workspace_id: UUID
    environment_id: UUID
    run_id: UUID
    attempt_id: UUID
    outcome: AnswerQuestionOutcome
    accepted_answer: AcceptedGroundedAnswer | None = None
    safe_output: AnswerQuestionOutput | None = None
    created_at: datetime

    @model_validator(mode="after")
    def validate_record(self) -> "AnswerDisclosureRecord":
        for name in (
            "request_id",
            "workspace_id",
            "environment_id",
            "run_id",
            "attempt_id",
        ):
            _id(getattr(self, name), name)
        _aware(self.created_at, "created_at")
        if (self.accepted_answer is None) == (self.safe_output is None):
            raise ValueError("a disclosure record must contain exactly one result payload")
        if self.accepted_answer is not None:
            if self.outcome is not AnswerQuestionOutcome.ANSWERED:
                raise ValueError("accepted disclosure record must be answered")
        else:
            if self.safe_output is None or self.safe_output.outcome is AnswerQuestionOutcome.ANSWERED:
                raise ValueError("safe disclosure record cannot contain answer content")
            if self.safe_output.outcome is not self.outcome:
                raise ValueError("safe disclosure outcome does not match record")
        return self


def compose_answer_disclosure(
    request: AnswerDisclosureRequest,
    *,
    accepted_at: datetime,
) -> AnswerDisclosureDecision:
    """Run the binary disclosure predicate after all validation facts exist."""

    eligibility = request.current_eligibility
    if request.decision.candidate is not None and (
        eligibility is None
        or eligibility.status is DisclosureEligibilityStatus.UNAVAILABLE
    ):
        return AnswerDisclosureDecision(
            resolution=_safe_resolution(
                request,
                AnswerQuestionSafeReasonCode.MANDATORY_PROCESSING_FAILED,
                outcome=AnswerQuestionOutcome.FAILED,
            )
        )
    eligible = eligibility is not None and eligibility.can_disclose
    decision = request.decision.model_copy(update={"disclosure_eligible": eligible})
    resolution = resolve_answer_outcome(decision)
    if resolution.retry_required:
        resolution = _safe_resolution(
            request,
            AnswerQuestionSafeReasonCode.INSUFFICIENT_SUPPORT,
        )
    if resolution.outcome is not AnswerQuestionOutcome.ANSWERED:
        return AnswerDisclosureDecision(resolution=resolution)

    candidate = request.decision.candidate
    evaluation = request.decision.evaluation
    citation_validation = request.decision.citation_validation
    semantic_support = request.decision.semantic_support
    if candidate is None or evaluation is None or citation_validation is None or semantic_support is None:
        return AnswerDisclosureDecision(
            resolution=_safe_resolution(request, AnswerQuestionSafeReasonCode.REJECTED_CANDIDATE)
        )
    assert eligibility is not None
    validation = AnswerDisclosureValidation(
        citation_validation_reference=citation_validation.validation_reference,
        semantic_support_validation_reference=semantic_support.validation_reference,
        evaluation_lineage_reference=evaluation.evaluation_lineage_reference,
        disclosure_eligibility_reference=eligibility.validation_reference,
        context_bundle_id=candidate.context_bundle.bundle.bundle_id,
        context_bundle_version=candidate.context_bundle.bundle.bundle_version,
        context_bundle_digest=candidate.context_bundle.bundle.bundle_digest,
        claim_ids=tuple(claim.claim_id for claim in candidate.answer.claims),
        citation_ids=tuple(citation.citation_id for citation in candidate.answer.citations),
    )
    return AnswerDisclosureDecision(
        resolution=resolution,
        accepted_answer=AcceptedGroundedAnswer(
            answer=candidate.answer,
            validation=validation,
            accepted_at=accepted_at,
        ),
    )


def _safe_resolution(
    request: AnswerDisclosureRequest,
    reason: AnswerQuestionSafeReasonCode,
    *,
    outcome: AnswerQuestionOutcome = AnswerQuestionOutcome.ABSTAINED,
) -> AnswerOutcomeResolution:
    return AnswerOutcomeResolution(
        outcome=outcome,
        output=AnswerQuestionOutput(
            correlation=request.decision.question.correlation,
            scope=request.decision.question.scope,
            outcome=outcome,
            reason=AnswerQuestionSafeReason(code=reason),
        ),
    )


# Compatibility aliases used by callers that use the shorter product terms.
GroundedAnswer = AcceptedGroundedAnswer
AcceptedAnswer = AcceptedGroundedAnswer


__all__ = [
    "ACCEPTED_GROUNDED_ANSWER_SCHEMA",
    "ANSWER_DISCLOSURE_DECISION_SCHEMA",
    "ANSWER_DISCLOSURE_RECORD_SCHEMA",
    "ANSWER_DISCLOSURE_REQUEST_SCHEMA",
    "AcceptedAnswer",
    "AcceptedGroundedAnswer",
    "AnswerDisclosureDecision",
    "AnswerDisclosureRequest",
    "AnswerDisclosureRecord",
    "AnswerDisclosureValidation",
    "CurrentDisclosureEligibility",
    "DisclosureEligibilityReason",
    "DisclosureEligibilityStatus",
    "GroundedAnswer",
    "compose_answer_disclosure",
]
