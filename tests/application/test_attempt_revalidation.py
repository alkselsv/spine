from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from spine.application.attempt_revalidation import (
    AttemptRevalidationService,
    InMemoryRevalidationResultRepository,
)
from spine.domain.context_bundles import (
    ContextBundleOutcome,
    RevalidationCheckpoint,
    RevalidationDisposition,
    RevalidationEvidenceObservation,
    RevalidationFailureCode,
    RevalidationObservation,
    RevalidationAvailability,
    RevalidationPublicationImpact,
    RevalidationPublicationObservation,
    RevalidationRequest,
    RevalidationSourceObservation,
)

from tests.domain.context_bundle.test_context_bundle_models import (
    ATTEMPT,
    ENVIRONMENT,
    SNAPSHOT,
    WORKSPACE,
    context_bundle,
)


SOURCE = UUID(int=20)
REVISION = UUID(int=21)
POLICY = UUID(int=23)


def request(**overrides: object) -> RevalidationRequest:
    values: dict[str, object] = {
        "bundle": context_bundle(),
        "workspace_id": WORKSPACE,
        "environment_id": ENVIRONMENT,
        "checkpoint": RevalidationCheckpoint.BEFORE_EVIDENCE_REUSE,
        "idempotency_key": "revalidate-1",
    }
    values.update(overrides)
    return RevalidationRequest(**values)


def observation(**overrides: object) -> RevalidationObservation:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE,
        "environment_id": ENVIRONMENT,
        "publication": RevalidationPublicationObservation(
            pinned_snapshot_id=SNAPSHOT,
            pinned_activation_generation=1,
            current_snapshot_id=SNAPSHOT,
            current_activation_generation=1,
        ),
        "source_states": (
            RevalidationSourceObservation(
                **source_observation(),
            ),
        ),
        "evidence_states": (
            RevalidationEvidenceObservation(
                **evidence_observation(),
            ),
        ),
    }
    values.update(overrides)
    return RevalidationObservation(**values)


def source_observation(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "source_object_id": SOURCE,
        "current_policy_version_id": POLICY,
        "current_policy_generation": 1,
        "membership_generation": 1,
        "authority_generation": 1,
        "service_authority_generation": 1,
        "current_revision_id": REVISION,
        "current_revision_digest": "6" * 64,
    }
    values.update(overrides)
    return values


def evidence_observation(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "evidence_id": "evidence-1",
        "source_object_id": SOURCE,
        "workspace_id": WORKSPACE,
        "environment_id": ENVIRONMENT,
        "projection_snapshot_id": SNAPSHOT,
        "activation_generation": 1,
        "source_revision_id": REVISION,
        "revision_digest": "6" * 64,
        "revision_number": None,
        "processing_receipt_id": UUID(int=22),
        "locator_contract_version": "locator:v1",
        "locator": {"kind": "page", "value": "1"},
        "evidence_digest": "7" * 64,
    }
    values.update(overrides)
    return values


class FakeStateProvider:
    def __init__(self, state: RevalidationObservation) -> None:
        self.state = state
        self.calls = 0

    async def resolve(self, _request: RevalidationRequest) -> RevalidationObservation:
        self.calls += 1
        return self.state


@pytest.mark.asyncio
async def test_activation_change_alone_replays_the_pinned_bundle() -> None:
    state = observation(
        publication=RevalidationPublicationObservation(
            pinned_snapshot_id=SNAPSHOT,
            pinned_activation_generation=1,
            current_snapshot_id=UUID(int=90),
            current_activation_generation=2,
            impact=RevalidationPublicationImpact.ACTIVATION_ONLY,
        )
    )
    provider = FakeStateProvider(state)
    service = AttemptRevalidationService(
        state_provider=provider,
        result_repository=InMemoryRevalidationResultRepository(),
    )

    result = await service.revalidate(request())

    assert result.disposition is RevalidationDisposition.SAFE_REPLAY
    assert result.safe_to_reuse_bundle
    assert result.activation_changed
    assert result.active_projection_changed
    assert result.bundle_reference == request().bundle.reference


@pytest.mark.asyncio
async def test_policy_revocation_rejects_reuse_without_a_regeneration_plan() -> None:
    provider = FakeStateProvider(
        observation(
            source_states=(
                RevalidationSourceObservation(
                    **source_observation(
                        current_policy_generation=2,
                        contributor_authorized=False,
                    )
                ),
            )
        )
    )
    service = AttemptRevalidationService(
        state_provider=provider,
        result_repository=InMemoryRevalidationResultRepository(),
    )

    result = await service.revalidate(request())

    assert result.disposition is RevalidationDisposition.REJECTED
    assert RevalidationFailureCode.CONTRIBUTOR_UNAUTHORIZED in result.failure_codes
    assert result.regeneration is None
    assert result.bundle_reference is None


@pytest.mark.asyncio
async def test_source_replacement_creates_a_new_attempt_and_pin() -> None:
    provider = FakeStateProvider(
        observation(
            source_states=(
                RevalidationSourceObservation(
                    **source_observation(
                        current_revision_id=UUID(int=25),
                        current_revision_digest="8" * 64,
                    )
                ),
            )
        )
    )
    service = AttemptRevalidationService(
        state_provider=provider,
        result_repository=InMemoryRevalidationResultRepository(),
    )

    result = await service.revalidate(request())

    assert result.disposition is RevalidationDisposition.SAFE_REGENERATION
    assert result.regeneration is not None
    assert result.regeneration.prior_attempt_id == ATTEMPT
    assert result.regeneration.new_attempt_id != ATTEMPT
    assert result.regeneration.new_publication_pin_id.int != 0
    assert result.regeneration.prior_bundle == request().bundle.reference


@pytest.mark.asyncio
async def test_regeneration_budget_exhaustion_fails_closed() -> None:
    provider = FakeStateProvider(
        observation(
            source_states=(
                RevalidationSourceObservation(
                    **source_observation(
                        current_revision_id=UUID(int=25),
                        current_revision_digest="8" * 64,
                    )
                ),
            )
        )
    )
    service = AttemptRevalidationService(
        state_provider=provider,
        result_repository=InMemoryRevalidationResultRepository(),
    )

    result = await service.revalidate(request(regeneration_count=1, regeneration_budget=1))

    assert result.disposition is RevalidationDisposition.REJECTED
    assert RevalidationFailureCode.REGENERATION_LIMIT in result.failure_codes
    assert result.regeneration is None


@pytest.mark.asyncio
async def test_cancelled_request_has_no_protected_bundle_result() -> None:
    provider = FakeStateProvider(observation())
    service = AttemptRevalidationService(
        state_provider=provider,
        result_repository=InMemoryRevalidationResultRepository(),
    )

    result = await service.revalidate(request(cancelled=True))

    assert result.disposition is RevalidationDisposition.CANCELLED
    assert result.bundle_reference is None
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_partial_bundle_replay_keeps_a_typed_partial_outcome_without_evidence() -> None:
    provider = FakeStateProvider(observation())
    service = AttemptRevalidationService(
        state_provider=provider,
        result_repository=InMemoryRevalidationResultRepository(),
    )

    result = await service.revalidate(
        request(bundle=context_bundle(outcome="partial", state="partial"))
    )

    assert result.disposition is RevalidationDisposition.SAFE_REPLAY
    assert result.bundle_outcome is ContextBundleOutcome.PARTIAL
    assert result.bundle_reference is not None
    assert not hasattr(result, "evidence")


@pytest.mark.asyncio
async def test_tombstoned_source_fails_closed_even_when_the_old_evidence_is_present() -> None:
    provider = FakeStateProvider(
        observation(
            source_states=(
                RevalidationSourceObservation(
                    **source_observation(tombstoned=True)
                ),
            )
        )
    )
    service = AttemptRevalidationService(
        state_provider=provider,
        result_repository=InMemoryRevalidationResultRepository(),
    )

    result = await service.revalidate(request())

    assert result.disposition is RevalidationDisposition.REJECTED
    assert RevalidationFailureCode.SOURCE_TOMBSTONED in result.failure_codes
    assert result.bundle_reference is None


@pytest.mark.asyncio
async def test_corrupt_evidence_is_bounded_to_a_new_attempt() -> None:
    provider = FakeStateProvider(
        observation(
            evidence_states=(
                RevalidationEvidenceObservation(
                    **evidence_observation(digest_matches=False),
                ),
            )
        )
    )
    service = AttemptRevalidationService(
        state_provider=provider,
        result_repository=InMemoryRevalidationResultRepository(),
    )

    result = await service.revalidate(request())

    assert result.disposition is RevalidationDisposition.SAFE_REGENERATION
    assert RevalidationFailureCode.EVIDENCE_CORRUPT in result.failure_codes
    assert result.regeneration is not None
    assert result.regeneration.prior_bundle.bundle_digest == request().bundle.bundle_digest


@pytest.mark.asyncio
async def test_exact_duplicate_is_idempotent() -> None:
    provider = FakeStateProvider(observation())
    service = AttemptRevalidationService(
        state_provider=provider,
        result_repository=InMemoryRevalidationResultRepository(),
    )

    first = await service.revalidate(request())
    duplicate = await service.revalidate(request())

    assert duplicate == first
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_conflicting_submission_with_the_same_key_is_rejected() -> None:
    service = AttemptRevalidationService(
        state_provider=FakeStateProvider(observation()),
        result_repository=InMemoryRevalidationResultRepository(),
    )

    await service.revalidate(request())
    conflict = await service.revalidate(
        request(checkpoint=RevalidationCheckpoint.BEFORE_FINAL_DISCLOSURE)
    )

    assert conflict.disposition is RevalidationDisposition.REJECTED
    assert conflict.failure_codes == (RevalidationFailureCode.IDEMPOTENCY_CONFLICT,)


@pytest.mark.asyncio
async def test_retryable_observation_is_not_cached_and_later_retry_can_replay() -> None:
    first_provider = FakeStateProvider(
        observation(availability=RevalidationAvailability.RETRYABLE_FAILURE)
    )
    repository = InMemoryRevalidationResultRepository()
    service = AttemptRevalidationService(
        state_provider=first_provider,
        result_repository=repository,
    )

    retryable = await service.revalidate(request())

    assert retryable.disposition is RevalidationDisposition.RETRYABLE_FAILURE
    assert retryable.retryable
    assert (
        await repository.resolve(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            idempotency_key="revalidate-1",
        )
        is None
    )

    second_provider = FakeStateProvider(observation())
    retry_service = AttemptRevalidationService(
        state_provider=second_provider,
        result_repository=repository,
    )
    replay = await retry_service.revalidate(request(retry_attempt=2))

    assert replay.disposition is RevalidationDisposition.SAFE_REPLAY
    assert second_provider.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "reason"),
    (
        (
            "membership_generation",
            2,
            RevalidationFailureCode.AUTHORITY_GENERATION_CHANGED,
        ),
        (
            "authority_generation",
            2,
            RevalidationFailureCode.AUTHORITY_GENERATION_CHANGED,
        ),
        (
            "service_authority_generation",
            2,
            RevalidationFailureCode.AUTHORITY_GENERATION_CHANGED,
        ),
        ("service_grant_valid", False, RevalidationFailureCode.SERVICE_GRANT_EXPIRED),
        ("retention_eligible", False, RevalidationFailureCode.RETENTION_INELIGIBLE),
        ("superseded", True, RevalidationFailureCode.SOURCE_SUPERSEDED),
    ),
)
async def test_current_authorization_or_source_change_fails_closed(
    field: str, value: object, reason: RevalidationFailureCode
) -> None:
    provider = FakeStateProvider(
        observation(
            source_states=(
                RevalidationSourceObservation(
                    **source_observation(**{field: value})
                ),
            )
        )
    )
    service = AttemptRevalidationService(
        state_provider=provider,
        result_repository=InMemoryRevalidationResultRepository(),
    )

    result = await service.revalidate(request())

    assert result.disposition is RevalidationDisposition.REJECTED
    assert reason in result.failure_codes


@pytest.mark.asyncio
async def test_missing_evidence_is_regeneration_only_and_never_replayed() -> None:
    service = AttemptRevalidationService(
        state_provider=FakeStateProvider(observation(evidence_states=())),
        result_repository=InMemoryRevalidationResultRepository(),
    )

    result = await service.revalidate(request())

    assert result.disposition is RevalidationDisposition.SAFE_REGENERATION
    assert RevalidationFailureCode.EVIDENCE_MISSING in result.failure_codes
    assert result.bundle_reference is None


@pytest.mark.asyncio
async def test_pin_mismatch_rejects_even_when_the_provider_reports_a_healthy_state() -> None:
    service = AttemptRevalidationService(
        state_provider=FakeStateProvider(
            observation(
                publication=RevalidationPublicationObservation(
                    pinned_snapshot_id=UUID(int=90),
                    pinned_activation_generation=1,
                    current_snapshot_id=SNAPSHOT,
                    current_activation_generation=1,
                )
            )
        ),
        result_repository=InMemoryRevalidationResultRepository(),
    )

    result = await service.revalidate(request())

    assert result.disposition is RevalidationDisposition.REJECTED
    assert result.failure_codes == (RevalidationFailureCode.PIN_INCONSISTENT,)


@pytest.mark.asyncio
async def test_unavailable_current_publication_cannot_be_replayed() -> None:
    service = AttemptRevalidationService(
        state_provider=FakeStateProvider(
            observation(
                publication=RevalidationPublicationObservation(
                    pinned_snapshot_id=SNAPSHOT,
                    pinned_activation_generation=1,
                    current_publication_available=False,
                )
            )
        ),
        result_repository=InMemoryRevalidationResultRepository(),
    )

    result = await service.revalidate(request())

    assert result.disposition is RevalidationDisposition.REJECTED
    assert result.failure_codes == (RevalidationFailureCode.REVALIDATION_UNAVAILABLE,)


@pytest.mark.asyncio
async def test_observation_from_another_workspace_fails_closed() -> None:
    service = AttemptRevalidationService(
        state_provider=FakeStateProvider(
            observation(workspace_id=UUID(int=99))
        ),
        result_repository=InMemoryRevalidationResultRepository(),
    )

    result = await service.revalidate(request())

    assert result.disposition is RevalidationDisposition.REJECTED
    assert result.failure_codes == (RevalidationFailureCode.REVALIDATION_UNAVAILABLE,)


@pytest.mark.asyncio
async def test_same_idempotency_key_isolated_between_workspaces() -> None:
    repository = InMemoryRevalidationResultRepository()
    service = AttemptRevalidationService(
        state_provider=FakeStateProvider(observation()),
        result_repository=repository,
    )

    first = await service.revalidate(request())
    other_workspace = first.model_copy(update={"workspace_id": UUID(int=99)})
    await repository.save(other_workspace)

    assert (
        await repository.resolve(
            workspace_id=UUID(int=99),
            environment_id=ENVIRONMENT,
            idempotency_key="revalidate-1",
        )
        == other_workspace
    )
    assert (
        await repository.resolve(
            workspace_id=UUID(int=98),
            environment_id=ENVIRONMENT,
            idempotency_key="revalidate-1",
        )
        is None
    )


@pytest.mark.asyncio
async def test_concurrent_duplicate_submissions_share_one_logical_result() -> None:
    provider = FakeStateProvider(observation())
    service = AttemptRevalidationService(
        state_provider=provider,
        result_repository=InMemoryRevalidationResultRepository(),
    )

    results = await asyncio.gather(
        service.revalidate(request()),
        service.revalidate(request()),
    )

    assert results[0] == results[1]
    assert provider.calls == 1
