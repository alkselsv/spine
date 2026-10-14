from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.projections import (
    CanonicalAsOf,
    ProjectionActivation,
    ProjectionActivationKind,
    ProjectionConfigVersion,
    ProjectionFreshness,
    ProjectionHealth,
    ProjectionSnapshot,
    ProjectionSnapshotState,
    PublishedActivePublication,
    UnavailableActivePublication,
    UnpublishedActivePublication,
)
from spine.memory.broker import (
    ContextBrokerV1,
    DeterministicFakePublicationResolver,
    ExplicitFakeContextBundleIssuer,
    FakeContextProfileResolver,
    FakeContextRetrieval,
    FakeTrustedContextValidator,
)
from spine.memory.contracts import (
    ContextOutcome,
    ContextProfile,
    ContextProfileRef,
    ContextRequest,
    PublicationPin,
)


NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
WORKSPACE = UUID(int=1)
ENVIRONMENT = UUID(int=2)
PROFILE = UUID(int=3)


def _profile(*, current: bool = False, healthy: bool = False) -> ContextProfile:
    return ContextProfile(
        ref=ContextProfileRef(profile_id=PROFILE, profile_version=1, profile_digest="a" * 64),
        retrieval_strategy="hybrid",
        projection_kind="qa",
        require_current_publication=current,
        require_healthy_publication=healthy,
    )


def _request(profile: ContextProfile) -> ContextRequest:
    return ContextRequest(
        request_id=UUID(int=10),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        query="question",
        profile_ref=profile.ref,
        trusted_request_context_ref=UUID(int=4),
        authorization_context_ref=UUID(int=5),
    )


def _config() -> ProjectionConfigVersion:
    digest = sha256(
        json.dumps(
            {"schema": "projection-config:r1", "payload": {}},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return ProjectionConfigVersion(
        projection_config_version_id=UUID(int=11),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        version=1,
        ontology_version_id=UUID(int=12),
        parser_contract_version="parser:v1",
        locator_contract_version="locator:v1",
        extraction_contract_version="extractor:v1",
        embedding_profile_version="embedding:v1",
        adapter_contract_version="adapter:v1",
        config_payload={},
        config_digest=digest,
        created_at=NOW,
    )


def _publication(
    *,
    snapshot_id: UUID = UUID(int=13),
    activation_id: UUID = UUID(int=14),
    generation: int = 1,
    freshness: ProjectionFreshness = ProjectionFreshness.CURRENT,
    health: ProjectionHealth = ProjectionHealth.HEALTHY,
) -> PublishedActivePublication:
    snapshot = ProjectionSnapshot(
        projection_snapshot_id=snapshot_id,
        projection_run_id=UUID(int=15),
        projection_candidate_id=UUID(int=16),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        snapshot_state=ProjectionSnapshotState.FINALIZED,
        projection_kind="qa",
        projection_config_version_id=UUID(int=11),
        canonical_boundary_id=UUID(int=17),
        candidate_evidence_digest="b" * 64,
        manifest_digest="c" * 64,
        member_count=0,
        created_at=NOW,
    )
    activation = ProjectionActivation(
        projection_activation_id=activation_id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        projection_snapshot_id=snapshot_id,
        activation_generation=generation,
        activation_kind=ProjectionActivationKind.PUBLISH,
        command_identity="activation-1",
        command_digest="d" * 64,
        idempotency_receipt_id=UUID(int=18),
        audit_event_id=UUID(int=19),
        activated_at=NOW,
    )
    return PublishedActivePublication(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        projection_snapshot=snapshot,
        projection_activation=activation,
        activation_generation=generation,
        active_pointer_cas_version=generation,
        canonical_boundary_id=UUID(int=17),
        canonical_boundary_manifest_digest="e" * 64,
        accepted_through_generation=2,
        canonical_boundary_membership_ref="members",
        as_of=CanonicalAsOf(
            canonical_boundary_id=UUID(int=17),
            accepted_through_generation=2,
            boundary_time=NOW,
        ),
        projection_config_version=_config(),
        freshness=freshness,
        health=health,
    )


def _broker(
    resolution: object,
    *,
    profile: ContextProfile | None = None,
) -> tuple[ContextBrokerV1, DeterministicFakePublicationResolver, FakeContextRetrieval, ExplicitFakeContextBundleIssuer]:
    profile = profile or _profile()
    resolver = FakeContextProfileResolver({profile.ref: profile})
    publication_resolver = DeterministicFakePublicationResolver(resolution)  # type: ignore[arg-type]
    retrieval = FakeContextRetrieval()
    issuer = ExplicitFakeContextBundleIssuer()
    return (
        ContextBrokerV1(
            trusted_context_validator=FakeTrustedContextValidator(
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
            ),
            profile_resolver=resolver,
            retrieval=retrieval,
            bundle_issuer=issuer,
            publication_resolver=publication_resolver,
        ),
        publication_resolver,
        retrieval,
        issuer,
    )


@pytest.mark.asyncio
async def test_broker_resolves_one_immutable_pin_and_passes_same_identity_downstream() -> None:
    publication = _publication()
    broker, resolver, retrieval, issuer = _broker(publication)

    response = await broker.retrieve(_request(_profile()))

    assert response.outcome is ContextOutcome.ALLOWED
    assert len(resolver.requests) == 1
    assert len(retrieval.publication_pins) == 1
    assert retrieval.publication_pins[0] is issuer.publication_pins[0]
    assert response.publication_pin_id == retrieval.publication_pins[0].pin_id
    assert issuer.issued[0].publication_pin_id == response.publication_pin_id
    assert retrieval.publication_pins[0].projection_snapshot == publication.projection_snapshot


@pytest.mark.asyncio
async def test_pinned_attempt_does_not_change_when_resolver_state_changes() -> None:
    broker, resolver, retrieval, _ = _broker(_publication(generation=1))

    await broker.retrieve(_request(_profile()))
    original_pin = retrieval.publication_pins[0]
    resolver.resolution = _publication(snapshot_id=UUID(int=20), generation=2)

    assert original_pin is not None
    assert original_pin.activation_generation == 1
    assert original_pin.projection_snapshot.projection_snapshot_id == UUID(int=13)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resolution", "expected"),
    (
        (
            UnpublishedActivePublication(
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                projection_kind="qa",
            ),
            ContextOutcome.UNAVAILABLE,
        ),
        (
            UnavailableActivePublication(
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                projection_kind="qa",
                reason_code="integrity_conflict",
            ),
            ContextOutcome.UNAVAILABLE,
        ),
    ),
)
async def test_unavailable_publication_fails_closed_without_downstream_reads(
    resolution: object, expected: ContextOutcome
) -> None:
    broker, _, retrieval, issuer = _broker(resolution)

    response = await broker.retrieve(_request(_profile()))

    assert response.outcome is expected
    assert retrieval.publication_pins == []
    assert issuer.issued == []


@pytest.mark.asyncio
async def test_profile_requiring_current_publication_abstains_on_stale_pin() -> None:
    broker, _, retrieval, _ = _broker(
        _publication(freshness=ProjectionFreshness.STALE),
        profile=_profile(current=True),
    )

    response = await broker.retrieve(_request(_profile(current=True)))

    assert response.outcome is ContextOutcome.STALE
    assert retrieval.publication_pins == []
    assert response.publication_pin_id is not None


@pytest.mark.asyncio
async def test_profile_requiring_healthy_publication_returns_degraded_without_bundle() -> None:
    broker, _, retrieval, issuer = _broker(
        _publication(health=ProjectionHealth.DEGRADED),
        profile=_profile(healthy=True),
    )

    response = await broker.retrieve(_request(_profile(healthy=True)))

    assert response.outcome is ContextOutcome.DEGRADED
    assert response.context_bundle_ref is None
    assert retrieval.publication_pins == []
    assert issuer.issued == []


def test_publication_pin_rejects_caller_controlled_physical_namespace() -> None:
    pin = PublicationPin.from_publication(_publication())

    with pytest.raises(ValidationError):
        PublicationPin.model_validate({**pin.model_dump(), "physical_namespace": "provider"})
