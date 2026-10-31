"""Resolve current retention eligibility from immutable source lineage."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field, model_validator

from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedPersistenceContext,
)
from spine.domain.common import DefinitionModel
from spine.domain.sources.retention import (
    RetentionApplicability,
    RetentionArtifactClass,
    RetentionTargetKind,
)


class RetentionEligibilityStatus(str, Enum):
    ELIGIBLE = "eligible"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    CONFLICTING = "conflicting"
    NOT_YET_EFFECTIVE = "not_yet_effective"
    EXPIRED = "expired"
    LINEAGE_UNAVAILABLE = "lineage_unavailable"
    AUTHORITY_UNAVAILABLE = "authority_unavailable"


class RetentionSourceLineage(DefinitionModel):
    source_object_id: UUID
    source_revision_id: UUID
    source_class: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")

    @model_validator(mode="after")
    def validate_ids(self) -> "RetentionSourceLineage":
        if self.source_object_id.int == 0 or self.source_revision_id.int == 0:
            raise ValueError("retention lineage identifiers must be non-zero")
        return self


@dataclass(frozen=True, slots=True)
class RetentionEligibilityQuery:
    workspace_id: UUID
    environment_id: UUID
    artifact_class: RetentionArtifactClass
    source_lineage: tuple[RetentionSourceLineage, ...]

    def __post_init__(self) -> None:
        if self.workspace_id.int == 0 or self.environment_id.int == 0:
            raise ValueError("retention query scope must be non-zero")
        if not self.source_lineage:
            raise ValueError("retention query requires exact source lineage")
        identities = tuple(
            (item.source_object_id, item.source_revision_id)
            for item in self.source_lineage
        )
        if len(set(identities)) != len(identities):
            raise ValueError("retention source lineage must be unique")


class RetentionSourceDecision(DefinitionModel):
    source_object_id: UUID
    source_revision_id: UUID
    status: RetentionEligibilityStatus
    applicability_id: UUID | None = None
    applicability_generation: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_outcome(self) -> "RetentionSourceDecision":
        eligible = self.status is RetentionEligibilityStatus.ELIGIBLE
        if eligible != (self.applicability_id is not None):
            raise ValueError("only eligible retention decisions carry applicability")
        if eligible != (self.applicability_generation is not None):
            raise ValueError("only eligible retention decisions carry a generation")
        return self


class RetentionEligibility(DefinitionModel):
    schema_version: Literal["retention-eligibility:v1"] = "retention-eligibility:v1"
    workspace_id: UUID
    environment_id: UUID
    artifact_class: RetentionArtifactClass
    status: RetentionEligibilityStatus
    source_decisions: tuple[RetentionSourceDecision, ...]

    @property
    def eligible(self) -> bool:
        return self.status is RetentionEligibilityStatus.ELIGIBLE

    @model_validator(mode="after")
    def validate_outcome(self) -> "RetentionEligibility":
        if not self.source_decisions:
            raise ValueError("retention eligibility requires source decisions")
        all_eligible = all(
            item.status is RetentionEligibilityStatus.ELIGIBLE
            for item in self.source_decisions
        )
        if all_eligible != (self.status is RetentionEligibilityStatus.ELIGIBLE):
            raise ValueError("aggregate retention status must match source decisions")
        return self


class CurrentRetentionApplicabilityCandidates(Protocol):
    """Return only current-pointer bindings matching exact revision lineage."""

    async def list_current_applicabilities(
        self,
        source: RetentionSourceLineage,
        context: TrustedPersistenceContext,
    ) -> tuple[RetentionApplicability, ...]: ...


class RetentionApplicabilityError(RuntimeError):
    """Disclosure-safe failure for unavailable retention authority."""

    def __init__(self) -> None:
        super().__init__("Retention applicability authority is unavailable.")


class RetentionApplicabilityConflictError(RetentionApplicabilityError):
    """Expected-current applicability no longer matches."""


class RetentionApplicabilityAuthority(Protocol):
    """Verify approved policy and approving authority inside the writer UoW."""

    async def verify(
        self,
        applicability: RetentionApplicability,
        context: TrustedPersistenceContext,
        transaction: object,
    ) -> None: ...


class RetentionApplicabilityEffectsWriter(Protocol):
    """Persist commit-consistent safe audit and outbox records."""

    async def persist(
        self,
        applicability: RetentionApplicability,
        context: TrustedPersistenceContext,
        transaction: object,
    ) -> tuple[UUID, UUID]: ...


class RetentionApplicabilityRepository(
    CurrentRetentionApplicabilityCandidates, Protocol
):
    async def record_applicability(
        self,
        applicability: RetentionApplicability,
        context: TrustedPersistenceContext,
    ) -> RetentionApplicability: ...


class RetentionEligibilityReader:
    """Fail closed when an exact source has no single effective binding."""

    def __init__(
        self,
        *,
        candidates: CurrentRetentionApplicabilityCandidates,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._candidates = candidates
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def resolve(
        self,
        query: RetentionEligibilityQuery,
        context: TrustedPersistenceContext,
    ) -> RetentionEligibility:
        if not _context_matches(query, context):
            return _uniform_result(query, RetentionEligibilityStatus.AUTHORITY_UNAVAILABLE)
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            return _uniform_result(query, RetentionEligibilityStatus.AUTHORITY_UNAVAILABLE)
        decisions: list[RetentionSourceDecision] = []
        try:
            for source in query.source_lineage:
                bindings = await self._candidates.list_current_applicabilities(
                    source, context
                )
                decisions.append(_decide_source(query, source, bindings, now))
        except Exception:
            return _uniform_result(query, RetentionEligibilityStatus.AUTHORITY_UNAVAILABLE)
        status = _aggregate_status(decisions)
        return RetentionEligibility(
            workspace_id=query.workspace_id,
            environment_id=query.environment_id,
            artifact_class=query.artifact_class,
            status=status,
            source_decisions=tuple(decisions),
        )


def _decide_source(
    query: RetentionEligibilityQuery,
    source: RetentionSourceLineage,
    bindings: tuple[RetentionApplicability, ...],
    now: datetime,
) -> RetentionSourceDecision:
    matches = tuple(
        binding
        for binding in bindings
        if binding.workspace_id == query.workspace_id
        and binding.environment_id == query.environment_id
        and query.artifact_class in binding.artifact_classes
        and (
            (
                binding.target_kind is RetentionTargetKind.EXACT_SOURCE
                and binding.source_object_id == source.source_object_id
            )
            or (
                binding.target_kind is RetentionTargetKind.SOURCE_CLASS
                and binding.source_class == source.source_class
            )
        )
    )
    status: RetentionEligibilityStatus
    selected: RetentionApplicability | None = None
    if not matches:
        status = RetentionEligibilityStatus.MISSING
    elif len(matches) > 1:
        policies = {
            (
                item.retention_policy_id,
                item.retention_policy_version_id,
                item.retention_policy_digest,
            )
            for item in matches
        }
        status = (
            RetentionEligibilityStatus.CONFLICTING
            if len(policies) > 1
            else RetentionEligibilityStatus.AMBIGUOUS
        )
    else:
        selected = matches[0]
        if now < selected.effective_from:
            status = RetentionEligibilityStatus.NOT_YET_EFFECTIVE
        elif selected.effective_until is not None and now >= selected.effective_until:
            status = RetentionEligibilityStatus.EXPIRED
        else:
            status = RetentionEligibilityStatus.ELIGIBLE
    return RetentionSourceDecision(
        source_object_id=source.source_object_id,
        source_revision_id=source.source_revision_id,
        status=status,
        applicability_id=(selected.applicability_id if status is RetentionEligibilityStatus.ELIGIBLE else None),
        applicability_generation=(selected.generation if status is RetentionEligibilityStatus.ELIGIBLE else None),
    )


def _aggregate_status(
    decisions: list[RetentionSourceDecision],
) -> RetentionEligibilityStatus:
    precedence = (
        RetentionEligibilityStatus.AUTHORITY_UNAVAILABLE,
        RetentionEligibilityStatus.LINEAGE_UNAVAILABLE,
        RetentionEligibilityStatus.CONFLICTING,
        RetentionEligibilityStatus.AMBIGUOUS,
        RetentionEligibilityStatus.EXPIRED,
        RetentionEligibilityStatus.NOT_YET_EFFECTIVE,
        RetentionEligibilityStatus.MISSING,
    )
    statuses = {item.status for item in decisions}
    return next(
        (status for status in precedence if status in statuses),
        RetentionEligibilityStatus.ELIGIBLE,
    )


def _context_matches(
    query: RetentionEligibilityQuery, context: TrustedPersistenceContext
) -> bool:
    try:
        context.validate_shape()
    except Exception:
        return False
    return isinstance(context.scope, EnvironmentScope) and (
        context.scope.workspace_id,
        context.scope.environment_id,
    ) == (query.workspace_id, query.environment_id)


def _uniform_result(
    query: RetentionEligibilityQuery, status: RetentionEligibilityStatus
) -> RetentionEligibility:
    return RetentionEligibility(
        workspace_id=query.workspace_id,
        environment_id=query.environment_id,
        artifact_class=query.artifact_class,
        status=status,
        source_decisions=tuple(
            RetentionSourceDecision(
                source_object_id=item.source_object_id,
                source_revision_id=item.source_revision_id,
                status=status,
            )
            for item in query.source_lineage
        ),
    )


__all__ = [
    "CurrentRetentionApplicabilityCandidates",
    "RetentionApplicabilityAuthority",
    "RetentionApplicabilityConflictError",
    "RetentionApplicabilityEffectsWriter",
    "RetentionApplicabilityError",
    "RetentionApplicabilityRepository",
    "RetentionEligibility",
    "RetentionEligibilityQuery",
    "RetentionEligibilityReader",
    "RetentionEligibilityStatus",
    "RetentionSourceDecision",
    "RetentionSourceLineage",
]
