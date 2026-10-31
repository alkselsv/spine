from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.lifecycle import (
    InMemoryLifecycleAuthorityRepository,
    LifecycleAuthorityError,
    LifecycleConflictError,
    LifecycleEligibilityQuery,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.domain.common import ContextOrigin
from spine.domain.sources.lifecycle import (
    LifecycleDecision,
    LifecycleDecisionEffect,
    LifecycleEligibilityStatus,
    LifecycleObservation,
    LifecycleObservationConfidence,
    LifecycleObservationKind,
    LifecycleReasonCode,
)


def _id(value: int) -> UUID:
    return UUID(int=value)


def _context(workspace: int = 2, environment: int = 3) -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(_id(workspace), _id(environment)),
        origin=ContextOrigin.INTERACTIVE,
        purpose=PersistencePurpose("lifecycle_governance"),
        operation=PersistenceOperation("lifecycle_decision"),
        trace_id=_id(20),
        acting_subject_id=_id(14),
        service_principal_id=None,
        provenance=TrustedContextProvenance(_id(21), b"verified-context-proof"),
    )


def _observation(
    observation_id: int = 1,
    *,
    observed_at: datetime = datetime(2026, 1, 2, tzinfo=timezone.utc),
) -> LifecycleObservation:
    return LifecycleObservation(
        observation_id=_id(observation_id),
        workspace_id=_id(2),
        environment_id=_id(3),
        source_object_id=_id(4),
        connection_id=_id(5),
        connection_version_id=_id(6),
        authorization_generation=2,
        configuration_generation=3,
        scope_reference=_id(7),
        scope_version_id=_id(8),
        scope_digest="a" * 64,
        provider_identity_reference=_id(9),
        kind=LifecycleObservationKind.SCOPE_WITHDRAWN,
        evidence_reference=_id(10),
        evidence_digest="b" * 64,
        confidence=LifecycleObservationConfidence.QUALIFIED,
        reason_code=LifecycleReasonCode.SCOPE_WITHDRAWN,
        observed_at=observed_at,
    )


def _decision(
    *,
    decision_id: int = 11,
    observation_id: int = 1,
    expected_id: UUID | None = None,
    expected_generation: int = 0,
    generation: int = 1,
    status: LifecycleEligibilityStatus = LifecycleEligibilityStatus.READ_WITHDRAWN,
) -> LifecycleDecision:
    return LifecycleDecision(
        decision_id=_id(decision_id),
        workspace_id=_id(2),
        environment_id=_id(3),
        source_object_id=_id(4),
        observation_id=_id(observation_id),
        connection_id=_id(5),
        connection_version_id=_id(6),
        scope_reference=_id(7),
        scope_version_id=_id(8),
        scope_digest="a" * 64,
        effect=LifecycleDecisionEffect.READ_WITHDRAWN,
        authority_reference=_id(12),
        authority_version_id=_id(13),
        authority_digest="c" * 64,
        status=status,
        legal_hold_active=False,
        expected_current_decision_id=expected_id,
        expected_current_generation=expected_generation,
        generation=generation,
        actor_id=_id(14),
        audit_event_id=_id(15 + decision_id),
        outbox_event_id=_id(30 + decision_id),
        decided_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
    )


class _AllowExactAuthority:
    async def authorize(self, decision, context, transaction) -> None:
        assert context.acting_subject_id == decision.actor_id
        assert transaction is None


class _DenyAuthority:
    async def authorize(self, decision, context, transaction) -> None:
        raise LifecycleAuthorityError()


def _query() -> LifecycleEligibilityQuery:
    return LifecycleEligibilityQuery(
        workspace_id=_id(2),
        environment_id=_id(3),
        source_object_id=_id(4),
        connection_id=_id(5),
        connection_version_id=_id(6),
        scope_reference=_id(7),
        scope_version_id=_id(8),
        scope_digest="a" * 64,
    )


@pytest.mark.asyncio
async def test_lifecycle_observation_does_not_advance_current_eligibility() -> None:
    repository = InMemoryLifecycleAuthorityRepository(_AllowExactAuthority())
    context = _context()
    first = _observation()
    await repository.record_observation(first, context)
    current = await repository.current_eligibility(_query(), context)

    assert current.current_decision_id is None
    assert current.generation == 0
    assert current.status is LifecycleEligibilityStatus.AUTHORITY_UNAVAILABLE


@pytest.mark.asyncio
async def test_current_eligibility_follows_cas_pointer_not_observation_time() -> None:
    repository = InMemoryLifecycleAuthorityRepository(_AllowExactAuthority())
    context = _context()
    observation = _observation()
    await repository.record_observation(observation, context)
    decision = _decision()
    await repository.record_decision(decision, context)
    await repository.record_observation(
        _observation(16, observed_at=datetime(2030, 1, 1, tzinfo=timezone.utc)),
        context,
    )

    current = await repository.current_eligibility(_query(), context)

    assert current.current_decision_id == decision.decision_id
    assert current.generation == 1
    assert current.status is LifecycleEligibilityStatus.READ_WITHDRAWN


@pytest.mark.asyncio
async def test_lifecycle_decision_rejects_stale_expected_current_pointer() -> None:
    repository = InMemoryLifecycleAuthorityRepository(_AllowExactAuthority())
    context = _context()
    await repository.record_observation(_observation(), context)
    await repository.record_decision(_decision(), context)

    with pytest.raises(LifecycleConflictError):
        await repository.record_decision(
            _decision(
                decision_id=17,
                expected_id=None,
                expected_generation=0,
                generation=1,
            ),
            context,
        )


@pytest.mark.asyncio
async def test_lifecycle_decision_requires_current_governance_authority() -> None:
    repository = InMemoryLifecycleAuthorityRepository(_DenyAuthority())
    context = _context()
    await repository.record_observation(_observation(), context)

    with pytest.raises(LifecycleAuthorityError):
        await repository.record_decision(_decision(), context)

    current = await repository.current_eligibility(_query(), context)
    assert current.current_decision_id is None
    assert current.status is LifecycleEligibilityStatus.AUTHORITY_UNAVAILABLE


@pytest.mark.asyncio
async def test_lifecycle_decision_rejects_observation_from_another_scope() -> None:
    repository = InMemoryLifecycleAuthorityRepository(_AllowExactAuthority())
    context = _context()
    await repository.record_observation(_observation(), context)

    with pytest.raises(LifecycleAuthorityError):
        await repository.record_decision(
            _decision().model_copy(update={"scope_digest": "d" * 64}), context
        )


@pytest.mark.asyncio
async def test_lifecycle_observation_replay_is_idempotent_and_immutable() -> None:
    repository = InMemoryLifecycleAuthorityRepository(_AllowExactAuthority())
    context = _context()
    observation = _observation()

    assert await repository.record_observation(observation, context) == observation
    assert await repository.record_observation(observation, context) == observation
    with pytest.raises(LifecycleConflictError):
        await repository.record_observation(
            observation.model_copy(update={"evidence_digest": "d" * 64}),
            context,
        )


@pytest.mark.asyncio
async def test_lifecycle_reader_denies_cross_workspace_without_returning_record() -> None:
    repository = InMemoryLifecycleAuthorityRepository(_AllowExactAuthority())
    with pytest.raises(LifecycleAuthorityError, match="Lifecycle authority is unavailable"):
        await repository.current_eligibility(_query(), _context(workspace=99))
