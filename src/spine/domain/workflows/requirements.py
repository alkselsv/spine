"""Versioned requirements extracted from a conversational workflow run."""

from __future__ import annotations

from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import Field, StrictFloat, StrictInt, StrictStr, field_validator, model_validator

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
