"""Framework-independent contracts for one governed ``answer_question`` run.

The models in this module describe the capability boundary only.  They do not
retrieve context, authorize disclosure, call a provider, or persist a run.
Retrieval outcomes intentionally have a separate type from terminal answer
outcomes so an adapter must make that mapping explicit.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
import json
from typing import Any, Literal, TypeVar
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from spine.domain.common import ValidatedDefinitionModel


ANSWER_QUESTION_INPUT_SCHEMA = "answer-question.input:v1"
ANSWER_QUESTION_OUTPUT_SCHEMA = "answer-question.output:v1"
_SHA256 = r"^[0-9a-f]{64}$"
_MAX_TEXT_LENGTH = 32_768


class AnswerQuestionContractError(ValueError):
    """Raised when serialized capability data cannot be accepted safely."""


class AnswerQuestionOutcome(str, Enum):
    """The complete and closed set of terminal Q&A outcomes."""

    ANSWERED = "answered"
    CLARIFICATION_REQUIRED = "clarification_required"
    ABSTAINED = "abstained"
    DENIED = "denied"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RetrievalOutcome(str, Enum):
    """Context Broker outcomes, which are not terminal Q&A outcomes."""

    EVIDENCE_AVAILABLE = "evidence_available"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class AnswerQuestionSafeReasonCode(str, Enum):
    """Disclosure-safe, non-content-bearing terminal reason categories."""

    AMBIGUOUS_QUESTION = "ambiguous_question"
    NO_USABLE_EVIDENCE = "no_usable_evidence"
    INSUFFICIENT_SUPPORT = "insufficient_support"
    UNRESOLVED_CONFLICT = "unresolved_conflict"
    INCOMPLETE_AUTHORIZED_COVERAGE = "incomplete_authorized_coverage"
    UNMET_FRESHNESS = "unmet_freshness"
    EVIDENCE_INVALIDATED = "evidence_invalidated"
    REJECTED_CANDIDATE = "rejected_candidate"
    AUTHORIZATION_DENIED = "authorization_denied"
    MANDATORY_PROCESSING_FAILED = "mandatory_processing_failed"
    CANCELLED = "cancelled"


class _AnswerQuestionModel(ValidatedDefinitionModel):
    """Closed immutable model with validated public copy and parse paths."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )

    def canonical_json(self) -> str:
        """Serialize the contract with stable keys and separators."""

        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def _require_non_zero(value: UUID, field_name: str) -> UUID:
    if value.int == 0:
        raise ValueError(f"{field_name} must be non-zero")
    return value


def _require_text(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value


class QuestionSubject(_AnswerQuestionModel):
    """The user-authored question subject, kept separate from answer text."""

    text: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _require_text(value, "question subject")


class AnswerQuestionScope(_AnswerQuestionModel):
    """The exact tenant and environment scope of one capability invocation."""

    workspace_id: UUID
    environment_id: UUID

    @field_validator("workspace_id", "environment_id")
    @classmethod
    def validate_identifier(cls, value: UUID, info: Any) -> UUID:
        return _require_non_zero(value, info.field_name)


class RunAttemptCorrelation(_AnswerQuestionModel):
    """Stable run, attempt and diagnostic identities carried by the contract."""

    run_id: UUID
    attempt_id: UUID
    trace_id: UUID
    correlation_id: UUID | None = None
    causation_id: UUID | None = None

    @field_validator(
        "run_id",
        "attempt_id",
        "trace_id",
        "correlation_id",
        "causation_id",
    )
    @classmethod
    def validate_identifier(cls, value: UUID | None, info: Any) -> UUID | None:
        if value is None:
            return None
        return _require_non_zero(value, info.field_name)


class CitationReference(_AnswerQuestionModel):
    """A structured pointer to one exact, bundle-scoped source revision."""

    citation_id: UUID
    context_bundle_id: UUID
    evidence_id: str = Field(min_length=1, max_length=256)
    source_object_id: UUID
    source_revision_id: UUID
    original_sha256: str = Field(pattern=_SHA256)
    locator_contract_version: str = Field(min_length=1, max_length=128)
    locator_kind: Literal[
        "page",
        "sheet_row",
        "message",
        "character_range",
        "record_field",
        "custom",
    ]
    locator: str = Field(min_length=1, max_length=512)
    evidence_digest: str = Field(pattern=_SHA256)

    @field_validator(
        "citation_id",
        "context_bundle_id",
        "source_object_id",
        "source_revision_id",
    )
    @classmethod
    def validate_identifier(cls, value: UUID, info: Any) -> UUID:
        return _require_non_zero(value, info.field_name)


class AnswerClaim(_AnswerQuestionModel):
    """One independently addressable claim in an accepted answer."""

    claim_id: UUID
    text: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)
    citation_ids: tuple[UUID, ...] = Field(min_length=1)

    @field_validator("claim_id")
    @classmethod
    def validate_claim_id(cls, value: UUID) -> UUID:
        return _require_non_zero(value, "claim_id")

    @field_validator("text")
    @classmethod
    def validate_claim_text(cls, value: str) -> str:
        return _require_text(value, "claim text")

    @field_validator("citation_ids")
    @classmethod
    def validate_citation_ids(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if any(identifier.int == 0 for identifier in value):
            raise ValueError("claim citation identifiers must be non-zero")
        if len(set(value)) != len(value):
            raise ValueError("claim citation identifiers must be unique")
        return value


class AnswerContent(_AnswerQuestionModel):
    """Accepted answer text plus structured claim and citation references."""

    text: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)
    claims: tuple[AnswerClaim, ...] = Field(min_length=1)
    citations: tuple[CitationReference, ...] = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def validate_answer_text(cls, value: str) -> str:
        return _require_text(value, "answer content")

    @model_validator(mode="after")
    def validate_claim_references(self) -> AnswerContent:
        citation_ids = {citation.citation_id for citation in self.citations}
        if len(citation_ids) != len(self.citations):
            raise ValueError("answer citation identifiers must be unique")
        for claim in self.claims:
            if not set(claim.citation_ids) <= citation_ids:
                raise ValueError("answer claim references an unknown citation")
        return self


class LineageReference(_AnswerQuestionModel):
    """Opaque typed lineage identity retained by a Q&A result."""

    kind: Literal[
        "run",
        "attempt",
        "context_bundle",
        "source_revision",
        "artifact",
        "citation",
    ]
    identifier: UUID
    schema_version: str = Field(min_length=1, max_length=128)

    @field_validator("identifier")
    @classmethod
    def validate_identifier(cls, value: UUID) -> UUID:
        return _require_non_zero(value, "lineage identifier")


class AnswerQuestionSafeReason(_AnswerQuestionModel):
    """A public reason code that contains no source-sensitive diagnostics."""

    code: AnswerQuestionSafeReasonCode


class ClarificationRequest(_AnswerQuestionModel):
    """Safe clarification text, without answer claims or source diagnostics."""

    prompt: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        return _require_text(value, "clarification prompt")


class AnswerQuestionInput(_AnswerQuestionModel):
    """Versioned input accepted by the ``answer_question`` capability."""

    schema_version: Literal[ANSWER_QUESTION_INPUT_SCHEMA] = ANSWER_QUESTION_INPUT_SCHEMA
    capability: Literal["answer_question"] = "answer_question"
    subject: QuestionSubject
    scope: AnswerQuestionScope
    correlation: RunAttemptCorrelation


class AnswerQuestionOutput(_AnswerQuestionModel):
    """Versioned terminal result with outcome-specific payload validation."""

    schema_version: Literal[ANSWER_QUESTION_OUTPUT_SCHEMA] = ANSWER_QUESTION_OUTPUT_SCHEMA
    capability: Literal["answer_question"] = "answer_question"
    correlation: RunAttemptCorrelation
    scope: AnswerQuestionScope
    outcome: AnswerQuestionOutcome
    answer: AnswerContent | None = None
    clarification: ClarificationRequest | None = None
    reason: AnswerQuestionSafeReason | None = None
    lineage: tuple[LineageReference, ...] = ()

    @model_validator(mode="after")
    def validate_outcome_payload(self) -> AnswerQuestionOutput:
        has_answer = self.answer is not None
        has_clarification = self.clarification is not None
        has_reason = self.reason is not None
        payload_count = sum((has_answer, has_clarification, has_reason))

        if self.outcome is AnswerQuestionOutcome.ANSWERED:
            if not has_answer or has_clarification or has_reason:
                raise ValueError("answered output requires exactly one answer payload")
            context_bundle_ids = {
                reference.identifier
                for reference in self.lineage
                if reference.kind == "context_bundle"
            }
            if not context_bundle_ids:
                raise ValueError("answered output requires context bundle lineage")
            citation_bundle_ids = {
                citation.context_bundle_id for citation in self.answer.citations
            }
            if not citation_bundle_ids <= context_bundle_ids:
                raise ValueError("answer citation is missing context bundle lineage")
            return self

        if self.outcome is AnswerQuestionOutcome.CLARIFICATION_REQUIRED:
            if not has_clarification or has_answer or not has_reason:
                raise ValueError(
                    "clarification_required output requires a clarification and reason"
                )
            if self.reason.code is not AnswerQuestionSafeReasonCode.AMBIGUOUS_QUESTION:
                raise ValueError("clarification_required reason must be ambiguous_question")
            return self

        if payload_count != 1 or not has_reason or has_answer or has_clarification:
            raise ValueError("non-answer output requires exactly one reason payload")

        expected_reason = {
            AnswerQuestionOutcome.ABSTAINED: {
                AnswerQuestionSafeReasonCode.NO_USABLE_EVIDENCE,
                AnswerQuestionSafeReasonCode.INSUFFICIENT_SUPPORT,
                AnswerQuestionSafeReasonCode.UNRESOLVED_CONFLICT,
                AnswerQuestionSafeReasonCode.INCOMPLETE_AUTHORIZED_COVERAGE,
                AnswerQuestionSafeReasonCode.UNMET_FRESHNESS,
                AnswerQuestionSafeReasonCode.EVIDENCE_INVALIDATED,
                AnswerQuestionSafeReasonCode.REJECTED_CANDIDATE,
            },
            AnswerQuestionOutcome.DENIED: {
                AnswerQuestionSafeReasonCode.AUTHORIZATION_DENIED,
            },
            AnswerQuestionOutcome.FAILED: {
                AnswerQuestionSafeReasonCode.MANDATORY_PROCESSING_FAILED,
            },
            AnswerQuestionOutcome.CANCELLED: {
                AnswerQuestionSafeReasonCode.CANCELLED,
            },
        }[self.outcome]
        if self.reason.code not in expected_reason:
            raise ValueError(f"{self.outcome.value} reason is incompatible with outcome")
        return self


_ModelT = TypeVar("_ModelT", bound=_AnswerQuestionModel)


def _parse_contract(
    model_type: type[_ModelT], value: str | Mapping[str, Any] | _ModelT
) -> _ModelT:
    try:
        if isinstance(value, str):
            return model_type.model_validate_json(value)
        return model_type.model_validate(value)
    except (ValueError, TypeError) as exc:
        raise AnswerQuestionContractError(
            f"{model_type.__name__} is not a compatible answer_question contract"
        ) from exc


def parse_answer_question_input(
    value: str | Mapping[str, Any] | AnswerQuestionInput,
) -> AnswerQuestionInput:
    """Parse input at a transport boundary and expose one typed contract error."""

    return _parse_contract(AnswerQuestionInput, value)


def parse_answer_question_output(
    value: str | Mapping[str, Any] | AnswerQuestionOutput,
) -> AnswerQuestionOutput:
    """Parse output at a transport boundary and fail closed on shape mismatch."""

    return _parse_contract(AnswerQuestionOutput, value)


def map_retrieval_outcome(
    retrieval_outcome: RetrievalOutcome,
    *,
    accepted: bool = False,
) -> AnswerQuestionOutcome:
    """Map a broker result explicitly to a terminal capability outcome.

    ``evidence_available`` only becomes ``answered`` when a later acceptance
    gate says so.  Without that independent decision it abstains; it is never
    treated as a final outcome itself.
    """

    if not isinstance(retrieval_outcome, RetrievalOutcome):
        raise AnswerQuestionContractError("retrieval outcome has an incompatible type")
    if retrieval_outcome is RetrievalOutcome.EVIDENCE_AVAILABLE:
        return AnswerQuestionOutcome.ANSWERED if accepted else AnswerQuestionOutcome.ABSTAINED
    if retrieval_outcome is RetrievalOutcome.INSUFFICIENT_EVIDENCE:
        return AnswerQuestionOutcome.ABSTAINED
    return AnswerQuestionOutcome.FAILED


__all__ = [
    "ANSWER_QUESTION_INPUT_SCHEMA",
    "ANSWER_QUESTION_OUTPUT_SCHEMA",
    "AnswerClaim",
    "AnswerContent",
    "AnswerQuestionContractError",
    "AnswerQuestionInput",
    "AnswerQuestionOutcome",
    "AnswerQuestionOutput",
    "AnswerQuestionSafeReason",
    "AnswerQuestionSafeReasonCode",
    "AnswerQuestionScope",
    "CitationReference",
    "ClarificationRequest",
    "LineageReference",
    "QuestionSubject",
    "RetrievalOutcome",
    "RunAttemptCorrelation",
    "map_retrieval_outcome",
    "parse_answer_question_input",
    "parse_answer_question_output",
]
