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
from spine.domain.context_bundles import (
    ContextBundleEvidenceReference,
    ContextBundleEvidenceStatus,
    ContextBundleLineageRef,
)


ANSWER_QUESTION_INPUT_SCHEMA = "answer-question.input:v1"
ANSWER_QUESTION_OUTPUT_SCHEMA = "answer-question.output:v1"
ANSWER_CONTENT_SCHEMA = "answer-content:v1"
ANSWER_CLAIM_SCHEMA = "answer-claim:v1"
CITATION_SCHEMA = "citation:v1"
CLAIM_EVIDENCE_RELATIONSHIP_SCHEMA = "claim-evidence-relationship:v1"
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


class AnswerClaimKind(str, Enum):
    """Whether a claim asserts evidence or a reproducible derivation from it."""

    FACTUAL = "factual"
    DERIVED = "derived"


class ClaimEvidenceRelationshipKind(str, Enum):
    """Closed composition semantics for claim-level attribution."""

    INDEPENDENT = "independent"
    CORROBORATING = "corroborating"
    JOINT = "joint"
    DERIVATION_INPUT = "derivation_input"
    CONTRADICTION = "contradiction"


class AnswerClaimLocation(_AnswerQuestionModel):
    """A stable span in one immutable accepted answer representation.

    The containing answer identity is explicit.  Offsets locate the claim for
    inspection and rendering, but neither offsets nor rendered markers define
    the claim's authoritative identity.
    """

    schema_version: Literal["answer-claim-location:v1"] = "answer-claim-location:v1"
    answer_id: UUID
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)

    @field_validator("answer_id")
    @classmethod
    def validate_answer_id(cls, value: UUID) -> UUID:
        return _require_non_zero(value, "answer_id")

    @model_validator(mode="after")
    def validate_offsets(self) -> AnswerClaimLocation:
        if self.end_offset <= self.start_offset:
            raise ValueError("claim location end must follow its start")
        return self


class Citation(_AnswerQuestionModel):
    """One exact persisted ContextBundle evidence identity.

    Display titles, excerpts, URLs, retrieval scores and inline markers are
    deliberately absent.  They are presentation or diagnostic data and cannot
    substitute for the persisted evidence lineage below.
    """

    schema_version: Literal[CITATION_SCHEMA] = CITATION_SCHEMA
    citation_id: UUID
    answer_id: UUID
    context_bundle: ContextBundleLineageRef
    evidence_reference: ContextBundleEvidenceReference

    @field_validator(
        "citation_id",
        "answer_id",
    )
    @classmethod
    def validate_identifier(cls, value: UUID | None, info: Any) -> UUID | None:
        if value is None:
            return None
        return _require_non_zero(value, info.field_name)

    @model_validator(mode="after")
    def validate_evidence_scope(self) -> Citation:
        if (
            self.evidence_reference.workspace_id != self.context_bundle.workspace_id
            or self.evidence_reference.environment_id
            != self.context_bundle.environment_id
        ):
            raise ValueError("citation evidence has a mixed workspace or environment")
        if (
            self.evidence_reference.revision_digest is None
            or self.evidence_reference.locator_contract_version is None
        ):
            raise ValueError("citation evidence requires complete revision and locator lineage")
        if self.evidence_reference.status is not ContextBundleEvidenceStatus.VALID:
            raise ValueError("citation requires valid persisted evidence")
        return self

    @property
    def context_bundle_id(self) -> UUID:
        """Compatibility accessor for callers needing only the bundle UUID."""

        return self.context_bundle.bundle.bundle_id

    @property
    def workspace_id(self) -> UUID:
        return self.context_bundle.workspace_id

    @property
    def environment_id(self) -> UUID:
        return self.context_bundle.environment_id

    @property
    def run_id(self) -> UUID:
        return self.context_bundle.run_id

    @property
    def attempt_id(self) -> UUID:
        return self.context_bundle.attempt_id

    @property
    def original_sha256(self) -> str:
        """Compatibility name for the immutable source-revision digest."""

        if self.evidence_reference.revision_digest is None:
            raise ValueError("citation evidence does not retain a revision digest")
        return self.evidence_reference.revision_digest

    @property
    def evidence_id(self) -> str:
        return self.evidence_reference.evidence_id

    @property
    def source_object_id(self) -> UUID:
        return self.evidence_reference.source_object_id

    @property
    def source_revision_id(self) -> UUID:
        return self.evidence_reference.source_revision_id

    @property
    def revision_digest(self) -> str | None:
        return self.evidence_reference.revision_digest

    @property
    def processing_receipt_id(self) -> UUID | None:
        return self.evidence_reference.processing_receipt_id

    @property
    def locator_contract_version(self) -> str | None:
        return self.evidence_reference.locator_contract_version

    @property
    def evidence_digest(self) -> str:
        return self.evidence_reference.evidence_digest


# The previous capability contract exported this name.  Keeping the alias lets
# consumers migrate without creating a second, weaker citation representation.
CitationReference = Citation


class AnswerClaim(_AnswerQuestionModel):
    """One independently identified semantic claim in an accepted answer."""

    schema_version: Literal[ANSWER_CLAIM_SCHEMA] = ANSWER_CLAIM_SCHEMA
    claim_id: UUID
    workspace_id: UUID
    environment_id: UUID
    run_id: UUID
    attempt_id: UUID
    kind: AnswerClaimKind = AnswerClaimKind.FACTUAL
    text: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)
    location: AnswerClaimLocation

    @field_validator(
        "claim_id", "workspace_id", "environment_id", "run_id", "attempt_id"
    )
    @classmethod
    def validate_identifier(cls, value: UUID, info: Any) -> UUID:
        return _require_non_zero(value, info.field_name)

    @field_validator("text")
    @classmethod
    def validate_claim_text(cls, value: str) -> str:
        return _require_text(value, "claim text")


class ClaimEvidenceRelationship(_AnswerQuestionModel):
    """Canonical claim attribution, independent of rendered citation markers."""

    schema_version: Literal[CLAIM_EVIDENCE_RELATIONSHIP_SCHEMA] = (
        CLAIM_EVIDENCE_RELATIONSHIP_SCHEMA
    )
    relationship_id: UUID
    answer_id: UUID
    workspace_id: UUID
    environment_id: UUID
    run_id: UUID
    attempt_id: UUID
    claim_id: UUID
    kind: ClaimEvidenceRelationshipKind
    citation_ids: tuple[UUID, ...] = ()
    related_claim_id: UUID | None = None

    @field_validator(
        "relationship_id",
        "answer_id",
        "workspace_id",
        "environment_id",
        "run_id",
        "attempt_id",
        "claim_id",
        "related_claim_id",
    )
    @classmethod
    def validate_identifier(cls, value: UUID | None, info: Any) -> UUID | None:
        if value is None:
            return None
        return _require_non_zero(value, info.field_name)

    @field_validator("citation_ids")
    @classmethod
    def validate_citation_ids(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if any(identifier.int == 0 for identifier in value):
            raise ValueError("relationship citation identifiers must be non-zero")
        if len(set(value)) != len(value):
            raise ValueError("relationship citation identifiers must be unique")
        return value

    @model_validator(mode="after")
    def validate_composition(self) -> ClaimEvidenceRelationship:
        if self.kind is ClaimEvidenceRelationshipKind.CONTRADICTION:
            if self.related_claim_id is None or self.citation_ids:
                raise ValueError(
                    "contradiction relationship requires one related claim and no citations"
                )
            if self.related_claim_id == self.claim_id:
                raise ValueError("contradiction relationship cannot target its own claim")
            return self
        if self.related_claim_id is not None:
            raise ValueError("evidence relationship cannot include a related claim")
        if self.kind in {
            ClaimEvidenceRelationshipKind.INDEPENDENT,
            ClaimEvidenceRelationshipKind.CORROBORATING,
        } and len(self.citation_ids) != 1:
            raise ValueError(f"{self.kind.value} relationship requires one citation")
        if self.kind is ClaimEvidenceRelationshipKind.JOINT and len(self.citation_ids) < 2:
            raise ValueError("joint relationship requires at least two citations")
        if (
            self.kind is ClaimEvidenceRelationshipKind.DERIVATION_INPUT
            and not self.citation_ids
        ):
            raise ValueError("derivation input relationship requires evidence")
        return self


class AnswerContent(_AnswerQuestionModel):
    """One accepted answer and its complete canonical evidence graph."""

    schema_version: Literal[ANSWER_CONTENT_SCHEMA] = ANSWER_CONTENT_SCHEMA
    answer_id: UUID
    workspace_id: UUID
    environment_id: UUID
    run_id: UUID
    attempt_id: UUID
    text: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)
    claims: tuple[AnswerClaim, ...] = Field(min_length=1)
    citations: tuple[Citation, ...] = Field(min_length=1)
    relationships: tuple[ClaimEvidenceRelationship, ...] = Field(min_length=1)

    @field_validator("answer_id", "workspace_id", "environment_id", "run_id", "attempt_id")
    @classmethod
    def validate_identifier(cls, value: UUID, info: Any) -> UUID:
        return _require_non_zero(value, info.field_name)

    @field_validator("text")
    @classmethod
    def validate_answer_text(cls, value: str) -> str:
        return _require_text(value, "answer content")

    @model_validator(mode="after")
    def validate_evidence_graph(self) -> AnswerContent:
        self._validate_unique_identities()
        self._validate_scope_and_locations()
        self._validate_bundle_and_evidence_identities()
        self._validate_relationships()
        return self

    def _validate_unique_identities(self) -> None:
        for label, identifiers in (
            ("claim", [item.claim_id for item in self.claims]),
            ("citation", [item.citation_id for item in self.citations]),
            ("relationship", [item.relationship_id for item in self.relationships]),
        ):
            if len(set(identifiers)) != len(identifiers):
                raise ValueError(f"answer {label} identifiers must be unique")

    def _validate_scope_and_locations(self) -> None:
        scoped_items = (*self.claims, *self.citations, *self.relationships)
        if any(
            item.workspace_id != self.workspace_id
            or item.environment_id != self.environment_id
            for item in scoped_items
        ):
            raise ValueError("answer evidence has a mixed workspace or environment")
        if any(
            item.run_id != self.run_id or item.attempt_id != self.attempt_id
            for item in scoped_items
        ):
            raise ValueError("answer evidence has a mixed run or attempt")
        for claim in self.claims:
            location = claim.location
            if location.answer_id != self.answer_id:
                raise ValueError("claim location belongs to a different accepted answer")
            if location.end_offset > len(self.text):
                raise ValueError("claim location is outside the accepted answer")
            if self.text[location.start_offset : location.end_offset] != claim.text:
                raise ValueError("claim location does not identify its text in the accepted answer")
        if any(
            item.answer_id != self.answer_id
            for item in (*self.citations, *self.relationships)
        ):
            raise ValueError("answer evidence belongs to a different accepted answer")

    def _validate_bundle_and_evidence_identities(self) -> None:
        bundle_identities = {
            (
                item.context_bundle.bundle.bundle_id,
                item.context_bundle.bundle.bundle_version,
                item.context_bundle.bundle.bundle_digest,
            )
            for item in self.citations
        }
        if len(bundle_identities) != 1:
            raise ValueError("answer citations must use one exact context bundle")
        evidence_identities = {
            (
                item.context_bundle.bundle.bundle_id,
                item.context_bundle.bundle.bundle_version,
                item.context_bundle.bundle.bundle_digest,
                item.evidence_reference.evidence_id,
            )
            for item in self.citations
        }
        if len(evidence_identities) != len(self.citations):
            raise ValueError("duplicate citation evidence identity")

    def _validate_relationships(self) -> None:
        claim_by_id = {item.claim_id: item for item in self.claims}
        citation_ids = {item.citation_id for item in self.citations}
        signatures: set[tuple[object, ...]] = set()
        cited_ids: set[UUID] = set()
        relationships_by_claim: dict[UUID, list[ClaimEvidenceRelationship]] = {
            claim_id: [] for claim_id in claim_by_id
        }
        citation_roles_by_claim: dict[UUID, set[UUID]] = {
            claim_id: set() for claim_id in claim_by_id
        }
        for relationship in self.relationships:
            if relationship.claim_id not in claim_by_id:
                raise ValueError("relationship references an unknown claim")
            if not set(relationship.citation_ids) <= citation_ids:
                raise ValueError("relationship references an unknown citation")
            if (
                relationship.related_claim_id is not None
                and relationship.related_claim_id not in claim_by_id
            ):
                raise ValueError("contradiction references an unknown claim")
            if relationship.kind is ClaimEvidenceRelationshipKind.CONTRADICTION:
                claim_pair = tuple(
                    sorted(
                        (relationship.claim_id, relationship.related_claim_id),
                        key=lambda identifier: identifier.int,
                    )
                )
                signature = (relationship.kind, claim_pair)
            else:
                signature = (
                    relationship.claim_id,
                    relationship.kind,
                    relationship.citation_ids,
                )
            if signature in signatures:
                raise ValueError("duplicate claim evidence relationship")
            signatures.add(signature)
            repeated_roles = citation_roles_by_claim[relationship.claim_id] & set(
                relationship.citation_ids
            )
            if repeated_roles:
                raise ValueError(
                    "one citation cannot have multiple evidence roles for the same claim"
                )
            citation_roles_by_claim[relationship.claim_id].update(
                relationship.citation_ids
            )
            cited_ids.update(relationship.citation_ids)
            relationships_by_claim[relationship.claim_id].append(relationship)

        if cited_ids != citation_ids:
            raise ValueError("every citation must participate in claim attribution")
        for claim_id, relationships in relationships_by_claim.items():
            claim = claim_by_id[claim_id]
            evidence_kinds = {
                item.kind
                for item in relationships
                if item.kind is not ClaimEvidenceRelationshipKind.CONTRADICTION
            }
            if claim.kind is AnswerClaimKind.DERIVED:
                if evidence_kinds != {ClaimEvidenceRelationshipKind.DERIVATION_INPUT}:
                    raise ValueError("derived claim requires only derivation input evidence")
                continue
            if ClaimEvidenceRelationshipKind.DERIVATION_INPUT in evidence_kinds:
                raise ValueError("factual claim cannot use derivation input evidence")
            primary = evidence_kinds & {
                ClaimEvidenceRelationshipKind.INDEPENDENT,
                ClaimEvidenceRelationshipKind.JOINT,
            }
            if not primary:
                raise ValueError("factual claim requires independent or joint evidence")


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
            if (
                self.answer.workspace_id != self.scope.workspace_id
                or self.answer.environment_id != self.scope.environment_id
            ):
                raise ValueError("answer evidence scope does not match capability scope")
            if (
                self.answer.run_id != self.correlation.run_id
                or self.answer.attempt_id != self.correlation.attempt_id
            ):
                raise ValueError("answer evidence lineage does not match run attempt")
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
    "ANSWER_CLAIM_SCHEMA",
    "ANSWER_CONTENT_SCHEMA",
    "ANSWER_QUESTION_INPUT_SCHEMA",
    "ANSWER_QUESTION_OUTPUT_SCHEMA",
    "CITATION_SCHEMA",
    "CLAIM_EVIDENCE_RELATIONSHIP_SCHEMA",
    "AnswerClaim",
    "AnswerClaimKind",
    "AnswerClaimLocation",
    "AnswerContent",
    "AnswerQuestionContractError",
    "AnswerQuestionInput",
    "AnswerQuestionOutcome",
    "AnswerQuestionOutput",
    "AnswerQuestionSafeReason",
    "AnswerQuestionSafeReasonCode",
    "AnswerQuestionScope",
    "Citation",
    "CitationReference",
    "ClaimEvidenceRelationship",
    "ClaimEvidenceRelationshipKind",
    "ClarificationRequest",
    "LineageReference",
    "QuestionSubject",
    "RetrievalOutcome",
    "RunAttemptCorrelation",
    "map_retrieval_outcome",
    "parse_answer_question_input",
    "parse_answer_question_output",
]
