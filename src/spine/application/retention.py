"""Resolve current retention eligibility from immutable source lineage."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field, model_validator

from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedPersistenceContext,
)
from spine.application.persistence.unit_of_work import UnitOfWork
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


class RetentionArtifactRef(DefinitionModel):
    """Exact immutable source-bearing artifact whose lineage must be resolved."""

    workspace_id: UUID
    environment_id: UUID
    artifact_class: RetentionArtifactClass
    artifact_id: UUID
    artifact_version: int = Field(ge=1)
    artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_identity(self) -> "RetentionArtifactRef":
        if any(
            value.int == 0
            for value in (self.workspace_id, self.environment_id, self.artifact_id)
        ):
            raise ValueError("retention artifact identifiers must be non-zero")
        return self


class RetentionEligibilityQuery(DefinitionModel):
    artifact: RetentionArtifactRef


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
            if self.status not in {
                RetentionEligibilityStatus.LINEAGE_UNAVAILABLE,
                RetentionEligibilityStatus.AUTHORITY_UNAVAILABLE,
            }:
                raise ValueError("retention eligibility requires source decisions")
            return self
        expected_status = _aggregate_status(list(self.source_decisions))
        if self.status is not expected_status:
            raise ValueError("aggregate retention status must match source decisions")
        return self


class CurrentRetentionApplicabilityCandidates(Protocol):
    """Return only current-pointer bindings matching exact revision lineage."""

    async def list_current_applicabilities(
        self,
        source: RetentionSourceLineage,
        context: TrustedPersistenceContext,
    ) -> tuple[RetentionApplicability, ...]: ...


class RetentionArtifactLineageResolver(Protocol):
    """Resolve complete, canonical lineage for one immutable artifact version."""

    async def resolve_artifact_lineage(
        self,
        artifact: RetentionArtifactRef,
        context: TrustedPersistenceContext,
    ) -> tuple[RetentionSourceLineage, ...]: ...


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
        transaction: UnitOfWork,
    ) -> None: ...

    async def is_current(
        self,
        applicability: RetentionApplicability,
        context: TrustedPersistenceContext,
    ) -> bool: ...


class RetentionApplicabilityEffectsWriter(Protocol):
    """Persist commit-consistent safe audit and outbox records."""

    async def persist(
        self,
        applicability: RetentionApplicability,
        context: TrustedPersistenceContext,
        transaction: UnitOfWork,
    ) -> tuple[UUID, UUID]: ...


class RetentionPolicyApprovalReader(Protocol):
    async def policy_is_current(
        self,
        applicability: RetentionApplicability,
        context: TrustedPersistenceContext,
        transaction: UnitOfWork | None,
    ) -> bool: ...


class RetentionBindingApprovalReader(Protocol):
    async def binding_is_approved(
        self,
        applicability: RetentionApplicability,
        context: TrustedPersistenceContext,
        transaction: UnitOfWork | None,
    ) -> bool: ...


class ApprovedRetentionApplicabilityAuthority:
    """Verify separate policy and applicability approvals without inferring either."""

    def __init__(
        self,
        *,
        policies: RetentionPolicyApprovalReader,
        bindings: RetentionBindingApprovalReader,
    ) -> None:
        self._policies = policies
        self._bindings = bindings

    async def verify(
        self,
        applicability: RetentionApplicability,
        context: TrustedPersistenceContext,
        transaction: UnitOfWork,
    ) -> None:
        try:
            approved = await self._verify(applicability, context, transaction)
        except RetentionApplicabilityError:
            raise
        except Exception:
            raise RetentionApplicabilityError() from None
        if not approved:
            raise RetentionApplicabilityError()

    async def is_current(
        self,
        applicability: RetentionApplicability,
        context: TrustedPersistenceContext,
    ) -> bool:
        try:
            return await self._verify(applicability, context, None)
        except Exception:
            return False

    async def _verify(
        self,
        applicability: RetentionApplicability,
        context: TrustedPersistenceContext,
        transaction: UnitOfWork | None,
    ) -> bool:
        if not _scope_matches_binding(applicability, context):
            return False
        policy_current = await self._policies.policy_is_current(
            applicability, context, transaction
        )
        if not policy_current:
            return False
        return await self._bindings.binding_is_approved(
            applicability, context, transaction
        )


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
        lineages: RetentionArtifactLineageResolver,
        authority: RetentionApplicabilityAuthority,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._candidates = candidates
        self._lineages = lineages
        self._authority = authority
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def resolve(
        self,
        query: RetentionEligibilityQuery,
        context: TrustedPersistenceContext,
    ) -> RetentionEligibility:
        if not _context_matches(query, context):
            return _empty_result(query, RetentionEligibilityStatus.AUTHORITY_UNAVAILABLE)
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            return _empty_result(query, RetentionEligibilityStatus.AUTHORITY_UNAVAILABLE)
        decisions: list[RetentionSourceDecision] = []
        try:
            source_lineage = await self._lineages.resolve_artifact_lineage(
                query.artifact, context
            )
            if not source_lineage or len(
                {(item.source_object_id, item.source_revision_id) for item in source_lineage}
            ) != len(source_lineage):
                return _empty_result(
                    query, RetentionEligibilityStatus.LINEAGE_UNAVAILABLE
                )
            for source in source_lineage:
                bindings = await self._candidates.list_current_applicabilities(
                    source, context
                )
                authority_current = True
                for binding in bindings:
                    if not await self._authority.is_current(binding, context):
                        authority_current = False
                        break
                if bindings and not authority_current:
                    decisions.append(
                        _source_result(
                            source, RetentionEligibilityStatus.AUTHORITY_UNAVAILABLE
                        )
                    )
                    continue
                decisions.append(_decide_source(query, source, bindings, now))
        except Exception:
            return _empty_result(query, RetentionEligibilityStatus.AUTHORITY_UNAVAILABLE)
        status = _aggregate_status(decisions)
        return RetentionEligibility(
            workspace_id=query.artifact.workspace_id,
            environment_id=query.artifact.environment_id,
            artifact_class=query.artifact.artifact_class,
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
        if binding.workspace_id == query.artifact.workspace_id
        and binding.environment_id == query.artifact.environment_id
        and query.artifact.artifact_class in binding.artifact_classes
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
    ) == (query.artifact.workspace_id, query.artifact.environment_id)


def _empty_result(
    query: RetentionEligibilityQuery, status: RetentionEligibilityStatus
) -> RetentionEligibility:
    return RetentionEligibility(
        workspace_id=query.artifact.workspace_id,
        environment_id=query.artifact.environment_id,
        artifact_class=query.artifact.artifact_class,
        status=status,
        source_decisions=(),
    )


def _source_result(
    source: RetentionSourceLineage, status: RetentionEligibilityStatus
) -> RetentionSourceDecision:
    return RetentionSourceDecision(
        source_object_id=source.source_object_id,
        source_revision_id=source.source_revision_id,
        status=status,
    )


def _scope_matches_binding(
    applicability: RetentionApplicability, context: TrustedPersistenceContext
) -> bool:
    try:
        context.validate_shape()
    except Exception:
        return False
    return isinstance(context.scope, EnvironmentScope) and (
        context.scope.workspace_id,
        context.scope.environment_id,
    ) == (applicability.workspace_id, applicability.environment_id)


__all__ = [
    "ApprovedRetentionApplicabilityAuthority",
    "CurrentRetentionApplicabilityCandidates",
    "RetentionArtifactLineageResolver",
    "RetentionArtifactRef",
    "RetentionApplicabilityAuthority",
    "RetentionApplicabilityConflictError",
    "RetentionApplicabilityEffectsWriter",
    "RetentionApplicabilityError",
    "RetentionApplicabilityRepository",
    "RetentionBindingApprovalReader",
    "RetentionEligibility",
    "RetentionEligibilityQuery",
    "RetentionEligibilityReader",
    "RetentionEligibilityStatus",
    "RetentionPolicyApprovalReader",
    "RetentionSourceDecision",
    "RetentionSourceLineage",
]
