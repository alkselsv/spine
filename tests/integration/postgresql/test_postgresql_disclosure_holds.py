from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from sqlalchemy import insert, select, update

from spine.application.disclosure_holds import (
    DisclosureConstraintQuery,
    DisclosureHoldAuthorityError,
    DisclosureHoldConflictError,
)
from spine.application.persistence.errors import (
    ConstraintConflictError,
    UnexpectedPersistenceError,
)
from spine.domain.sources.disclosure_holds import (
    DisclosureHold,
    DisclosureHoldEffect,
    DisclosureHoldReason,
    DisclosureHoldRelease,
    disclosure_hold_dependency_id,
)
from spine.domain.sources.lifecycle import (
    LifecycleDecisionEffect,
    LifecycleEligibilityStatus,
    LifecycleObservationConfidence,
    LifecycleObservationKind,
    LifecycleReasonCode,
)
from spine.infrastructure.persistence.postgresql_disclosure_holds import (
    PostgreSQLDisclosureHoldRepository,
    _hold_columns,
)
from spine.infrastructure.disclosure_hold_composition import (
    PostgreSQLDisclosureHoldEffectsWriter,
    PostgreSQLCurrentDisclosureHoldAuthority,
)
from spine.infrastructure.persistence.postgresql_lifecycle import (
    PostgreSQLLifecycleAuthorityRepository,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    audit_events,
    current_disclosure_holds,
    disclosure_hold_source_revisions,
    disclosure_hold_targets,
    disclosure_holds,
    outbox_intents,
)
from tests.integration.postgresql.test_postgresql_lifecycle_authority import (
    _AllowAuthority,
    _AllowObservationAuthority,
    _DecisionEffects,
    _decision,
    _observation,
    _prepare_lifecycle_case,
)
from tests.integration.postgresql.test_postgresql_unit_of_work import (
    PersistenceAdapter,
    postgresql_adapter,
    postgresql_contract_database,
    synthetic_uuid,
)

pytestmark = [pytest.mark.postgresql, pytest.mark.asyncio(loop_scope="session")]


class _HoldAuthority:
    async def authorize_create(self, hold, context, transaction) -> None:
        assert transaction is not None
        assert hold.actor_id == context.service_principal_id

    async def authorize_release(self, release, hold, context, transaction) -> None:
        assert transaction is not None
        assert release.hold_id == hold.hold_id
        assert release.actor_id == context.service_principal_id


class _HoldEffects:
    async def persist_hold(self, hold, context, transaction):
        await self._persist(
            hold.audit_event_id,
            hold.outbox_event_id,
            hold.hold_id,
            hold.effective_from,
            context,
            transaction,
        )
        return hold.audit_event_id, hold.outbox_event_id

    async def persist_release(self, release, context, transaction):
        await self._persist(
            release.audit_event_id,
            release.outbox_event_id,
            release.hold_id,
            release.released_at,
            context,
            transaction,
        )
        return release.audit_event_id, release.outbox_event_id

    async def _persist(self, audit_id, outbox_id, hold_id, occurred_at, context, transaction):
        await transaction._execute(  # type: ignore[attr-defined]
            insert(audit_events).values(
                audit_event_id=audit_id,
                workspace_id=context.scope.workspace_id,
                environment_id=context.scope.environment_id,
                event_type="source.disclosure_hold",
                schema_version=1,
                origin=context.origin.value,
                acting_subject_id=context.acting_subject_id,
                service_principal_id=context.service_principal_id,
                trace_id=context.trace_id,
                correlation_id=None,
                causation_id=None,
                occurred_at=occurred_at,
                appended_at=occurred_at,
                target_type="source_object",
                target_id=hold_id,
                target_schema_version=1,
                outcome="committed",
                reason="source_disclosure_hold",
                producer_deduplication_id=f"disclosure-hold-{audit_id}",
                payload={"hold_id": str(hold_id)},
            )
        )
        await transaction._execute(  # type: ignore[attr-defined]
            insert(outbox_intents).values(
                event_id=outbox_id,
                workspace_id=context.scope.workspace_id,
                environment_id=context.scope.environment_id,
                event_type="source.disclosure_hold",
                event_schema_version=1,
                aggregate_type="source_object",
                aggregate_id=hold_id,
                aggregate_schema_version=1,
                producer_deduplication_id=f"disclosure-hold-{outbox_id}",
                payload={"hold_id": str(hold_id)},
                trace_id=context.trace_id,
                correlation_id=None,
                causation_id=None,
                created_at=occurred_at,
            )
        )


async def _review_case(adapter: PersistenceAdapter, number: int):
    case = await _prepare_lifecycle_case(adapter, number)
    context = adapter.ingestion_context(
        case.source.workspace_id,
        case.source.environment_id,
        "disclosure_hold",
    )
    lifecycle = PostgreSQLLifecycleAuthorityRepository(
        uow_factory=adapter.uow_factory,
        observation_authority=_AllowObservationAuthority(),
        decision_authority=_AllowAuthority(),
        decision_effects=_DecisionEffects(),
    )
    observation = _observation(case, number + 10).model_copy(
        update={
            "kind": LifecycleObservationKind.TRASHED,
            "confidence": LifecycleObservationConfidence.QUALIFIED,
            "reason_code": LifecycleReasonCode.PROVIDER_TRASHED,
        }
    )
    decision = _decision(case, observation, context, number + 20).model_copy(
        update={
            "effect": LifecycleDecisionEffect.DISCLOSURE_REVIEW_REQUIRED,
            "status": LifecycleEligibilityStatus.DISCLOSURE_REVIEW_REQUIRED,
        }
    )
    await lifecycle.record_observation(observation, context)
    await lifecycle.record_decision(decision, context)
    return case, context, decision


def _hold(case, context, decision, number: int) -> DisclosureHold:
    return DisclosureHold(
        hold_id=synthetic_uuid(number),
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        source_object_id=case.source.source_object_id,
        source_revision_ids=(case.revision.revision_id,),
        dependency_ids=(
            disclosure_hold_dependency_id(
                case.source.workspace_id,
                case.source.environment_id,
                case.source.source_object_id,
                case.revision.revision_id,
            ),
        ),
        artifact_ids=(synthetic_uuid(number + 2),),
        review_decision_id=decision.decision_id,
        evidence_observation_id=decision.observation_id,
        prohibited_effects=(
            DisclosureHoldEffect.RETRIEVAL,
            DisclosureHoldEffect.FINAL_DISCLOSURE,
        ),
        authority_reference=decision.authority_reference,
        authority_version_id=decision.authority_version_id,
        authority_digest=decision.authority_digest,
        actor_id=decision.actor_id,
        reason_code=DisclosureHoldReason.GOVERNED_SOURCE_REVIEW,
        effective_from=datetime(2026, 10, 31, 2, tzinfo=timezone.utc),
        effective_until=datetime(2026, 12, 1, tzinfo=timezone.utc),
        audit_event_id=synthetic_uuid(number + 5),
        outbox_event_id=synthetic_uuid(number + 6),
    )


def _release(hold: DisclosureHold, context, number: int) -> DisclosureHoldRelease:
    return DisclosureHoldRelease(
        release_id=synthetic_uuid(number),
        workspace_id=hold.workspace_id,
        environment_id=hold.environment_id,
        source_object_id=hold.source_object_id,
        hold_id=hold.hold_id,
        authority_reference=hold.authority_reference,
        authority_version_id=hold.authority_version_id,
        authority_digest=hold.authority_digest,
        actor_id=hold.actor_id,
        reason_code=DisclosureHoldReason.REVIEW_COMPLETED,
        released_at=datetime(2026, 11, 2, tzinfo=timezone.utc),
        audit_event_id=synthetic_uuid(number + 3),
        outbox_event_id=synthetic_uuid(number + 4),
    )


def _query(hold: DisclosureHold, at: datetime) -> DisclosureConstraintQuery:
    return DisclosureConstraintQuery(
        workspace_id=hold.workspace_id,
        environment_id=hold.environment_id,
        source_object_id=hold.source_object_id,
        source_revision_id=hold.source_revision_ids[0],
        dependency_id=hold.dependency_ids[0],
        artifact_id=hold.artifact_ids[0],
        effect=DisclosureHoldEffect.FINAL_DISCLOSURE,
        evaluated_at=at,
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_hold_create_release_replay_and_workspace_isolation(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    case, context, decision = await _review_case(postgresql_adapter, 733100)
    repository = PostgreSQLDisclosureHoldRepository(
        uow_factory=postgresql_adapter.uow_factory,
        authority=_HoldAuthority(),
        effects=PostgreSQLDisclosureHoldEffectsWriter(),
    )
    hold = _hold(case, context, decision, 733130)
    release = _release(hold, context, 733140)

    assert await repository.create_hold(hold, context) == hold
    assert await repository.create_hold(hold, context) == hold
    active = await repository.active_holds(_query(hold, datetime(2026, 11, 1, tzinfo=timezone.utc)), context)
    assert active.hold_ids == (hold.hold_id,)
    async with postgresql_adapter.uow_factory(context) as uow:
        target = (
            (
                await uow._execute(  # type: ignore[attr-defined]
                    select(disclosure_hold_targets).where(disclosure_hold_targets.c.hold_id == hold.hold_id)
                )
            )
            .mappings()
            .one()
        )
        assert (
            target["workspace_id"],
            target["environment_id"],
            target["source_object_id"],
            target["source_revision_id"],
            target["dependency_id"],
            target["artifact_id"],
        ) == (
            hold.workspace_id,
            hold.environment_id,
            hold.source_object_id,
            hold.source_revision_ids[0],
            hold.dependency_ids[0],
            hold.artifact_ids[0],
        )
        await uow.rollback()  # type: ignore[attr-defined]
    unrelated = await repository.active_holds(
        replace(
            _query(hold, datetime(2026, 11, 1, tzinfo=timezone.utc)),
            artifact_id=synthetic_uuid(733999),
        ),
        context,
    )
    assert unrelated.hold_ids == ()

    with pytest.raises(UnexpectedPersistenceError):
        async with postgresql_adapter.uow_factory(context) as uow:
            await uow._execute(  # type: ignore[attr-defined]
                update(disclosure_holds)
                .where(disclosure_holds.c.hold_id == hold.hold_id)
                .values(effective_until=datetime(2027, 1, 1, tzinfo=timezone.utc))
            )
            await uow.commit()  # type: ignore[attr-defined]

    with pytest.raises(ConstraintConflictError):
        async with postgresql_adapter.uow_factory(context) as uow:
            await uow._execute(  # type: ignore[attr-defined]
                insert(disclosure_hold_targets).values(
                    workspace_id=hold.workspace_id,
                    environment_id=hold.environment_id,
                    source_object_id=hold.source_object_id,
                    hold_id=hold.hold_id,
                    source_revision_id=hold.source_revision_ids[0],
                    dependency_id=hold.dependency_ids[0],
                    artifact_id=synthetic_uuid(733997),
                )
            )
            await uow.commit()  # type: ignore[attr-defined]

    with pytest.raises(UnexpectedPersistenceError):
        async with postgresql_adapter.uow_factory(context) as uow:
            await uow._execute(  # type: ignore[attr-defined]
                update(disclosure_hold_targets)
                .where(disclosure_hold_targets.c.hold_id == hold.hold_id)
                .values(artifact_id=synthetic_uuid(733998))
            )
            await uow.commit()  # type: ignore[attr-defined]

    assert await repository.release_hold(release, context) == release
    assert await repository.release_hold(release, context) == release
    historical = await repository.active_holds(_query(hold, datetime(2026, 11, 1, tzinfo=timezone.utc)), context)
    assert historical.hold_ids == (hold.hold_id,)
    released = await repository.active_holds(_query(hold, datetime(2026, 11, 3, tzinfo=timezone.utc)), context)
    assert released.hold_ids == ()
    late_successor = hold.model_copy(
        update={
            "hold_id": synthetic_uuid(733150),
            "audit_event_id": synthetic_uuid(733151),
            "outbox_event_id": synthetic_uuid(733152),
            "supersedes_hold_id": hold.hold_id,
        }
    )
    with pytest.raises(DisclosureHoldConflictError):
        await repository.create_hold(late_successor, context)

    foreign = await _prepare_lifecycle_case(postgresql_adapter, 733160)
    foreign_context = postgresql_adapter.ingestion_context(
        foreign.source.workspace_id, foreign.source.environment_id, "disclosure_hold"
    )
    with pytest.raises(DisclosureHoldAuthorityError):
        await repository.active_holds(
            _query(hold, datetime(2026, 11, 1, tzinfo=timezone.utc)),
            foreign_context,
        )
    async with postgresql_adapter.uow_factory(foreign_context) as uow:
        hidden = await uow._execute(  # type: ignore[attr-defined]
            select(disclosure_holds.c.hold_id).where(disclosure_holds.c.hold_id == hold.hold_id)
        )
        assert hidden.scalar_one_or_none() is None
        await uow.rollback()  # type: ignore[attr-defined]


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_hold_release_cas_allows_one_immutable_winner(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    case, context, decision = await _review_case(postgresql_adapter, 733200)
    repository = PostgreSQLDisclosureHoldRepository(
        uow_factory=postgresql_adapter.uow_factory,
        authority=_HoldAuthority(),
        effects=_HoldEffects(),
    )
    hold = _hold(case, context, decision, 733230)
    await repository.create_hold(hold, context)
    first = _release(hold, context, 733240)
    second = _release(hold, context, 733250)

    results = await asyncio.gather(
        repository.release_hold(first, context),
        repository.release_hold(second, context),
        return_exceptions=True,
    )

    assert sum(isinstance(item, DisclosureHoldRelease) for item in results) == 1
    assert sum(isinstance(item, (DisclosureHoldConflictError, DisclosureHoldAuthorityError)) for item in results) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_current_authority_is_exact_current_and_workspace_scoped(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    assert postgresql_adapter.governance_context is not None
    case, worker_context, decision = await _review_case(postgresql_adapter, 733300)
    create_context = postgresql_adapter.governance_context(
        case.source.workspace_id,
        case.source.environment_id,
        decision.actor_id,
        "disclosure_hold.create",
    )
    release_context = postgresql_adapter.governance_context(
        case.source.workspace_id,
        case.source.environment_id,
        decision.actor_id,
        "disclosure_hold.release",
    )
    repository = PostgreSQLDisclosureHoldRepository(
        uow_factory=postgresql_adapter.uow_factory,
        authority=PostgreSQLCurrentDisclosureHoldAuthority(),
        effects=PostgreSQLDisclosureHoldEffectsWriter(),
    )
    hold = _hold(case, worker_context, decision, 733330)

    assert await repository.create_hold(hold, create_context) == hold
    stale = hold.model_copy(
        update={
            "hold_id": synthetic_uuid(733331),
            "authority_digest": "f" * 64,
            "audit_event_id": synthetic_uuid(733332),
            "outbox_event_id": synthetic_uuid(733333),
        }
    )
    with pytest.raises(DisclosureHoldAuthorityError):
        await repository.create_hold(stale, create_context)

    foreign_context = postgresql_adapter.governance_context(
        synthetic_uuid(733399),
        case.source.environment_id,
        decision.actor_id,
        "disclosure_hold.create",
    )
    with pytest.raises(DisclosureHoldAuthorityError):
        await repository.create_hold(
            hold.model_copy(update={"hold_id": synthetic_uuid(733398)}),
            foreign_context,
        )

    release = _release(hold, worker_context, 733340)
    assert await repository.release_hold(release, release_context) == release


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_active_pointer_rejects_incomplete_declared_target_set(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    case, context, decision = await _review_case(postgresql_adapter, 733400)
    hold = _hold(case, context, decision, 733430).model_copy(
        update={"artifact_ids": (synthetic_uuid(733432), synthetic_uuid(733433))}
    )

    with pytest.raises(ConstraintConflictError):
        async with postgresql_adapter.uow_factory(context) as uow:
            await PostgreSQLDisclosureHoldEffectsWriter().persist_hold(
                hold, context, uow
            )
            await uow._execute(  # type: ignore[attr-defined]
                insert(disclosure_holds).values(**_hold_columns(hold))
            )
            await uow._execute(  # type: ignore[attr-defined]
                insert(disclosure_hold_source_revisions).values(
                    workspace_id=hold.workspace_id,
                    environment_id=hold.environment_id,
                    source_object_id=hold.source_object_id,
                    hold_id=hold.hold_id,
                    source_revision_id=hold.source_revision_ids[0],
                )
            )
            await uow._execute(  # type: ignore[attr-defined]
                insert(disclosure_hold_targets).values(
                    workspace_id=hold.workspace_id,
                    environment_id=hold.environment_id,
                    source_object_id=hold.source_object_id,
                    hold_id=hold.hold_id,
                    source_revision_id=hold.source_revision_ids[0],
                    dependency_id=hold.dependency_ids[0],
                    artifact_id=hold.artifact_ids[0],
                )
            )
            await uow._execute(  # type: ignore[attr-defined]
                insert(current_disclosure_holds).values(
                    workspace_id=hold.workspace_id,
                    environment_id=hold.environment_id,
                    source_object_id=hold.source_object_id,
                    hold_id=hold.hold_id,
                    current_release_id=None,
                    state="active",
                    generation=1,
                    updated_at=hold.effective_from,
                )
            )
