"""Derive a disclosure-safe source scope for one retrieval attempt.

This module consumes server-owned profile and publication decisions and reuses
the content-authorization decision service for every candidate source.  It
does not grant access, select a publication, or invoke a physical provider.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from spine.application.content_authorization import (
    AuthorizationContextMode,
    AuthorizationDecisionKind,
    AuthorizationReasonCode,
    ContentAuthorizationDecision,
    ContentAuthorizationDecisionService,
    TrustedAuthorizationContext,
)
from spine.application.processing_disposition import CurrentProcessingDispositionAuthority
from spine.application.context_profiles import EffectiveContextRetrievalPolicy
from spine.domain.common import ValidatedDefinitionModel
from spine.domain.context_profiles import (
    CompletenessRequirement,
    ContextProfileRef,
    PartialAnswerFallback,
    RetrievalStrategy,
)
from spine.domain.projections import (
    CanonicalAsOf,
    ProjectionFreshness,
    ProjectionHealth,
    ProjectionSnapshotMember,
    ProjectionSnapshotMemberDisposition,
    ProjectionSnapshotState,
    PublishedActivePublication,
)


def _require_id(value: UUID, field_name: str) -> UUID:
    if value.int == 0:
        raise ValueError(f"{field_name} must be a non-zero UUID")
    return value


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


class AuthorizedRetrievalScopeOutcome(str, Enum):
    RESOLVED = "resolved"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"
    UNSAFE = "unsafe"


class AuthorizedRetrievalScopeReason(str, Enum):
    RESOLVED = "resolved"
    INVALID_TRUSTED_CONTEXT = "invalid_trusted_context"
    SCOPE_MISMATCH = "scope_mismatch"
    PUBLICATION_UNAVAILABLE = "publication_unavailable"
    AUTHORIZATION_DENIED = "authorization_denied"
    NO_AUTHORIZED_SOURCES = "no_authorized_sources"
    AUTHORIZATION_UNAVAILABLE = "authorization_unavailable"
    SOURCE_UNAVAILABLE = "source_unavailable"
    SOURCE_STALE = "source_stale"
    UNSAFE_PROVIDER_NARROWING = "unsafe_provider_narrowing"


class AuthorizedRetrievalScopeRequest(ValidatedDefinitionModel):
    """Trusted inputs already resolved by CB-05 and CB-06."""

    schema_version: Literal["authorized-retrieval-scope-request:v1"] = (
        "authorized-retrieval-scope-request:v1"
    )
    trusted_context: TrustedAuthorizationContext
    profile: EffectiveContextRetrievalPolicy
    publication: PublishedActivePublication


class AuthorizedSourceConstraint(ValidatedDefinitionModel):
    """The exact source and policy lineage a read plan may consume."""

    schema_version: Literal["authorized-source-constraint:v1"] = (
        "authorized-source-constraint:v1"
    )
    projection_snapshot_member_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    policy_version_id: UUID
    policy_generation: int = Field(ge=1)
    membership_generation: int = Field(ge=1)
    authority_generation: int = Field(ge=1)
    service_authority_generation: int | None = Field(default=None, ge=1)
    decision_reference: UUID

    @field_validator(
        "projection_snapshot_member_id",
        "source_object_id",
        "source_revision_id",
        "policy_version_id",
        "decision_reference",
    )
    @classmethod
    def _validate_ids(cls, value: UUID, info: object) -> UUID:
        return _require_id(value, getattr(info, "field_name", "identifier"))


class AuthorizedRetrievalScope(ValidatedDefinitionModel):
    """Minimum provider-neutral constraints for one authorized read plan."""

    schema_version: Literal["authorized-retrieval-scope:v1"] = (
        "authorized-retrieval-scope:v1"
    )
    workspace_id: UUID
    environment_id: UUID
    profile_ref: ContextProfileRef
    projection_kind: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    projection_snapshot_id: UUID
    projection_activation_id: UUID
    activation_generation: int = Field(ge=1)
    active_pointer_cas_version: int = Field(ge=1)
    canonical_boundary_id: UUID
    canonical_boundary_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_as_of: CanonicalAsOf
    projection_config_version_id: UUID
    publication_freshness: ProjectionFreshness
    publication_health: ProjectionHealth
    source_constraints: tuple[AuthorizedSourceConstraint, ...] = Field(min_length=1)
    retrieval_strategy: RetrievalStrategy
    max_results: int = Field(ge=1, le=1000)
    max_tokens: int = Field(ge=1, le=65536)
    max_relation_hops: int = Field(ge=0, le=8)
    freshness_max_age_seconds: int = Field(ge=0, le=31_536_000)
    completeness_requirement: CompletenessRequirement
    partial_answer_fallback: PartialAnswerFallback

    @field_validator(
        "workspace_id",
        "environment_id",
        "projection_snapshot_id",
        "projection_activation_id",
        "canonical_boundary_id",
        "projection_config_version_id",
    )
    @classmethod
    def _validate_scope_ids(cls, value: UUID, info: object) -> UUID:
        return _require_id(value, getattr(info, "field_name", "identifier"))

    @model_validator(mode="after")
    def _validate_source_uniqueness(self) -> "AuthorizedRetrievalScope":
        source_ids = tuple(item.source_object_id for item in self.source_constraints)
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("authorized source constraints must be unique")
        return self


class AuthorizedRetrievalScopeResolution(ValidatedDefinitionModel):
    """Safe result; failures never carry source or publication details."""

    schema_version: Literal["authorized-retrieval-scope-resolution:v1"] = (
        "authorized-retrieval-scope-resolution:v1"
    )
    outcome: AuthorizedRetrievalScopeOutcome
    reason: AuthorizedRetrievalScopeReason
    scope: AuthorizedRetrievalScope | None = None

    @model_validator(mode="after")
    def _validate_outcome(self) -> "AuthorizedRetrievalScopeResolution":
        resolved = self.outcome is AuthorizedRetrievalScopeOutcome.RESOLVED
        if resolved:
            if self.reason is not AuthorizedRetrievalScopeReason.RESOLVED:
                raise ValueError("resolved scope requires the resolved reason")
            if self.scope is None:
                raise ValueError("resolved scope requires scope data")
        elif self.scope is not None:
            raise ValueError("rejected scope cannot carry scope data")
        elif self.reason is AuthorizedRetrievalScopeReason.RESOLVED:
            raise ValueError("rejected scope cannot use the resolved reason")
        return self


class SnapshotMemberRepository(Protocol):
    async def list_snapshot_members(
        self, projection_snapshot_id: UUID
    ) -> tuple[ProjectionSnapshotMember, ...]: ...


class ProviderScopeSafetyPolicy(Protocol):
    """Prove that a downstream provider can enforce this exact scope."""

    async def can_express(self, scope: AuthorizedRetrievalScope) -> bool: ...


class RetrievalDisclosureConstraint(Protocol):
    """Additional deny-only lifecycle/hold authority for a candidate member."""

    async def allows(
        self,
        context: TrustedAuthorizationContext,
        member: ProjectionSnapshotMember,
        evaluated_at: datetime,
    ) -> bool: ...


class AuthorizedRetrievalScopeResolver:
    """Resolve an authorized subset without leaking filtered source state."""

    def __init__(
        self,
        *,
        authorization: ContentAuthorizationDecisionService,
        snapshot_members: SnapshotMemberRepository,
        disclosure_constraint: RetrievalDisclosureConstraint,
        processing_authority: CurrentProcessingDispositionAuthority,
        provider_scope_safety: ProviderScopeSafetyPolicy | None = None,
        expected_purpose: str = "answer_question",
        expected_operation: str = "retrieve_context",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._authorization = authorization
        self._snapshot_members = snapshot_members
        self._provider_scope_safety = provider_scope_safety
        self._disclosure_constraint = disclosure_constraint
        self._processing_authority = processing_authority
        self._expected_purpose = expected_purpose
        self._expected_operation = expected_operation
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def resolve(
        self, request: AuthorizedRetrievalScopeRequest
    ) -> AuthorizedRetrievalScopeResolution:
        if type(request) is not AuthorizedRetrievalScopeRequest:
            return self._reject(
                AuthorizedRetrievalScopeOutcome.DENIED,
                AuthorizedRetrievalScopeReason.INVALID_TRUSTED_CONTEXT,
            )

        context = request.trusted_context
        profile = request.profile
        publication = request.publication
        if not self._request_is_in_scope(context, profile, publication):
            return self._reject(
                AuthorizedRetrievalScopeOutcome.DENIED,
                AuthorizedRetrievalScopeReason.SCOPE_MISMATCH,
            )
        if (
            context.purpose != self._expected_purpose
            or context.operation != self._expected_operation
            or context.mode
            not in {
                AuthorizationContextMode.HUMAN_DISCLOSURE,
                AuthorizationContextMode.INTERACTIVE_PROCESSING,
                AuthorizationContextMode.NONINTERACTIVE_PROCESSING,
            }
        ):
            return self._reject(
                AuthorizedRetrievalScopeOutcome.DENIED,
                AuthorizedRetrievalScopeReason.INVALID_TRUSTED_CONTEXT,
            )
        if (
            publication.projection_snapshot.snapshot_state
            is not ProjectionSnapshotState.FINALIZED
        ):
            return self._reject(
                AuthorizedRetrievalScopeOutcome.UNAVAILABLE,
                AuthorizedRetrievalScopeReason.PUBLICATION_UNAVAILABLE,
            )

        try:
            members = await self._snapshot_members.list_snapshot_members(
                publication.projection_snapshot.projection_snapshot_id
            )
        except Exception:
            return self._reject(
                AuthorizedRetrievalScopeOutcome.UNAVAILABLE,
                AuthorizedRetrievalScopeReason.PUBLICATION_UNAVAILABLE,
            )
        included = self._included_members(members, publication)
        if included is None:
            return self._reject(
                AuthorizedRetrievalScopeOutcome.UNAVAILABLE,
                AuthorizedRetrievalScopeReason.PUBLICATION_UNAVAILABLE,
            )
        if not included:
            return self._reject(
                AuthorizedRetrievalScopeOutcome.DENIED,
                AuthorizedRetrievalScopeReason.NO_AUTHORIZED_SOURCES,
            )

        constraints: list[AuthorizedSourceConstraint] = []
        for member in included:
            try:
                processing = await self._processing_authority.resolve(
                    workspace_id=context.workspace_id,
                    environment_id=context.environment_id,
                    source_object_id=member.source_object_id,
                    source_revision_id=member.source_revision_id,
                )
            except Exception:
                processing = None
            if processing is None or not processing.eligible:
                return self._reject(
                    AuthorizedRetrievalScopeOutcome.UNAVAILABLE,
                    AuthorizedRetrievalScopeReason.SOURCE_UNAVAILABLE,
                )
            try:
                disclosure_allowed = await self._disclosure_constraint.allows(
                    context, member, self._now()
                )
            except Exception:
                disclosure_allowed = False
            if not disclosure_allowed:
                return self._reject(
                    AuthorizedRetrievalScopeOutcome.UNAVAILABLE,
                    AuthorizedRetrievalScopeReason.SOURCE_UNAVAILABLE,
                )
            decision = await self._decide(context, member.source_object_id)
            if decision is None:
                return self._reject(
                    AuthorizedRetrievalScopeOutcome.UNAVAILABLE,
                    AuthorizedRetrievalScopeReason.AUTHORIZATION_UNAVAILABLE,
                )
            if not decision.allowed:
                if decision.reason_code is AuthorizationReasonCode.AUTHORIZATION_DENIED:
                    continue
                return self._reject(
                    AuthorizedRetrievalScopeOutcome.UNAVAILABLE,
                    self._reason_for_denial(decision.reason_code),
                )
            constraint = self._constraint(member, decision, profile, self._now())
            if isinstance(constraint, AuthorizedRetrievalScopeReason):
                return self._reject(
                    AuthorizedRetrievalScopeOutcome.UNAVAILABLE,
                    constraint,
                )
            constraints.append(constraint)

        if not constraints:
            return self._reject(
                AuthorizedRetrievalScopeOutcome.DENIED,
                AuthorizedRetrievalScopeReason.NO_AUTHORIZED_SOURCES,
            )

        try:
            scope = AuthorizedRetrievalScope(
                workspace_id=profile.workspace_id,
                environment_id=profile.environment_id,
                profile_ref=profile.profile_ref,
                projection_kind=publication.projection_kind,
                projection_snapshot_id=publication.projection_snapshot.projection_snapshot_id,
                projection_activation_id=publication.projection_activation.projection_activation_id,
                activation_generation=publication.activation_generation,
                active_pointer_cas_version=publication.active_pointer_cas_version,
                canonical_boundary_id=publication.canonical_boundary_id,
                canonical_boundary_manifest_digest=publication.canonical_boundary_manifest_digest,
                canonical_as_of=publication.as_of,
                projection_config_version_id=publication.projection_config_version.projection_config_version_id,
                publication_freshness=publication.freshness,
                publication_health=publication.health,
                source_constraints=tuple(constraints),
                retrieval_strategy=profile.retrieval_strategy,
                max_results=profile.max_results,
                max_tokens=profile.max_tokens,
                max_relation_hops=profile.max_relation_hops,
                freshness_max_age_seconds=profile.freshness_max_age_seconds,
                completeness_requirement=profile.completeness_requirement,
                partial_answer_fallback=profile.partial_answer_fallback,
            )
        except Exception:
            return self._reject(
                AuthorizedRetrievalScopeOutcome.UNAVAILABLE,
                AuthorizedRetrievalScopeReason.AUTHORIZATION_UNAVAILABLE,
            )
        if self._provider_scope_safety is None:
            return self._reject(
                AuthorizedRetrievalScopeOutcome.UNSAFE,
                AuthorizedRetrievalScopeReason.UNSAFE_PROVIDER_NARROWING,
            )
        if self._provider_scope_safety is not None:
            try:
                safe = await self._provider_scope_safety.can_express(scope)
            except Exception:
                safe = False
            if not safe:
                return self._reject(
                    AuthorizedRetrievalScopeOutcome.UNSAFE,
                    AuthorizedRetrievalScopeReason.UNSAFE_PROVIDER_NARROWING,
                )
        return AuthorizedRetrievalScopeResolution(
            outcome=AuthorizedRetrievalScopeOutcome.RESOLVED,
            reason=AuthorizedRetrievalScopeReason.RESOLVED,
            scope=scope,
        )

    async def _decide(
        self, context: TrustedAuthorizationContext, source_object_id: UUID
    ) -> ContentAuthorizationDecision | None:
        try:
            if context.mode is AuthorizationContextMode.HUMAN_DISCLOSURE:
                return await self._authorization.decide_human_disclosure(
                    context, [source_object_id]
                )
            if context.mode is AuthorizationContextMode.INTERACTIVE_PROCESSING:
                return await self._authorization.decide_interactive_processing(
                    context, [source_object_id]
                )
            return await self._authorization.decide_noninteractive_processing(
                context, [source_object_id]
            )
        except Exception:
            return None

    def _constraint(
        self,
        member: ProjectionSnapshotMember,
        decision: ContentAuthorizationDecision,
        profile: EffectiveContextRetrievalPolicy,
        now: datetime,
    ) -> AuthorizedSourceConstraint | AuthorizedRetrievalScopeReason:
        if len(decision.policy_evidence) != 1 or decision.source_object_ids != (
            member.source_object_id,
        ):
            return AuthorizedRetrievalScopeReason.AUTHORIZATION_UNAVAILABLE
        evidence = decision.policy_evidence[0]
        if evidence.source_revision_id != member.source_revision_id:
            return AuthorizedRetrievalScopeReason.SOURCE_UNAVAILABLE
        if evidence.retention_eligible is not True:
            return AuthorizedRetrievalScopeReason.SOURCE_UNAVAILABLE
        observed_at = evidence.source_revision_observed_at
        if observed_at is None:
            return AuthorizedRetrievalScopeReason.SOURCE_STALE
        age = now - observed_at
        if (
            age.total_seconds() < 0
            or age.total_seconds() > profile.freshness_max_age_seconds
        ):
            return AuthorizedRetrievalScopeReason.SOURCE_STALE
        return AuthorizedSourceConstraint(
            projection_snapshot_member_id=member.projection_snapshot_member_id,
            source_object_id=member.source_object_id,
            source_revision_id=member.source_revision_id,
            policy_version_id=evidence.policy_version_id,
            policy_generation=evidence.policy_generation,
            membership_generation=decision.membership_generation,
            authority_generation=decision.authority_generation,
            service_authority_generation=decision.service_authority_generation,
            decision_reference=decision.decision_reference,
        )

    def _request_is_in_scope(
        self,
        context: TrustedAuthorizationContext,
        profile: EffectiveContextRetrievalPolicy,
        publication: PublishedActivePublication,
    ) -> bool:
        return (
            context.workspace_id == profile.workspace_id == publication.workspace_id
            and context.environment_id
            == profile.environment_id
            == publication.environment_id
            and self._publication_is_consistent(publication)
        )

    @staticmethod
    def _publication_is_consistent(publication: PublishedActivePublication) -> bool:
        snapshot = publication.projection_snapshot
        activation = publication.projection_activation
        config = publication.projection_config_version
        nested = (snapshot, activation, config)
        return (
            all(
                item.workspace_id == publication.workspace_id
                and item.environment_id == publication.environment_id
                and item.projection_kind == publication.projection_kind
                for item in nested
            )
            and snapshot.projection_snapshot_id == activation.projection_snapshot_id
            and snapshot.projection_config_version_id
            == config.projection_config_version_id
            and snapshot.canonical_boundary_id == publication.canonical_boundary_id
            and activation.activation_generation == publication.activation_generation
            and publication.as_of.canonical_boundary_id
            == publication.canonical_boundary_id
            and publication.as_of.accepted_through_generation
            == publication.accepted_through_generation
        )

    @staticmethod
    def _included_members(
        members: tuple[ProjectionSnapshotMember, ...],
        publication: PublishedActivePublication,
    ) -> tuple[ProjectionSnapshotMember, ...] | None:
        if any(type(member) is not ProjectionSnapshotMember for member in members):
            return None
        seen: set[UUID] = set()
        included: list[ProjectionSnapshotMember] = []
        for member in members:
            if (
                member.projection_snapshot_id
                != publication.projection_snapshot.projection_snapshot_id
                or member.canonical_boundary_id != publication.canonical_boundary_id
                or member.workspace_id != publication.workspace_id
                or member.environment_id != publication.environment_id
            ):
                return None
            if member.source_object_id in seen:
                return None
            seen.add(member.source_object_id)
            if member.disposition is ProjectionSnapshotMemberDisposition.INCLUDED:
                included.append(member)
        return tuple(sorted(included, key=lambda item: item.source_object_id.hex))

    @staticmethod
    def _reason_for_denial(
        reason: AuthorizationReasonCode,
    ) -> AuthorizedRetrievalScopeReason:
        if reason is AuthorizationReasonCode.SOURCE_UNAVAILABLE:
            return AuthorizedRetrievalScopeReason.SOURCE_UNAVAILABLE
        if reason is AuthorizationReasonCode.AUTHORITY_STALE:
            return AuthorizedRetrievalScopeReason.AUTHORIZATION_UNAVAILABLE
        if reason is AuthorizationReasonCode.CONTEXT_INVALID:
            return AuthorizedRetrievalScopeReason.INVALID_TRUSTED_CONTEXT
        return AuthorizedRetrievalScopeReason.AUTHORIZATION_UNAVAILABLE

    def _now(self) -> datetime:
        return _aware(self._clock(), "scope clock")

    @staticmethod
    def _reject(
        outcome: AuthorizedRetrievalScopeOutcome,
        reason: AuthorizedRetrievalScopeReason,
    ) -> AuthorizedRetrievalScopeResolution:
        return AuthorizedRetrievalScopeResolution(outcome=outcome, reason=reason)


class AuthorizedRetrievalScopeResolverPort(Protocol):
    async def resolve(
        self, request: AuthorizedRetrievalScopeRequest
    ) -> AuthorizedRetrievalScopeResolution: ...


AuthorizedRetrievalScopeV1 = AuthorizedRetrievalScope


__all__ = [
    "AuthorizedRetrievalScope",
    "AuthorizedRetrievalScopeOutcome",
    "AuthorizedRetrievalScopeReason",
    "AuthorizedRetrievalScopeRequest",
    "AuthorizedRetrievalScopeResolution",
    "AuthorizedRetrievalScopeResolver",
    "AuthorizedRetrievalScopeResolverPort",
    "AuthorizedRetrievalScopeV1",
    "AuthorizedSourceConstraint",
    "ProviderScopeSafetyPolicy",
    "SnapshotMemberRepository",
]
