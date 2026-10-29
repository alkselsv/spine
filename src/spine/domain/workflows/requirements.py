"""Versioned requirements extracted from a conversational workflow run."""

from __future__ import annotations

from enum import Enum
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

from spine.domain.artifacts import ArtifactRef, GoogleSheetCellLocator, Locator
from spine.domain.common import DefinitionModel
from spine.domain.workflows.runs import AcceptedSourceRevisionRef


class RequirementState(str, Enum):
    CANDIDATE = "candidate"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    UNVERIFIED = "unverified"


class RequirementReviewState(str, Enum):
    NEEDS_REVIEW = "needs_review"
    REVIEWED = "reviewed"
    CHANGES_REQUESTED = "changes_requested"


class RequirementCategory(str, Enum):
    FUNCTIONAL = "functional"
    NON_FUNCTIONAL = "non_functional"
    CONSTRAINT = "constraint"
    ASSUMPTION = "assumption"
    UNRESOLVED_INFORMATION = "unresolved_information"


class RequirementEvidenceStatus(str, Enum):
    REFERENCED = "referenced"
    UNVERIFIED = "unverified"
    UNSUPPORTED = "unsupported"


class RequirementEvidenceRef(DefinitionModel):
    """An exact source revision and parsed evidence segment without source text."""

    source_ref: AcceptedSourceRevisionRef
    parsed_artifact_ref: ArtifactRef
    segment_id: UUID
    segment_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    locator: Locator | GoogleSheetCellLocator

    @field_validator("segment_id")
    @classmethod
    def reject_zero_segment_id(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("evidence segment identity must be non-zero")
        return value

    @model_validator(mode="after")
    def validate_exact_parsed_reference(self) -> "RequirementEvidenceRef":
        if self.parsed_artifact_ref.id.int == 0:
            raise ValueError("parsed artifact identity must be non-zero")
        if (
            self.parsed_artifact_ref.schema_name != "parsed-document"
            or self.parsed_artifact_ref.schema_version != "parsed-document:v1"
        ):
            raise ValueError("evidence must reference a versioned parsed document")
        if (
            isinstance(self.locator, GoogleSheetCellLocator)
            and self.locator.source_revision_id != self.source_ref.source_revision_id
        ):
            raise ValueError("Google Sheets locator must reference the same source revision")
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
            (reference.parsed_artifact_ref.id, reference.segment_id)
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
        return self
