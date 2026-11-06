"""One fail-closed authority seam for every current source-bearing use.

The seam deliberately carries identities and digests only.  Its implementation
owns current-head, canonical-acceptance, processing-disposition, lifecycle,
retention and disclosure-hold reads.  Callers cannot assemble a positive result
from separate booleans or silently omit one contributor from a mixed artifact.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Protocol
from uuid import UUID

from pydantic import Field, model_validator

from spine.domain.common import DefinitionModel
from spine.domain.sources.retention import RetentionArtifactClass


class SourceUsageKind(str, Enum):
    PROJECTION_REQUEST = "projection_request"
    PROJECTION_BUILD = "projection_build"
    PROJECTION_ACTIVATION = "projection_activation"
    RETRIEVAL = "retrieval"
    CITATION = "citation"
    CACHE_REUSE = "cache_reuse"
    FINAL_DISCLOSURE = "final_disclosure"


class SourceUsageReason(str, Enum):
    ALLOWED = "allowed"
    AUTHORITY_UNAVAILABLE = "authority_unavailable"
    PROCESSING_INELIGIBLE = "processing_ineligible"
    LIFECYCLE_INELIGIBLE = "lifecycle_ineligible"
    RETENTION_INELIGIBLE = "retention_ineligible"
    DISCLOSURE_HELD = "disclosure_held"


class SourceUsageArtifact(DefinitionModel):
    """Exact immutable artifact whose complete source lineage is checked."""

    artifact_class: RetentionArtifactClass
    artifact_id: UUID
    artifact_version: int = Field(ge=0)
    artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_identity(self) -> "SourceUsageArtifact":
        if self.artifact_id.int == 0:
            raise ValueError("source usage artifact identity must be non-zero")
        return self


class SourceUsageDependency(DefinitionModel):
    """One exact contributor; dependency identity is hold/citation lineage."""

    source_object_id: UUID
    source_revision_id: UUID
    dependency_id: UUID | None = None
    artifact_member_id: UUID | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> "SourceUsageDependency":
        values = (
            self.source_object_id,
            self.source_revision_id,
            self.dependency_id,
            self.artifact_member_id,
        )
        if any(value is not None and value.int == 0 for value in values):
            raise ValueError("source usage dependency identity must be non-zero")
        return self


class SourceUsageRequest(DefinitionModel):
    """Content-free complete-lineage request for one guarded operation."""

    workspace_id: UUID
    environment_id: UUID
    usage: SourceUsageKind
    artifact: SourceUsageArtifact
    dependencies: tuple[SourceUsageDependency, ...] = Field(min_length=1)
    evaluated_at: datetime

    @model_validator(mode="after")
    def validate_request(self) -> "SourceUsageRequest":
        if self.workspace_id.int == 0 or self.environment_id.int == 0:
            raise ValueError("source usage scope must be non-zero")
        identities = tuple(
            (item.source_object_id, item.source_revision_id)
            for item in self.dependencies
        )
        if len(set(identities)) != len(identities):
            raise ValueError("source usage dependencies must be unique")
        value = self.evaluated_at
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("source usage evaluation time must be timezone-aware")
        return self


class CurrentSourceUsageDecision(DefinitionModel):
    """Disclosure-safe decision; it never carries titles, text or locators."""

    allowed: bool
    reason: SourceUsageReason
    required_dependency_count: int = Field(ge=1)
    checked_dependencies: tuple[SourceUsageDependency, ...] = ()

    @model_validator(mode="after")
    def validate_decision(self) -> "CurrentSourceUsageDecision":
        if self.allowed != (self.reason is SourceUsageReason.ALLOWED):
            raise ValueError("source usage reason must match the decision")
        if (
            self.allowed
            and len(self.checked_dependencies) != self.required_dependency_count
        ):
            raise ValueError("allowed source usage requires complete dependency checks")
        identities = tuple(
            (item.source_object_id, item.source_revision_id)
            for item in self.checked_dependencies
        )
        if len(set(identities)) != len(identities):
            raise ValueError("checked source usage dependencies must be unique")
        return self


class CurrentSourceUsageAuthority(Protocol):
    """Resolve all canonical deny constraints through one trusted boundary."""

    async def evaluate(
        self,
        request: SourceUsageRequest,
    ) -> CurrentSourceUsageDecision: ...


class CurrentSourceUsageGuard:
    """Validate authority output and fail closed on absence or malformed scope."""

    def __init__(self, authority: CurrentSourceUsageAuthority | None) -> None:
        self._authority = authority

    async def evaluate(
        self,
        request: SourceUsageRequest,
    ) -> CurrentSourceUsageDecision:
        denied = CurrentSourceUsageDecision(
            allowed=False,
            reason=SourceUsageReason.AUTHORITY_UNAVAILABLE,
            required_dependency_count=len(request.dependencies),
        )
        if self._authority is None:
            return denied
        try:
            decision = await self._authority.evaluate(request)
        except Exception:
            return denied
        if not isinstance(decision, CurrentSourceUsageDecision):
            return denied
        requested = set(request.dependencies)
        checked = set(decision.checked_dependencies)
        if (
            decision.required_dependency_count != len(request.dependencies)
            or not checked.issubset(requested)
            or (decision.allowed and checked != requested)
        ):
            return denied
        return decision


__all__ = [
    "CurrentSourceUsageAuthority",
    "CurrentSourceUsageDecision",
    "CurrentSourceUsageGuard",
    "SourceUsageArtifact",
    "SourceUsageDependency",
    "SourceUsageKind",
    "SourceUsageReason",
    "SourceUsageRequest",
]
