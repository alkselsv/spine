"""Resolve server-approved context profiles for one trusted broker request.

The resolver is deliberately smaller than the Context Broker.  It chooses an
immutable, tenant-scoped profile and derives the effective retrieval limits;
it does not resolve publication state or make a content authorization
decision.  Those authorities remain separate ports.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from spine.application.persistence.context_profiles import ContextProfileRepository
from spine.application.persistence.errors import (
    ContextProfileApprovalError,
    ContextProfileDigestMismatchError,
    ContextProfileError,
    ContextProfileMalformedError,
    ContextProfileNotFoundError,
    ContextProfileRevokedError,
)
from spine.domain.common import ValidatedDefinitionModel
from spine.domain.context_profiles import (
    CompletenessRequirement,
    ContextProfileRef,
    ContextProfileVersion,
    PartialAnswerFallback,
    RetrievalStrategy,
)


class ContextProfileCapability(str, Enum):
    """The capability required to ask the broker for context."""

    RETRIEVE = "context.retrieve"


class ContextProfileResolutionOutcome(str, Enum):
    RESOLVED = "resolved"
    REJECTED = "rejected"


class ContextProfileResolutionReason(str, Enum):
    RESOLVED = "resolved"
    INVALID_TRUSTED_CONTEXT = "invalid_trusted_context"
    CAPABILITY_DENIED = "capability_denied"
    PROFILE_UNAVAILABLE = "profile_unavailable"
    PROFILE_DIGEST_MISMATCH = "profile_digest_mismatch"
    PROFILE_REVOKED = "profile_revoked"
    PROFILE_MALFORMED = "profile_malformed"
    PROFILE_NOT_APPROVED = "profile_not_approved"
    PROFILE_SCOPE_MISMATCH = "profile_scope_mismatch"
    INVALID_REQUEST_OVERRIDE = "invalid_request_override"


class ContextProfileResolutionRequest(ValidatedDefinitionModel):
    """Trusted request context plus caller limits for one resolution.

    The object is issued by the trusted composition boundary.  Its closed
    shape is intentional: projection snapshots, physical namespaces,
    candidates, boundaries and access policies are not request selectors.
    """

    schema_version: Literal["context-profile-resolution-request:v1"] = (
        "context-profile-resolution-request:v1"
    )
    workspace_id: UUID
    environment_id: UUID
    profile_ref: ContextProfileRef
    trusted_request_context_ref: UUID
    authorization_context_ref: UUID
    limit: int = Field(default=15, ge=1, le=1000)
    max_tokens: int | None = Field(default=None, ge=1, le=65536)
    max_relation_hops: int | None = Field(default=None, ge=0, le=8)
    freshness_max_age_seconds: int | None = Field(default=None, ge=0, le=31_536_000)
    completeness_requirement: CompletenessRequirement | None = None
    allow_partial_answer: bool | None = None

    @field_validator(
        "workspace_id",
        "environment_id",
        "trusted_request_context_ref",
        "authorization_context_ref",
    )
    @classmethod
    def _require_non_zero_ids(cls, value: UUID, info: object) -> UUID:
        if value.int == 0:
            raise ValueError(f"{getattr(info, 'field_name', 'scope')} must be a non-zero UUID")
        return value


class EffectiveContextRetrievalPolicy(ValidatedDefinitionModel):
    """The only profile data a provider-neutral retrieval adapter needs."""

    schema_version: Literal["effective-context-retrieval-policy:v1"] = (
        "effective-context-retrieval-policy:v1"
    )
    workspace_id: UUID
    environment_id: UUID
    profile_ref: ContextProfileRef
    retrieval_strategy: RetrievalStrategy
    max_results: int = Field(ge=1, le=1000)
    max_tokens: int = Field(ge=1, le=65536)
    max_relation_hops: int = Field(ge=0, le=8)
    freshness_max_age_seconds: int = Field(ge=0, le=31_536_000)
    completeness_requirement: CompletenessRequirement
    partial_answer_fallback: PartialAnswerFallback

    @field_validator("workspace_id", "environment_id")
    @classmethod
    def _require_non_zero_scope(cls, value: UUID, info: object) -> UUID:
        if value.int == 0:
            raise ValueError(f"{getattr(info, 'field_name', 'scope')} must be a non-zero UUID")
        return value


class ContextProfileResolution(ValidatedDefinitionModel):
    """Safe resolution outcome; rejected outcomes carry no profile data."""

    schema_version: Literal["context-profile-resolution:v1"] = (
        "context-profile-resolution:v1"
    )
    outcome: ContextProfileResolutionOutcome
    reason: ContextProfileResolutionReason
    profile: ContextProfileVersion | None = None
    policy: EffectiveContextRetrievalPolicy | None = None

    @model_validator(mode="after")
    def _validate_outcome(self) -> "ContextProfileResolution":
        resolved = self.outcome is ContextProfileResolutionOutcome.RESOLVED
        if resolved:
            if self.reason is not ContextProfileResolutionReason.RESOLVED:
                raise ValueError("resolved profile requires the resolved reason")
            if self.profile is None or self.policy is None:
                raise ValueError("resolved profile requires profile and policy")
        elif self.profile is not None or self.policy is not None:
            raise ValueError("rejected profile cannot carry profile data")
        elif self.reason is ContextProfileResolutionReason.RESOLVED:
            raise ValueError("rejected profile cannot use the resolved reason")
        return self


class ContextProfileCapabilityPolicy(Protocol):
    """Server-owned capability check; a profile is never an authorization grant."""

    async def allows(self, request: ContextProfileResolutionRequest) -> bool: ...


class ContextProfileResolutionPort(Protocol):
    """Application port consumed by the Context Broker composition root."""

    async def resolve(
        self, request: ContextProfileResolutionRequest
    ) -> ContextProfileResolution: ...


class DefaultContextProfileCapabilityPolicy:
    """Fail-closed default; production composition must provide an attestation policy."""

    async def allows(self, request: ContextProfileResolutionRequest) -> bool:
        del request
        return False


class ServerApprovedContextProfileResolver:
    """Resolve one exact immutable profile and narrow caller retrieval limits."""

    def __init__(
        self,
        *,
        repository: ContextProfileRepository,
        capability_policy: ContextProfileCapabilityPolicy | None = None,
    ) -> None:
        self._repository = repository
        self._capability_policy = capability_policy or DefaultContextProfileCapabilityPolicy()

    async def resolve(
        self, request: ContextProfileResolutionRequest
    ) -> ContextProfileResolution:
        if type(request) is not ContextProfileResolutionRequest:
            return self._rejected(ContextProfileResolutionReason.INVALID_TRUSTED_CONTEXT)

        try:
            allowed = await self._capability_policy.allows(request)
        except Exception:
            allowed = False
        if not allowed:
            return self._rejected(ContextProfileResolutionReason.CAPABILITY_DENIED)

        try:
            profile = await self._repository.resolve_for_scope(
                workspace_id=request.workspace_id,
                environment_id=request.environment_id,
                profile_id=request.profile_ref.profile_id,
                profile_version=request.profile_ref.profile_version,
                profile_digest=request.profile_ref.profile_digest,
            )
        except ContextProfileDigestMismatchError:
            return self._rejected(ContextProfileResolutionReason.PROFILE_DIGEST_MISMATCH)
        except ContextProfileRevokedError:
            return self._rejected(ContextProfileResolutionReason.PROFILE_REVOKED)
        except ContextProfileApprovalError:
            return self._rejected(ContextProfileResolutionReason.PROFILE_NOT_APPROVED)
        except ContextProfileMalformedError:
            return self._rejected(ContextProfileResolutionReason.PROFILE_MALFORMED)
        except ContextProfileNotFoundError:
            return self._rejected(ContextProfileResolutionReason.PROFILE_UNAVAILABLE)
        except ContextProfileError:
            return self._rejected(ContextProfileResolutionReason.PROFILE_UNAVAILABLE)
        except Exception:
            return self._rejected(ContextProfileResolutionReason.PROFILE_UNAVAILABLE)

        if type(profile) is not ContextProfileVersion:
            return self._rejected(ContextProfileResolutionReason.PROFILE_MALFORMED)
        if (
            profile.workspace_id != request.workspace_id
            or profile.environment_id != request.environment_id
        ):
            return self._rejected(ContextProfileResolutionReason.PROFILE_SCOPE_MISMATCH)
        if profile.reference != request.profile_ref:
            return self._rejected(ContextProfileResolutionReason.PROFILE_DIGEST_MISMATCH)
        try:
            if profile.revoked:
                return self._rejected(ContextProfileResolutionReason.PROFILE_REVOKED)
            if not profile.server_approved:
                return self._rejected(ContextProfileResolutionReason.PROFILE_NOT_APPROVED)
            if profile.profile_digest != ContextProfileVersion.calculate_digest(profile):
                return self._rejected(ContextProfileResolutionReason.PROFILE_MALFORMED)
            policy = _effective_policy(request, profile)
        except _InvalidRequestOverride:
            return self._rejected(ContextProfileResolutionReason.INVALID_REQUEST_OVERRIDE)
        except (TypeError, ValueError):
            return self._rejected(ContextProfileResolutionReason.PROFILE_MALFORMED)

        return ContextProfileResolution(
            outcome=ContextProfileResolutionOutcome.RESOLVED,
            reason=ContextProfileResolutionReason.RESOLVED,
            profile=profile,
            policy=policy,
        )

    @staticmethod
    def _rejected(reason: ContextProfileResolutionReason) -> ContextProfileResolution:
        return ContextProfileResolution(
            outcome=ContextProfileResolutionOutcome.REJECTED,
            reason=reason,
        )


class _InvalidRequestOverride(ValueError):
    pass


def _effective_policy(
    request: ContextProfileResolutionRequest,
    profile: ContextProfileVersion,
) -> EffectiveContextRetrievalPolicy:
    requested_completeness = request.completeness_requirement
    completeness = profile.completeness_requirement
    if requested_completeness is not None:
        order = {
            CompletenessRequirement.BEST_EFFORT: 0,
            CompletenessRequirement.COMPLETE: 1,
            CompletenessRequirement.EXHAUSTIVE: 2,
        }
        completeness = max(
            (profile.completeness_requirement, requested_completeness),
            key=order.__getitem__,
        )

    if (
        request.allow_partial_answer is True
        and profile.partial_answer_fallback is PartialAnswerFallback.DISALLOWED
    ):
        raise _InvalidRequestOverride("partial fallback cannot be expanded")
    partial_fallback = (
        PartialAnswerFallback.DISALLOWED
        if request.allow_partial_answer is False
        else profile.partial_answer_fallback
    )
    return EffectiveContextRetrievalPolicy(
        workspace_id=profile.workspace_id,
        environment_id=profile.environment_id,
        profile_ref=profile.reference,
        retrieval_strategy=profile.retrieval_strategy,
        max_results=min(request.limit, profile.max_results),
        max_tokens=min(request.max_tokens or profile.max_tokens, profile.max_tokens),
        max_relation_hops=min(
            request.max_relation_hops
            if request.max_relation_hops is not None
            else profile.max_relation_hops,
            profile.max_relation_hops,
        ),
        freshness_max_age_seconds=min(
            request.freshness_max_age_seconds
            if request.freshness_max_age_seconds is not None
            else profile.freshness_max_age_seconds,
            profile.freshness_max_age_seconds,
        ),
        completeness_requirement=completeness,
        partial_answer_fallback=partial_fallback,
    )


ContextProfileResolver = ServerApprovedContextProfileResolver
ContextProfileResolverPort = ContextProfileResolutionPort
ContextProfileResolutionRequestV1 = ContextProfileResolutionRequest
ContextProfileResolutionV1 = ContextProfileResolution


__all__ = [
    "ContextProfileCapability",
    "ContextProfileCapabilityPolicy",
    "ContextProfileResolution",
    "ContextProfileResolutionOutcome",
    "ContextProfileResolutionPort",
    "ContextProfileResolutionReason",
    "ContextProfileResolutionRequest",
    "ContextProfileResolutionRequestV1",
    "ContextProfileResolutionV1",
    "DefaultContextProfileCapabilityPolicy",
    "EffectiveContextRetrievalPolicy",
    "ServerApprovedContextProfileResolver",
    "ContextProfileResolver",
    "ContextProfileResolverPort",
]
