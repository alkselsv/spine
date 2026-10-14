"""Concrete ContextProfile composition adapters for the Context Broker."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from spine.application.context_profiles import (
    ContextProfileResolutionOutcome,
    ContextProfileResolutionPort,
    ContextProfileResolutionRequest,
    ServerApprovedContextProfileResolver,
)
from spine.application.persistence.errors import (
    ContextProfileApprovalError,
    ContextProfileDigestMismatchError,
    ContextProfileNotFoundError,
    ContextProfileRevokedError,
)
from spine.domain.context_profiles import ContextProfileRef, ContextProfileVersion
from spine.memory.contracts import ContextProfile, ContextRequest


class ContextProfileBrokerAdapter:
    """Bridge the application resolver into the provider-neutral broker seam."""

    def __init__(self, resolver: ContextProfileResolutionPort) -> None:
        self._resolver = resolver

    async def resolve_for_request(self, request: ContextRequest) -> ContextProfile | None:
        resolution = await self._resolver.resolve(
            ContextProfileResolutionRequest(
                workspace_id=request.workspace_id,
                environment_id=request.environment_id,
                profile_ref=request.profile_ref,
                trusted_request_context_ref=request.trusted_request_context_ref,
                authorization_context_ref=request.authorization_context_ref,
                limit=request.limit,
            )
        )
        if (
            resolution.outcome is not ContextProfileResolutionOutcome.RESOLVED
            or resolution.policy is None
        ):
            return None
        policy = resolution.policy
        return ContextProfile(
            ref=policy.profile_ref,
            retrieval_strategy=policy.retrieval_strategy.value,
            max_items=policy.max_results,
            max_tokens=policy.max_tokens,
            max_relation_hops=policy.max_relation_hops,
            freshness_max_age_seconds=policy.freshness_max_age_seconds,
            completeness_requirement=policy.completeness_requirement.value,
            partial_answer_fallback=policy.partial_answer_fallback.value,
        )


class _DeterministicProfileDirectory:
    def __init__(
        self,
        profiles: Mapping[
            ContextProfileRef | tuple[UUID, UUID, ContextProfileRef], ContextProfileVersion
        ],
        *,
        error: Exception | None = None,
    ) -> None:
        self._profiles: dict[
            tuple[UUID, UUID, ContextProfileRef], ContextProfileVersion
        ] = {}
        for key, profile in profiles.items():
            if isinstance(key, tuple):
                self._profiles[key] = profile
            else:
                self._profiles[(profile.workspace_id, profile.environment_id, key)] = profile
        self.error = error
        self.requests: list[tuple[UUID, int, str]] = []

    async def resolve_for_scope(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        profile_id: UUID,
        profile_version: int,
        profile_digest: str,
    ) -> ContextProfileVersion:
        self.requests.append((profile_id, profile_version, profile_digest))
        if self.error is not None:
            raise self.error
        scoped_profiles = [
            profile
            for (stored_workspace, stored_environment, stored_ref), profile in self._profiles.items()
            if (
                stored_workspace == workspace_id
                and stored_environment == environment_id
                and stored_ref.profile_id == profile_id
                and stored_ref.profile_version == profile_version
            )
        ]
        profile = next(
            (item for item in scoped_profiles if item.profile_digest == profile_digest),
            None,
        )
        if not scoped_profiles:
            raise ContextProfileNotFoundError("Context profile is unavailable.")
        if profile is None:
            raise ContextProfileDigestMismatchError("Context profile identity is invalid.")
        if profile.revoked:
            raise ContextProfileRevokedError("Context profile is unavailable.")
        if not profile.server_approved:
            raise ContextProfileApprovalError("Context profile is unavailable.")
        return profile


class _DeterministicCapabilityPolicy:
    def __init__(self, *, allowed: bool) -> None:
        self.allowed = allowed

    async def allows(self, request: ContextProfileResolutionRequest) -> bool:
        del request
        return self.allowed


class DeterministicFakeContextProfileResolver(ServerApprovedContextProfileResolver):
    """Offline fake implementing the same application resolution port."""

    def __init__(
        self,
        profiles: Mapping[
            ContextProfileRef | tuple[UUID, UUID, ContextProfileRef], ContextProfileVersion
        ],
        *,
        error: Exception | None = None,
        capability_allowed: bool = True,
    ) -> None:
        self.repository = _DeterministicProfileDirectory(profiles, error=error)
        super().__init__(
            repository=self.repository,
            capability_policy=_DeterministicCapabilityPolicy(allowed=capability_allowed),
        )


FakeContextProfileResolver = DeterministicFakeContextProfileResolver

__all__ = [
    "ContextProfileBrokerAdapter",
    "DeterministicFakeContextProfileResolver",
    "FakeContextProfileResolver",
]
