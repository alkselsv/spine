from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.context_profiles import (
    ContextProfileResolutionOutcome,
    ContextProfileResolutionReason,
    ContextProfileResolutionRequest,
)
from spine.domain.context_profiles import (
    CompletenessRequirement,
    ContextProfileVersion,
    PartialAnswerFallback,
    RetrievalStrategy,
)
from spine.memory.broker import (
    ContextBrokerV1,
    ExplicitFakeContextBundleIssuer,
    FakeContextRetrieval,
    FakeTrustedContextValidator,
)
from spine.memory.contracts import ContextOutcome, ContextRequest
from spine.infrastructure.memory.context_profiles import (
    ContextProfileBrokerAdapter,
    DeterministicFakeContextProfileResolver,
)


WORKSPACE = UUID("00000000-0000-0000-0000-000000000101")
OTHER_WORKSPACE = UUID("00000000-0000-0000-0000-000000000102")
ENVIRONMENT = UUID("00000000-0000-0000-0000-000000000201")
OTHER_ENVIRONMENT = UUID("00000000-0000-0000-0000-000000000202")
PROFILE = UUID("00000000-0000-0000-0000-000000000301")
CREATED = datetime(2026, 10, 14, tzinfo=timezone.utc)


def profile(**overrides: object) -> ContextProfileVersion:
    values: dict[str, object] = {
        "profile_id": PROFILE,
        "workspace_id": WORKSPACE,
        "environment_id": ENVIRONMENT,
        "profile_version": 1,
        "retrieval_strategy": RetrievalStrategy.HYBRID,
        "max_results": 20,
        "max_tokens": 4000,
        "max_relation_hops": 2,
        "freshness_max_age_seconds": 3600,
        "completeness_requirement": CompletenessRequirement.BEST_EFFORT,
        "partial_answer_fallback": PartialAnswerFallback.DISALLOWED,
        "server_approved": True,
        "created_at": CREATED,
    }
    values.update(overrides)
    return ContextProfileVersion.create(**values)


def request(subject: ContextProfileVersion | None = None, **overrides: object) -> ContextProfileResolutionRequest:
    subject = subject or profile()
    values: dict[str, object] = {
        "workspace_id": subject.workspace_id,
        "environment_id": subject.environment_id,
        "profile_ref": subject.reference,
        "trusted_request_context_ref": UUID("00000000-0000-0000-0000-000000000401"),
        "authorization_context_ref": UUID("00000000-0000-0000-0000-000000000402"),
        "limit": 100,
    }
    values.update(overrides)
    return ContextProfileResolutionRequest(**values)


@pytest.mark.asyncio
async def test_resolver_returns_server_profile_and_bounded_policy() -> None:
    subject = profile(max_results=20, max_tokens=4000)
    resolver = DeterministicFakeContextProfileResolver({subject.reference: subject})

    result = await resolver.resolve(request(subject, limit=100, max_tokens=10000))

    assert result.outcome is ContextProfileResolutionOutcome.RESOLVED
    assert result.profile == subject
    assert result.policy is not None
    assert result.policy.max_results == 20
    assert result.policy.max_tokens == 4000
    assert result.policy.retrieval_strategy is RetrievalStrategy.HYBRID


@pytest.mark.asyncio
async def test_resolver_isolates_workspaces_and_environments() -> None:
    subject = profile()
    other = profile(
        workspace_id=OTHER_WORKSPACE,
        environment_id=OTHER_ENVIRONMENT,
        profile_id=UUID("00000000-0000-0000-0000-000000000302"),
    )
    resolver = DeterministicFakeContextProfileResolver(
        {subject.reference: subject, other.reference: other}
    )

    workspace_result = await resolver.resolve(request(other))
    environment_result = await resolver.resolve(
        request(subject, environment_id=OTHER_ENVIRONMENT)
    )

    assert workspace_result.profile == other
    assert environment_result.reason is ContextProfileResolutionReason.PROFILE_UNAVAILABLE
    assert environment_result.profile is None


@pytest.mark.asyncio
async def test_broker_uses_server_profile_resolution_adapter() -> None:
    subject = profile(max_results=4, max_tokens=2000)
    resolver = DeterministicFakeContextProfileResolver({subject.reference: subject})
    broker_request = ContextRequest(
        request_id=UUID("00000000-0000-0000-0000-000000000401"),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        query="trusted query",
        profile_ref=subject.reference,
        trusted_request_context_ref=UUID("00000000-0000-0000-0000-000000000402"),
        authorization_context_ref=UUID("00000000-0000-0000-0000-000000000403"),
        limit=50,
    )
    retrieval = FakeContextRetrieval(outcome=ContextOutcome.ALLOWED)
    issuer = ExplicitFakeContextBundleIssuer()
    broker = ContextBrokerV1(
        trusted_context_validator=FakeTrustedContextValidator(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
        ),
        profile_resolver=ContextProfileBrokerAdapter(resolver),
        retrieval=retrieval,
        bundle_issuer=issuer,
    )

    response = await broker.retrieve(broker_request)

    assert response.outcome is ContextOutcome.ALLOWED
    assert retrieval.requests[0] == broker_request
    assert issuer.issued


@pytest.mark.asyncio
async def test_resolver_requires_caller_capability_before_profile_lookup() -> None:
    subject = profile()
    resolver = DeterministicFakeContextProfileResolver(
        {subject.reference: subject}, capability_allowed=False
    )

    result = await resolver.resolve(request(subject))

    assert result.reason is ContextProfileResolutionReason.CAPABILITY_DENIED
    assert resolver.repository.requests == []


@pytest.mark.asyncio
async def test_resolver_requires_exact_version_and_digest() -> None:
    first = profile(profile_version=1)
    replacement = profile(
        profile_id=UUID("00000000-0000-0000-0000-000000000302"),
        profile_version=2,
        predecessor_profile_id=first.profile_id,
        max_results=5,
    )
    resolver = DeterministicFakeContextProfileResolver(
        {first.reference: first, replacement.reference: replacement}
    )

    old_result = await resolver.resolve(request(first))
    new_result = await resolver.resolve(request(replacement))
    bad_digest = await resolver.resolve(
        request(first, profile_ref=first.reference.model_copy(update={"profile_digest": "0" * 64}))
    )

    assert old_result.profile == first
    assert new_result.profile == replacement
    assert bad_digest.reason is ContextProfileResolutionReason.PROFILE_DIGEST_MISMATCH


@pytest.mark.asyncio
async def test_resolver_applies_partial_fallback_without_expanding_profile() -> None:
    subject = profile(partial_answer_fallback=PartialAnswerFallback.DISALLOWED)
    resolver = DeterministicFakeContextProfileResolver({subject.reference: subject})

    rejected = await resolver.resolve(request(subject, allow_partial_answer=True))
    narrowed = await resolver.resolve(request(subject, allow_partial_answer=False))

    assert rejected.reason is ContextProfileResolutionReason.INVALID_REQUEST_OVERRIDE
    assert narrowed.policy is not None
    assert narrowed.policy.partial_answer_fallback is PartialAnswerFallback.DISALLOWED


def test_resolution_request_rejects_client_selected_authority() -> None:
    subject = profile()
    for field_name in (
        "active_snapshot_id",
        "activation_generation",
        "canonical_boundary",
        "canonical_as_of",
        "physical_namespace",
        "partition_id",
        "projection_candidate",
        "access_policy",
    ):
        with pytest.raises(ValidationError):
            ContextProfileResolutionRequest.model_validate(
                {**request(subject).model_dump(), field_name: "caller-value"}
            )
