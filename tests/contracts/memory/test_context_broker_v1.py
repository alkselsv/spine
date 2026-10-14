from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.memory.broker import (
    DeterministicFakeContextBroker,
    ExplicitFakeContextBundleIssuer,
    FakeContextProfileResolver,
    FakeContextRetrieval,
    FakeTrustedContextValidator,
    LegacyContextBrokerAdapter,
)
from spine.memory.contracts import (
    ContextBundleRef,
    ContextOutcome,
    ContextProfile,
    ContextProfileRef,
    ContextReasonCode,
    ContextRequest,
    ContextResponse,
    RetrievalQuery,
)


WORKSPACE_ID = UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT_ID = UUID("20000000-0000-4000-8000-000000000001")
REQUEST_CONTEXT_REF = UUID("30000000-0000-4000-8000-000000000001")
AUTHORIZATION_CONTEXT_REF = UUID("40000000-0000-4000-8000-000000000001")
PROFILE_ID = UUID("50000000-0000-4000-8000-000000000001")


def profile_ref() -> ContextProfileRef:
    return ContextProfileRef(
        profile_id=PROFILE_ID,
        profile_version=1,
        profile_digest="a" * 64,
    )


def request() -> ContextRequest:
    return ContextRequest(
        request_id=UUID("60000000-0000-4000-8000-000000000001"),
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        query="What is the answer?",
        profile_ref=profile_ref(),
        trusted_request_context_ref=REQUEST_CONTEXT_REF,
        authorization_context_ref=AUTHORIZATION_CONTEXT_REF,
        limit=5,
    )


def profile() -> ContextProfile:
    return ContextProfile(ref=profile_ref(), retrieval_strategy="hybrid")


def broker(
    *,
    trusted: bool = True,
    result: ContextOutcome = ContextOutcome.ALLOWED,
    profile_available: bool = True,
    validator_error: Exception | None = None,
    profile_error: Exception | None = None,
    retrieval_error: Exception | None = None,
    issuer_error: Exception | None = None,
) -> tuple[DeterministicFakeContextBroker, ExplicitFakeContextBundleIssuer]:
    resolver = FakeContextProfileResolver(
        {profile_ref(): profile()} if profile_available else {},
        error=profile_error,
    )
    issuer = ExplicitFakeContextBundleIssuer(error=issuer_error)
    return (
        DeterministicFakeContextBroker(
            trusted_context_validator=FakeTrustedContextValidator(
                allowed=trusted,
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
                error=validator_error,
            ),
            profile_resolver=resolver,
            retrieval=FakeContextRetrieval(outcome=result, error=retrieval_error),
            bundle_issuer=issuer,
        ),
        issuer,
    )


def test_context_profile_ref_has_only_its_public_identity_fields() -> None:
    assert set(ContextProfileRef.model_fields) == {
        "profile_id",
        "profile_version",
        "profile_digest",
    }
    assert ContextProfileRef.model_json_schema()["$id"] == "spine/context-profile-ref:v1"
    assert ContextBundleRef.model_json_schema()["$id"] == "spine/context-bundle-ref:v1"
    with pytest.raises(ValidationError):
        ContextProfileRef(
            profile_id=PROFILE_ID,
            profile_version=1,
            profile_digest="a" * 64,
            workspace_id=WORKSPACE_ID,
        )
    with pytest.raises(ValidationError):
        profile_ref().profile_version = 2


def test_context_request_rejects_client_selected_projection_authority() -> None:
    for field_name in (
        "active_snapshot_id",
        "activation_generation",
        "canonical_boundary",
        "canonical_as_of",
        "physical_namespace",
        "projection_candidate",
        "partition_id",
        "access_policy",
    ):
        with pytest.raises(ValidationError):
            ContextRequest.model_validate({**request().model_dump(), field_name: "caller-value"})


@pytest.mark.asyncio
async def test_fake_broker_issues_an_explicit_fake_bundle_for_allowed_retrieval() -> None:
    context_broker, issuer = broker()

    response = await context_broker.retrieve(request())

    assert response.outcome is ContextOutcome.ALLOWED
    assert response.reason_code is ContextReasonCode.ALLOWED
    assert response.context_bundle_ref is not None
    assert issuer.production_authorized is False
    assert "protected" not in response.model_dump_json()


@pytest.mark.asyncio
async def test_invalid_trusted_context_is_denied_without_issuing_a_bundle() -> None:
    context_broker, issuer = broker(trusted=False)

    response = await context_broker.retrieve(request())

    assert response.outcome is ContextOutcome.DENIED
    assert response.reason_code is ContextReasonCode.INVALID_TRUSTED_CONTEXT
    assert response.context_bundle_ref is None
    assert issuer.issued == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "reason"),
    (
        (ContextOutcome.UNAVAILABLE, ContextReasonCode.RETRIEVAL_UNAVAILABLE),
        (ContextOutcome.STALE, ContextReasonCode.STALE_CONTEXT),
        (ContextOutcome.DEGRADED, ContextReasonCode.DEGRADED_CONTEXT),
        (ContextOutcome.PARTIAL, ContextReasonCode.PARTIAL_CONTEXT),
        (ContextOutcome.CANCELLED, ContextReasonCode.CANCELLED),
        (ContextOutcome.INSUFFICIENT, ContextReasonCode.INSUFFICIENT_EVIDENCE),
        (ContextOutcome.ABSTAINED, ContextReasonCode.INSUFFICIENT_EVIDENCE),
    ),
)
async def test_fake_broker_maps_controlled_retrieval_outcomes_safely(
    outcome: ContextOutcome,
    reason: ContextReasonCode,
) -> None:
    context_broker, _ = broker(result=outcome)

    response = await context_broker.retrieve(request())

    assert response.outcome is outcome
    assert response.reason_code is reason
    if outcome in {ContextOutcome.DEGRADED, ContextOutcome.PARTIAL}:
        assert response.context_bundle_ref is not None
    else:
        assert response.context_bundle_ref is None
    assert "protected" not in response.model_dump_json()


@pytest.mark.asyncio
async def test_missing_server_profile_is_unavailable_without_provider_details() -> None:
    context_broker, issuer = broker(profile_available=False)

    response = await context_broker.retrieve(request())

    assert response.outcome is ContextOutcome.UNAVAILABLE
    assert response.reason_code is ContextReasonCode.PROFILE_UNAVAILABLE
    assert response.context_bundle_ref is None
    assert issuer.issued == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_option",
    ("validator_error", "profile_error", "retrieval_error", "issuer_error"),
)
async def test_port_errors_map_to_safe_typed_responses(error_option: str) -> None:
    context_broker, issuer = broker(**{error_option: RuntimeError("protected provider detail")})

    response = await context_broker.retrieve(request())

    assert response.outcome in {ContextOutcome.UNAVAILABLE, ContextOutcome.DENIED}
    assert response.context_bundle_ref is None
    assert "protected provider detail" not in response.model_dump_json()
    if error_option != "issuer_error":
        assert issuer.issued == []


@pytest.mark.asyncio
async def test_fake_broker_keeps_two_workspace_requests_isolated() -> None:
    context_broker, _ = broker()
    other_request = request().model_copy(
        update={
            "request_id": UUID("60000000-0000-4000-8000-000000000002"),
            "workspace_id": UUID("10000000-0000-4000-8000-000000000002"),
        }
    )

    first, second = await context_broker.retrieve(request()), await context_broker.retrieve(other_request)

    assert first.outcome is ContextOutcome.ALLOWED
    assert first.context_bundle_ref is not None
    assert second.outcome is ContextOutcome.DENIED
    assert second.context_bundle_ref is None
    assert "protected" not in second.model_dump_json()


@pytest.mark.asyncio
async def test_legacy_broker_adapter_preserves_retrieval_result_shape() -> None:
    context_broker, _ = broker()
    legacy = LegacyContextBrokerAdapter(context_broker)

    result = await legacy.retrieve(
        RetrievalQuery(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            query="What is the answer?",
            requested_by={"kind": "human", "id": REQUEST_CONTEXT_REF},
            context_profile="profile",
        )
    )

    assert result.context == ()
    assert result.retrieval_strategy == "context-broker:v1"
    assert result.index_version == "context-broker:v1"
    assert result.outcome is ContextOutcome.UNAVAILABLE


def test_context_response_rejects_bundle_on_denied_outcome() -> None:
    with pytest.raises(ValidationError):
        ContextResponse(
            outcome=ContextOutcome.DENIED,
            reason_code=ContextReasonCode.AUTHORIZATION_DENIED,
            context_bundle_ref=ContextBundleRef(
                bundle_id=UUID("80000000-0000-4000-8000-000000000001"),
                bundle_version=1,
                bundle_digest="b" * 64,
            ),
        )
