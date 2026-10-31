from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.disclosure_holds import (
    CreateDisclosureHoldCommand,
    DisclosureConstraintError,
    DisclosureConstraintQuery,
    DisclosureHoldAuthorityError,
    DisclosureHoldConflictError,
    DisclosureHoldService,
    InMemoryDisclosureHoldRepository,
    ReleaseDisclosureHoldCommand,
    require_disclosure_unheld,
)
from spine.application.lifecycle import (
    InMemoryLifecycleAuthorityRepository,
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
from spine.domain.sources.disclosure_holds import (
    DisclosureHold,
    DisclosureHoldEffect,
    DisclosureHoldReason,
    DisclosureHoldRelease,
    disclosure_hold_dependency_id,
)
from spine.domain.sources.lifecycle import (
    LifecycleAuthorityKind,
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


def _context(*, workspace: int = 2, actor: int = 14) -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(_id(workspace), _id(3)),
        origin=ContextOrigin.INTERACTIVE,
        purpose=PersistencePurpose("disclosure_hold_governance"),
        operation=PersistenceOperation("disclosure_hold"),
        trace_id=_id(90),
        acting_subject_id=_id(actor),
        service_principal_id=None,
        provenance=TrustedContextProvenance(_id(91), b"verified-context-proof"),
    )


def _observation() -> LifecycleObservation:
    return LifecycleObservation(
        observation_id=_id(1),
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
        kind=LifecycleObservationKind.TRASHED,
        evidence_reference=_id(10),
        evidence_digest="b" * 64,
        confidence=LifecycleObservationConfidence.QUALIFIED,
        reason_code=LifecycleReasonCode.PROVIDER_TRASHED,
        observed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )


def _decision() -> LifecycleDecision:
    return LifecycleDecision(
        decision_id=_id(11),
        workspace_id=_id(2),
        environment_id=_id(3),
        source_object_id=_id(4),
        observation_id=_id(1),
        connection_id=_id(5),
        connection_version_id=_id(6),
        scope_reference=_id(7),
        scope_version_id=_id(8),
        scope_digest="a" * 64,
        effect=LifecycleDecisionEffect.DISCLOSURE_REVIEW_REQUIRED,
        authority_reference=_id(12),
        authority_version_id=_id(13),
        authority_digest="c" * 64,
        authority_kind=LifecycleAuthorityKind.SOURCE_GOVERNANCE,
        status=LifecycleEligibilityStatus.DISCLOSURE_REVIEW_REQUIRED,
        legal_hold_active=False,
        expected_current_decision_id=None,
        expected_current_generation=0,
        generation=1,
        actor_id=_id(14),
        audit_event_id=_id(15),
        outbox_event_id=_id(16),
        decided_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
    )


def _hold(*, hold_id: int = 20, actor: int = 14) -> DisclosureHold:
    return DisclosureHold(
        hold_id=_id(hold_id),
        workspace_id=_id(2),
        environment_id=_id(3),
        source_object_id=_id(4),
        source_revision_ids=(_id(30),),
        dependency_ids=(
            disclosure_hold_dependency_id(_id(2), _id(3), _id(4), _id(30)),
        ),
        artifact_ids=(_id(32),),
        review_decision_id=_id(11),
        evidence_observation_id=_id(1),
        prohibited_effects=(
            DisclosureHoldEffect.RETRIEVAL,
            DisclosureHoldEffect.FINAL_DISCLOSURE,
        ),
        authority_reference=_id(40),
        authority_version_id=_id(41),
        authority_digest="d" * 64,
        actor_id=_id(actor),
        reason_code=DisclosureHoldReason.GOVERNED_SOURCE_REVIEW,
        effective_from=datetime(2026, 1, 4, tzinfo=timezone.utc),
        effective_until=datetime(2026, 2, 4, tzinfo=timezone.utc),
        audit_event_id=_id(42 + hold_id),
        outbox_event_id=_id(142 + hold_id),
    )


def _release(hold: DisclosureHold, *, release_id: int = 50) -> DisclosureHoldRelease:
    return DisclosureHoldRelease(
        release_id=_id(release_id),
        workspace_id=hold.workspace_id,
        environment_id=hold.environment_id,
        source_object_id=hold.source_object_id,
        hold_id=hold.hold_id,
        authority_reference=_id(51),
        authority_version_id=_id(52),
        authority_digest="e" * 64,
        actor_id=_id(14),
        reason_code=DisclosureHoldReason.REVIEW_COMPLETED,
        released_at=datetime(2026, 1, 6, tzinfo=timezone.utc),
        audit_event_id=_id(53 + release_id),
        outbox_event_id=_id(153 + release_id),
    )


def _lifecycle_query() -> LifecycleEligibilityQuery:
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


def _constraint(
    effect: DisclosureHoldEffect,
    *,
    evaluated_at: datetime = datetime(2026, 1, 5, tzinfo=timezone.utc),
) -> DisclosureConstraintQuery:
    return DisclosureConstraintQuery(
        workspace_id=_id(2),
        environment_id=_id(3),
        source_object_id=_id(4),
        source_revision_id=_id(30),
        dependency_id=disclosure_hold_dependency_id(
            _id(2), _id(3), _id(4), _id(30)
        ),
        artifact_id=_id(32),
        effect=effect,
        evaluated_at=evaluated_at,
    )


def _create_command(hold: DisclosureHold) -> CreateDisclosureHoldCommand:
    return CreateDisclosureHoldCommand(
        hold=hold,
        observation=_observation(),
        review_decision=_decision(),
        lifecycle_query=_lifecycle_query(),
    )


class _LifecycleAuthority:
    async def verify(self, observation, context, transaction) -> None:
        return None

    async def authorize(self, decision, context, transaction) -> None:
        return None


class _HoldAuthority:
    async def authorize_create(self, hold, context, transaction) -> None:
        return None

    async def authorize_release(self, release, hold, context, transaction) -> None:
        return None


class _DenyHoldAuthority(_HoldAuthority):
    async def authorize_create(self, hold, context, transaction) -> None:
        raise DisclosureHoldAuthorityError()


async def _review_repository() -> InMemoryLifecycleAuthorityRepository:
    authority = _LifecycleAuthority()
    repository = InMemoryLifecycleAuthorityRepository(authority, authority)
    await repository.record_observation(_observation(), _context())
    await repository.record_decision(_decision(), _context())
    return repository


@pytest.mark.asyncio
async def test_authorized_hold_create_replay_and_current_reader() -> None:
    lifecycle = await _review_repository()
    repository = InMemoryDisclosureHoldRepository(_HoldAuthority())
    service = DisclosureHoldService(repository, lifecycle)
    command = _create_command(_hold())

    assert await service.create(command, _context()) == command.hold
    assert await service.create(command, _context()) == command.hold
    current = await repository.active_holds(
        _constraint(DisclosureHoldEffect.RETRIEVAL), _context()
    )

    assert current.hold_ids == (_id(20),)
    assert current.denied is True


@pytest.mark.asyncio
async def test_hold_conflicting_replay_and_unauthorized_worker_self_approval_deny() -> (
    None
):
    lifecycle = await _review_repository()
    repository = InMemoryDisclosureHoldRepository(_DenyHoldAuthority())
    service = DisclosureHoldService(repository, lifecycle)

    with pytest.raises(DisclosureHoldAuthorityError):
        await service.create(_create_command(_hold()), _context())

    worker = replace(
        _context(),
        origin=ContextOrigin.WORKER,
        acting_subject_id=None,
        service_principal_id=_id(14),
    )
    worker_repository = InMemoryDisclosureHoldRepository(_HoldAuthority())
    worker_service = DisclosureHoldService(worker_repository, lifecycle)
    with pytest.raises(DisclosureHoldAuthorityError):
        await worker_service.create(_create_command(_hold()), worker)

    allowed = InMemoryDisclosureHoldRepository(_HoldAuthority())
    service = DisclosureHoldService(allowed, lifecycle)
    await service.create(_create_command(_hold()), _context())
    with pytest.raises(DisclosureHoldConflictError):
        await service.create(
            _create_command(_hold().model_copy(update={"authority_digest": "f" * 64})),
            _context(),
        )


@pytest.mark.asyncio
async def test_separate_release_clears_only_exact_hold_without_granting_disclosure() -> (
    None
):
    lifecycle = await _review_repository()
    repository = InMemoryDisclosureHoldRepository(_HoldAuthority())
    service = DisclosureHoldService(repository, lifecycle)
    hold = _hold()
    await service.create(_create_command(hold), _context())
    release = _release(hold)

    assert (
        await service.release(ReleaseDisclosureHoldCommand(release), _context())
        == release
    )
    assert (
        await service.release(ReleaseDisclosureHoldCommand(release), _context())
        == release
    )
    historical = await repository.active_holds(
        _constraint(
            DisclosureHoldEffect.FINAL_DISCLOSURE,
            evaluated_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
        ),
        _context(),
    )
    assert historical.hold_ids == (hold.hold_id,)
    current = await repository.active_holds(
        _constraint(
            DisclosureHoldEffect.FINAL_DISCLOSURE,
            evaluated_at=datetime(2026, 1, 7, tzinfo=timezone.utc),
        ),
        _context(),
    )
    assert current.denied is False

    with pytest.raises(DisclosureConstraintError):
        await require_disclosure_unheld(
            lifecycle,
            repository,
            _lifecycle_query(),
            _constraint(DisclosureHoldEffect.FINAL_DISCLOSURE),
            _context(),
        )


@pytest.mark.asyncio
async def test_release_one_hold_preserves_other_hold_and_workspace_isolation() -> None:
    lifecycle = await _review_repository()
    repository = InMemoryDisclosureHoldRepository(_HoldAuthority())
    service = DisclosureHoldService(repository, lifecycle)
    first = _hold()
    second = _hold(hold_id=21)
    await service.create(_create_command(first), _context())
    await service.create(_create_command(second), _context())
    await service.release(ReleaseDisclosureHoldCommand(_release(first)), _context())

    current = await repository.active_holds(
        _constraint(
            DisclosureHoldEffect.FINAL_DISCLOSURE,
            evaluated_at=datetime(2026, 1, 7, tzinfo=timezone.utc),
        ),
        _context(),
    )
    assert current.hold_ids == (_id(21),)

    with pytest.raises(DisclosureHoldAuthorityError):
        await repository.active_holds(
            _constraint(DisclosureHoldEffect.FINAL_DISCLOSURE),
            _context(workspace=99),
        )


@pytest.mark.asyncio
async def test_supersession_release_requires_reciprocal_exact_successor() -> None:
    lifecycle = await _review_repository()
    repository = InMemoryDisclosureHoldRepository(_HoldAuthority())
    service = DisclosureHoldService(repository, lifecycle)
    first = _hold()
    await service.create(_create_command(first), _context())

    successor = _hold(hold_id=21).model_copy(
        update={"supersedes_hold_id": first.hold_id}
    )
    await service.create(_create_command(successor), _context())
    with pytest.raises(DisclosureHoldAuthorityError):
        await service.release(
            ReleaseDisclosureHoldCommand(_release(first)), _context()
        )
    invalid_release = _release(first).model_copy(
        update={"superseded_by_hold_id": _id(22)}
    )
    with pytest.raises(DisclosureHoldAuthorityError):
        await service.release(ReleaseDisclosureHoldCommand(invalid_release), _context())

    release = _release(first).model_copy(
        update={"superseded_by_hold_id": successor.hold_id}
    )
    assert await service.release(ReleaseDisclosureHoldCommand(release), _context()) == release


@pytest.mark.asyncio
async def test_released_hold_cannot_gain_late_successor() -> None:
    lifecycle = await _review_repository()
    repository = InMemoryDisclosureHoldRepository(_HoldAuthority())
    service = DisclosureHoldService(repository, lifecycle)
    first = _hold()
    await service.create(_create_command(first), _context())
    await service.release(
        ReleaseDisclosureHoldCommand(_release(first)), _context()
    )

    successor = _hold(hold_id=21).model_copy(
        update={"supersedes_hold_id": first.hold_id}
    )
    with pytest.raises(DisclosureHoldConflictError):
        await service.create(_create_command(successor), _context())


@pytest.mark.asyncio
async def test_review_and_hold_deny_despite_independent_access_policy_allow() -> None:
    lifecycle = await _review_repository()
    repository = InMemoryDisclosureHoldRepository(_HoldAuthority())
    service = DisclosureHoldService(repository, lifecycle)
    await service.create(_create_command(_hold()), _context())

    access_policy_allowed = True
    assert access_policy_allowed is True
    with pytest.raises(DisclosureConstraintError, match="Disclosure is unavailable"):
        await require_disclosure_unheld(
            lifecycle,
            repository,
            _lifecycle_query(),
            _constraint(DisclosureHoldEffect.FINAL_DISCLOSURE),
            _context(),
        )
