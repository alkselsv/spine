"""Versioned requirements extracted from a conversational workflow run."""

from __future__ import annotations

from enum import Enum
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import (
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from spine.domain.artifacts import (
    ArtifactRef,
    GoogleSheetCellLocator,
    Locator,
    TimeRangeLocator,
)
from spine.domain.common import ActorRef, DefinitionModel
from spine.domain.workflows.runs import AcceptedSourceRevisionRef


class RequirementState(str, Enum):
    CANDIDATE = "candidate"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    UNVERIFIED = "unverified"
    HUMAN_REVIEWED = "human_reviewed"
    ACCEPTED = "accepted"


class RequirementReviewState(str, Enum):
    NEEDS_REVIEW = "needs_review"
    REVIEWED = "reviewed"
    CHANGES_REQUESTED = "changes_requested"


class RequirementCategory(str, Enum):
    FUNCTIONAL = "functional"
    NON_FUNCTIONAL = "non_functional"
    DESIGN = "design"
    SCHEDULE = "schedule"
    CONSTRAINT = "constraint"
    AGREEMENT = "agreement"
    OPEN_QUESTION = "open_question"
    ASSUMPTION = "assumption"
    UNRESOLVED_INFORMATION = "unresolved_information"


class RequirementEvidenceStatus(str, Enum):
    REFERENCED = "referenced"
    UNVERIFIED = "unverified"
    UNSUPPORTED = "unsupported"


class RequirementEvidenceRelationship(str, Enum):
    """The bounded relationship between a requirement and one evidence item."""

    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    CONTEXT = "context"


class RequirementEvidenceValidationStatus(str, Enum):
    """Disclosure-safe result of validating one exact evidence reference."""

    UNVERIFIED = "unverified"
    VALID = "valid"
    DENIED = "denied"
    STALE = "stale"
    TOMBSTONED = "tombstoned"
    MISSING = "missing"
    LOCATOR_MISMATCH = "locator_mismatch"
    FABRICATED = "fabricated"
    DEGRADED = "degraded"


class ClarificationReason(str, Enum):
    MISSING_INFORMATION = "missing_information"
    AMBIGUOUS_INFORMATION = "ambiguous_information"


class ClarificationAnswer(DefinitionModel):
    """One manager answer admitted as canonical workflow data."""

    schema_version: Literal["clarification-answer:v1"] = "clarification-answer:v1"
    text: StrictStr = Field(min_length=1, max_length=32_768)
    answered_by: ActorRef
    answered_event_id: UUID
    answered_at: datetime

    @field_validator("text")
    @classmethod
    def reject_blank_answer(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("clarification answer must not be blank")
        return value

    @field_validator("answered_event_id")
    @classmethod
    def reject_zero_answer_event(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("clarification answer event identity must be non-zero")
        return value

    @field_validator("answered_at")
    @classmethod
    def require_aware_answer_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clarification answer timestamp must be timezone-aware")
        return value


class Clarification(DefinitionModel):
    """A durable question tied to one run, requirement and artifact version."""

    schema_version: Literal["clarification:v1"] = "clarification:v1"
    clarification_id: UUID
    run_id: UUID
    requirement_id: UUID
    originating_artifact_id: UUID
    originating_artifact_version: StrictInt = Field(ge=1)
    question: StrictStr = Field(min_length=1, max_length=8_192)
    reason: ClarificationReason
    asked_by: ActorRef
    asked_event_id: UUID
    asked_at: datetime
    correlation_id: UUID
    answer: ClarificationAnswer | None = None
    resulting_artifact_version: StrictInt | None = Field(default=None, ge=2)
    affected_validation_references: tuple[UUID, ...] = ()

    @field_validator(
        "clarification_id",
        "run_id",
        "requirement_id",
        "originating_artifact_id",
        "asked_event_id",
        "correlation_id",
    )
    @classmethod
    def reject_zero_clarification_identity(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("clarification identities must be non-zero")
        return value

    @field_validator("question")
    @classmethod
    def reject_blank_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("clarification question must not be blank")
        return value

    @field_validator("asked_at")
    @classmethod
    def require_aware_asked_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clarification question timestamp must be timezone-aware")
        return value

    @field_validator("affected_validation_references")
    @classmethod
    def validate_validation_lineage(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if any(item.int == 0 for item in value) or len(set(value)) != len(value):
            raise ValueError("clarification validation lineage must contain unique non-zero identities")
        return value

    @model_validator(mode="after")
    def validate_answer_lineage(self) -> "Clarification":
        if self.answer is None:
            if self.resulting_artifact_version is not None or self.affected_validation_references:
                raise ValueError("answered clarification lineage requires an answer")
            return self
        if self.resulting_artifact_version != self.originating_artifact_version + 1:
            raise ValueError("answered clarification must identify its immediate successor")
        if self.answer.answered_at < self.asked_at:
            raise ValueError("clarification answer cannot predate the question")
        return self


class RequirementEvidenceValidation(DefinitionModel):
    """Content-free validation lineage retained with a candidate artifact."""

    schema_version: Literal["requirement-evidence-validation:v1"] = (
        "requirement-evidence-validation:v1"
    )
    validation_reference: UUID
    status: RequirementEvidenceValidationStatus = RequirementEvidenceValidationStatus.UNVERIFIED
    authorization_decision_reference: UUID | None = None
    validated_at: datetime | None = None

    @field_validator("validation_reference", "authorization_decision_reference")
    @classmethod
    def reject_zero_validation_ids(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("evidence validation identities must be non-zero")
        return value

    @field_validator("validated_at")
    @classmethod
    def require_aware_validation_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("evidence validation timestamps must be timezone-aware")
        return value


class RequirementEvidenceRef(DefinitionModel):
    """An exact source revision and parsed or time-based evidence segment."""

    source_ref: AcceptedSourceRevisionRef
    parsed_artifact_ref: ArtifactRef | None = None
    transcript_locator: TimeRangeLocator | None = None
    segment_id: UUID
    segment_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    locator_contract_version: StrictStr = Field(
        default="locator:v1", pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
    )
    locator: Locator | GoogleSheetCellLocator | None = None
    relationship: RequirementEvidenceRelationship = RequirementEvidenceRelationship.SUPPORTS
    validation: RequirementEvidenceValidation | None = None

    @field_validator("segment_id")
    @classmethod
    def reject_zero_segment_id(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("evidence segment identity must be non-zero")
        return value

    @model_validator(mode="after")
    def validate_exact_parsed_reference(self) -> "RequirementEvidenceRef":
        if (self.parsed_artifact_ref is None) == (self.transcript_locator is None):
            raise ValueError("evidence must identify exactly one parsed or transcript locator")
        if self.parsed_artifact_ref is not None:
            if self.parsed_artifact_ref.id.int == 0:
                raise ValueError("parsed artifact identity must be non-zero")
            if (
                self.parsed_artifact_ref.schema_name != "parsed-document"
                or self.parsed_artifact_ref.schema_version != "parsed-document:v1"
            ):
                raise ValueError("evidence must reference a versioned parsed document")
            if self.locator is None:
                raise ValueError("parsed evidence requires its stable locator")
            if (
                isinstance(self.locator, GoogleSheetCellLocator)
                and self.locator.source_revision_id != self.source_ref.source_revision_id
            ):
                raise ValueError("Google Sheets locator must reference the same source revision")
        elif self.transcript_locator is not None:
            if self.locator is not None:
                raise ValueError("transcript evidence uses its time-range locator")
            if (
                self.transcript_locator.source_object_id != self.source_ref.source_object_id
                or self.transcript_locator.source_revision_id != self.source_ref.source_revision_id
                or self.transcript_locator.segment_ids != (self.segment_id,)
            ):
                raise ValueError("transcript locator must identify the exact source segment")
        if (
            self.validation is not None
            and self.validation.status is RequirementEvidenceValidationStatus.VALID
            and self.validation.authorization_decision_reference is None
        ):
            raise ValueError("valid evidence requires an authorization decision reference")
        return self


class Requirement(DefinitionModel):
    """One traceable, unaccepted statement proposed by requirements extraction."""

    schema_version: Literal["requirement:v1"] = "requirement:v1"
    requirement_id: UUID
    category: RequirementCategory
    statement: StrictStr = Field(min_length=1, max_length=8_192)
    constraints: tuple[StrictStr, ...] = ()
    uncertainty: StrictStr | None = Field(default=None, max_length=2_000)
    ambiguous_markers: tuple[StrictStr, ...] = ()
    missing_markers: tuple[StrictStr, ...] = ()
    state: RequirementState = RequirementState.CANDIDATE
    review_state: RequirementReviewState = RequirementReviewState.NEEDS_REVIEW
    confidence: StrictFloat | None = Field(default=None, ge=0, le=1)
    source_refs: tuple[AcceptedSourceRevisionRef, ...] = ()
    evidence_refs: tuple[RequirementEvidenceRef, ...] = ()
    evidence_status: RequirementEvidenceStatus = RequirementEvidenceStatus.UNVERIFIED
    upstream_artifact_ids: tuple[UUID, ...] = ()

    @field_validator("requirement_id", "upstream_artifact_ids")
    @classmethod
    def reject_zero_ids(cls, value: UUID | tuple[UUID, ...]) -> UUID | tuple[UUID, ...]:
        values = value if isinstance(value, tuple) else (value,)
        if any(identifier.int == 0 for identifier in values):
            raise ValueError("requirement identities must be non-zero")
        if len(set(values)) != len(values):
            raise ValueError("upstream artifact identities must be unique")
        return value

    @model_validator(mode="after")
    def validate_evidence_lineage(self) -> "Requirement":
        source_refs = {
            (reference.source_object_id, reference.source_revision_id, reference.provenance_id)
            for reference in self.source_refs
        }
        if len(source_refs) != len(self.source_refs):
            raise ValueError("requirement source references must be unique")
        evidence_keys = tuple(
            (
                reference.parsed_artifact_ref.id
                if reference.parsed_artifact_ref is not None
                else reference.transcript_locator.transcript_artifact_id,
                reference.segment_id,
            )
            for reference in self.evidence_refs
        )
        if len(set(evidence_keys)) != len(evidence_keys):
            raise ValueError("requirement evidence references must be unique")
        if any(
            (
                reference.source_ref.source_object_id,
                reference.source_ref.source_revision_id,
                reference.source_ref.provenance_id,
            ) not in source_refs
            for reference in self.evidence_refs
        ):
            raise ValueError("requirement evidence must belong to a referenced source revision")
        if self.evidence_status is RequirementEvidenceStatus.REFERENCED and not self.evidence_refs:
            raise ValueError("referenced evidence status requires exact evidence references")
        return self


class RequirementsArtifact(DefinitionModel):
    """Immutable, explicitly versioned requirements candidate for one run."""

    artifact_id: UUID
    artifact_version: StrictInt = Field(ge=1)
    schema_version: str = Field(default="requirements:v1", pattern=r"^requirements:v[1-9][0-9]*$")
    production_identity: StrictStr = Field(min_length=1, max_length=255)
    production_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_id: UUID
    environment_id: UUID
    work_item_id: UUID
    run_id: UUID
    requirements: tuple[Requirement, ...]
    clarifications: tuple[Clarification, ...] = ()
    state: RequirementState = RequirementState.CANDIDATE
    upstream_artifact_ids: tuple[UUID, ...] = ()
    predecessor_version: StrictInt | None = Field(default=None, ge=1)

    @field_validator("artifact_id", "workspace_id", "environment_id", "work_item_id", "run_id")
    @classmethod
    def reject_zero_identity(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("requirements artifact identities must be non-zero")
        return value

    @field_validator("upstream_artifact_ids")
    @classmethod
    def validate_lineage(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if any(identifier.int == 0 for identifier in value) or len(set(value)) != len(value):
            raise ValueError("requirements artifact lineage must contain unique non-zero identities")
        return value

    @model_validator(mode="after")
    def validate_successor(self) -> "RequirementsArtifact":
        if self.predecessor_version is None and self.artifact_version != 1:
            raise ValueError("successor versions must identify their predecessor")
        if self.predecessor_version is not None and self.predecessor_version != self.artifact_version - 1:
            raise ValueError("requirements versions must be monotonic")
        if self.artifact_id in self.upstream_artifact_ids:
            raise ValueError("requirements artifact cannot reference itself as upstream")
        requirement_ids = tuple(item.requirement_id for item in self.requirements)
        if len(set(requirement_ids)) != len(requirement_ids):
            raise ValueError("requirements in an artifact must have stable unique identities")
        clarification_ids = tuple(item.clarification_id for item in self.clarifications)
        if len(set(clarification_ids)) != len(clarification_ids):
            raise ValueError("clarifications in an artifact must have stable unique identities")
        if any(
            item.run_id != self.run_id
            or item.originating_artifact_id != self.artifact_id
            or item.requirement_id not in requirement_ids
            or item.originating_artifact_version > self.artifact_version
            or (
                item.answer is not None
                and (
                    item.resulting_artifact_version is None
                    or item.resulting_artifact_version > self.artifact_version
                )
            )
            for item in self.clarifications
        ):
            raise ValueError("clarification lineage must belong to the exact requirements artifact")
        return self
