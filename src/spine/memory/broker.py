"""Provider-neutral Context Broker v1 composition and deterministic fakes.

The v1 broker owns the small composition seam between trusted request context,
server-approved profiles, retrieval and bundle issuance.  The implementations
in this module are deliberately fake-backed: a fake bundle reference is useful
for contract tests, but is never evidence of production authorization.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from spine.domain.projections import (
    PublishedActivePublication,
    UnavailableActivePublication,
    UnpublishedActivePublication,
)

from .contracts import (
    ContextBundleRef,
    ContextOutcome,
    ContextProfile,
    ContextProfileRef,
    ContextReasonCode,
    ContextRequest,
    ContextResponse,
    ContextRetrievalResult,
    PublicationPin,
    RetrievalQuery,
    RetrievalResult,
    context_reason_for_outcome,
)


class TrustedContextValidator(Protocol):
    """Validate that request and authorization references are trusted."""

    async def validate(self, request: ContextRequest) -> bool: ...


class ContextProfileResolver(Protocol):
    """Resolve only after trusted request context has been validated."""

    async def resolve_for_request(self, request: ContextRequest) -> ContextProfile | None: ...


class ActivePublicationResolver(Protocol):
    """Read the PostgreSQL-owned publication authority exactly once per attempt."""

    async def resolve_active_publication(
        self, projection_kind: str
    ) -> PublishedActivePublication | UnpublishedActivePublication | UnavailableActivePublication: ...


class ContextRetrievalPort(Protocol):
    """Return a typed, content-free retrieval disposition."""

    async def retrieve(
        self,
        request: ContextRequest,
        profile: ContextProfile,
        publication_pin: PublicationPin | None = None,
    ) -> ContextRetrievalResult: ...


class ContextBundleIssuer(Protocol):
    """Issue the exact bundle reference handed to the caller's run."""

    async def issue(
        self,
        request: ContextRequest,
        profile: ContextProfile,
        retrieval: ContextRetrievalResult,
        publication_pin: PublicationPin | None = None,
    ) -> ContextBundleRef: ...


class ContextBrokerV1:
    """Compose the v1 request seam with fail-closed safe outcome mapping."""

    def __init__(
        self,
        *,
        trusted_context_validator: TrustedContextValidator,
        profile_resolver: ContextProfileResolver,
        retrieval: ContextRetrievalPort,
        bundle_issuer: ContextBundleIssuer,
        publication_resolver: ActivePublicationResolver | None = None,
    ) -> None:
        self._trusted_context_validator = trusted_context_validator
        self._profile_resolver = profile_resolver
        self._retrieval = retrieval
        self._bundle_issuer = bundle_issuer
        self._publication_resolver = publication_resolver

    async def retrieve(self, request: ContextRequest) -> ContextResponse:
        """Return a typed v1 response without exposing protected payloads."""

        try:
            trusted = await self._trusted_context_validator.validate(request)
        except Exception:
            trusted = False
        if not trusted:
            return ContextResponse(
                outcome=ContextOutcome.DENIED,
                reason_code=ContextReasonCode.INVALID_TRUSTED_CONTEXT,
            )

        try:
            profile = await self._profile_resolver.resolve_for_request(request)
        except Exception:
            profile = None
        if profile is None or profile.ref != request.profile_ref:
            return ContextResponse(
                outcome=ContextOutcome.UNAVAILABLE,
                reason_code=ContextReasonCode.PROFILE_UNAVAILABLE,
            )

        publication_pin: PublicationPin | None = None
        if self._publication_resolver is not None:
            try:
                publication = await self._publication_resolver.resolve_active_publication(
                    profile.projection_kind
                )
            except Exception:
                return ContextResponse(
                    outcome=ContextOutcome.UNAVAILABLE,
                    reason_code=ContextReasonCode.PUBLICATION_UNAVAILABLE,
                )
            if isinstance(publication, UnpublishedActivePublication | UnavailableActivePublication):
                return ContextResponse(
                    outcome=ContextOutcome.UNAVAILABLE,
                    reason_code=ContextReasonCode.PUBLICATION_UNAVAILABLE,
                )
            if (
                publication.workspace_id != request.workspace_id
                or publication.environment_id != request.environment_id
                or publication.projection_kind != profile.projection_kind
            ):
                return ContextResponse(
                    outcome=ContextOutcome.UNAVAILABLE,
                    reason_code=ContextReasonCode.PUBLICATION_UNAVAILABLE,
                )
            try:
                publication_pin = PublicationPin.from_publication(publication)
            except Exception:
                return ContextResponse(
                    outcome=ContextOutcome.UNAVAILABLE,
                    reason_code=ContextReasonCode.PUBLICATION_UNAVAILABLE,
                )
            if profile.require_current_publication and publication_pin.freshness.value == "stale":
                return ContextResponse(
                    outcome=ContextOutcome.STALE,
                    reason_code=ContextReasonCode.STALE_CONTEXT,
                    publication_pin_id=publication_pin.pin_id,
                )
            if profile.require_healthy_publication and publication_pin.health.value == "degraded":
                return ContextResponse(
                    outcome=ContextOutcome.DEGRADED,
                    reason_code=ContextReasonCode.DEGRADED_CONTEXT,
                    publication_pin_id=publication_pin.pin_id,
                )

        try:
            if publication_pin is None:
                retrieval = await self._retrieval.retrieve(request, profile)
            else:
                retrieval = await self._retrieval.retrieve(request, profile, publication_pin)
        except Exception:
            return ContextResponse(
                outcome=ContextOutcome.UNAVAILABLE,
                reason_code=ContextReasonCode.RETRIEVAL_UNAVAILABLE,
            )
        if retrieval.outcome not in {
            ContextOutcome.ALLOWED,
            ContextOutcome.DEGRADED,
            ContextOutcome.PARTIAL,
        }:
            return ContextResponse(
                outcome=retrieval.outcome,
                reason_code=retrieval.reason_code,
                diagnostic=retrieval.diagnostic,
                publication_pin_id=publication_pin.pin_id if publication_pin else None,
            )

        try:
            if publication_pin is None:
                bundle_ref = await self._bundle_issuer.issue(request, profile, retrieval)
            else:
                bundle_ref = await self._bundle_issuer.issue(
                    request, profile, retrieval, publication_pin
                )
        except Exception:
            return ContextResponse(
                outcome=ContextOutcome.UNAVAILABLE,
                reason_code=ContextReasonCode.BUNDLE_UNAVAILABLE,
            )
        return ContextResponse(
            outcome=retrieval.outcome,
            context_bundle_ref=bundle_ref,
            reason_code=retrieval.reason_code,
            diagnostic=retrieval.diagnostic,
            publication_pin_id=publication_pin.pin_id if publication_pin else None,
        )


class DeterministicFakeContextBroker(ContextBrokerV1):
    """Explicit fake-backed v1 broker used by offline contract tests."""


class FakeTrustedContextValidator:
    """Deterministic trusted-context port with no authorization authority."""

    def __init__(
        self,
        *,
        allowed: bool = True,
        workspace_id: UUID | None = None,
        environment_id: UUID | None = None,
        error: Exception | None = None,
    ) -> None:
        self.allowed = allowed
        self.workspace_id = workspace_id
        self.environment_id = environment_id
        self.error = error
        self.requests: list[ContextRequest] = []

    async def validate(self, request: ContextRequest) -> bool:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.allowed and (
            self.workspace_id is None or request.workspace_id == self.workspace_id
        ) and (self.environment_id is None or request.environment_id == self.environment_id)


class FakeContextProfileResolver:
    """In-memory server-owned profile directory for the tracer bullet."""

    def __init__(
        self,
        profiles: dict[ContextProfileRef, ContextProfile],
        *,
        error: Exception | None = None,
    ) -> None:
        self.profiles = dict(profiles)
        self.error = error
        self.requests: list[ContextProfileRef] = []

    async def resolve(self, profile_ref: ContextProfileRef) -> ContextProfile | None:
        self.requests.append(profile_ref)
        if self.error is not None:
            raise self.error
        return self.profiles.get(profile_ref)

    async def resolve_for_request(self, request: ContextRequest) -> ContextProfile | None:
        return await self.resolve(request.profile_ref)


class DeterministicFakePublicationResolver:
    """Offline publication resolver with explicit, immutable test state."""

    def __init__(
        self,
        resolution: PublishedActivePublication | UnpublishedActivePublication | UnavailableActivePublication,
    ) -> None:
        self.resolution = resolution
        self.requests: list[str] = []

    async def resolve_active_publication(
        self, projection_kind: str
    ) -> PublishedActivePublication | UnpublishedActivePublication | UnavailableActivePublication:
        self.requests.append(projection_kind)
        return self.resolution


FakeProjectionRepository = DeterministicFakePublicationResolver
FakeActivePublicationResolver = DeterministicFakePublicationResolver


class FakeContextRetrieval:
    """Controlled fake result; it never claims to retrieve production evidence."""

    def __init__(
        self,
        *,
        outcome: ContextOutcome = ContextOutcome.ALLOWED,
        error: Exception | None = None,
    ) -> None:
        self.outcome = outcome
        self.error = error
        self.requests: list[ContextRequest] = []
        self.publication_pins: list[PublicationPin | None] = []

    async def retrieve(
        self,
        request: ContextRequest,
        profile: ContextProfile,
        publication_pin: PublicationPin | None = None,
    ) -> ContextRetrievalResult:
        self.requests.append(request)
        self.publication_pins.append(publication_pin)
        del profile
        if self.error is not None:
            raise self.error
        return ContextRetrievalResult(
            outcome=self.outcome,
            reason_code=context_reason_for_outcome(self.outcome),
            publication_pin_id=publication_pin.pin_id if publication_pin else None,
        )


class ExplicitFakeContextBundleIssuer:
    """Deterministic bundle issuer marked explicitly as non-production."""

    production_authorized = False

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.issued: list[ContextBundleRef] = []
        self.publication_pins: list[PublicationPin | None] = []

    async def issue(
        self,
        request: ContextRequest,
        profile: ContextProfile,
        retrieval: ContextRetrievalResult,
        publication_pin: PublicationPin | None = None,
    ) -> ContextBundleRef:
        if self.error is not None:
            raise self.error
        if retrieval.outcome not in {
            ContextOutcome.ALLOWED,
            ContextOutcome.DEGRADED,
            ContextOutcome.PARTIAL,
        }:
            raise ValueError("fake issuer cannot issue a non-bundle outcome")
        pin_identity = str(publication_pin.pin_id) if publication_pin else "legacy"
        identity = (
            f"{request.request_id}:{profile.ref.profile_id}:{profile.ref.profile_version}:"
            f"{pin_identity}"
        )
        bundle_id = uuid5(NAMESPACE_URL, f"spine:fake-context-bundle:{identity}")
        bundle_digest = sha256(identity.encode("utf-8")).hexdigest()
        bundle_ref = ContextBundleRef(
            bundle_id=bundle_id,
            bundle_version=1,
            bundle_digest=bundle_digest,
            publication_pin_id=publication_pin.pin_id if publication_pin else None,
        )
        self.issued.append(bundle_ref)
        self.publication_pins.append(publication_pin)
        return bundle_ref


class LegacyContextBrokerAdapter:
    """Adapt v1 dispositions to the existing legacy RetrievalResult surface."""

    def __init__(self, broker: ContextBrokerV1) -> None:
        self._broker = broker

    async def retrieve(self, query: RetrievalQuery) -> RetrievalResult:
        profile_id = _legacy_profile_id(query.context_profile)
        request = ContextRequest(
            request_id=uuid5(
                NAMESPACE_URL,
                f"spine:legacy-context-request:{query.workspace_id}:{query.environment_id}:{query.query}",
            ),
            workspace_id=query.workspace_id,
            environment_id=query.environment_id,
            query=query.query,
            profile_ref=ContextProfileRef(
                profile_id=profile_id,
                profile_version=1,
                profile_digest=sha256(query.context_profile.encode("utf-8")).hexdigest(),
            ),
            trusted_request_context_ref=query.requested_by.id,
            authorization_context_ref=uuid5(
                NAMESPACE_URL,
                f"spine:legacy-authorization:{query.requested_by.id}",
            ),
            limit=query.limit,
        )
        response = await self._broker.retrieve(request)
        return RetrievalResult(
            retrieval_strategy="context-broker:v1",
            index_version="context-broker:v1",
            outcome=response.outcome,
        )


def _legacy_profile_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        return uuid5(NAMESPACE_URL, f"spine:legacy-profile:{value}")


ContextRequestV1 = ContextRequest
ContextResponseV1 = ContextResponse
ContextProfileRefV1 = ContextProfileRef
ContextBundleRefV1 = ContextBundleRef


__all__ = [
    "ContextBrokerV1",
    "ActivePublicationResolver",
    "ContextBundleIssuer",
    "ContextProfileResolver",
    "ContextRetrievalPort",
    "ContextRequestV1",
    "ContextResponseV1",
    "ContextProfileRefV1",
    "ContextBundleRefV1",
    "DeterministicFakeContextBroker",
    "ExplicitFakeContextBundleIssuer",
    "FakeContextProfileResolver",
    "FakeContextRetrieval",
    "FakeTrustedContextValidator",
    "DeterministicFakePublicationResolver",
    "FakeActivePublicationResolver",
    "FakeProjectionRepository",
    "LegacyContextBrokerAdapter",
    "TrustedContextValidator",
]
