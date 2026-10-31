from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from dataclasses import replace

import pytest
from sqlalchemy import insert, select

from spine.application.lifecycle import (
    LifecycleAuthorityError,
    LifecycleConflictError,
    LifecycleEligibilityQuery,
)
from spine.application.persistence.canonical import CanonicalAcceptanceCommand
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.repositories import SourceObservationCommand
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
from spine.infrastructure.persistence.postgresql_lifecycle import (
    PostgreSQLLifecycleAuthorityRepository,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    audit_events,
    lifecycle_decisions,
    outbox_intents,
)
from tests.integration.postgresql.test_postgresql_unit_of_work import (
    PersistenceAdapter,
    postgresql_adapter,
    postgresql_contract_database,
    prepare_canonical_case,
    synthetic_uuid,
)
from tests.contracts.persistence.test_canonical_acceptance_contract import (
    _audit as canonical_audit,
    _canonical_outbox,
)
from tests.contracts.persistence.test_source_observation_contract import (
    NOW,
    connector_source,
    provenance as source_provenance,
    tombstone,
)


pytestmark = [pytest.mark.postgresql, pytest.mark.asyncio(loop_scope="session")]


class _AllowAuthority:
    async def authorize(self, decision, context, transaction) -> None:
        assert transaction is not None
        assert context.service_principal_id is not None
        assert decision.actor_id == context.service_principal_id


class _AllowObservationAuthority:
    async def verify(self, observation, context, transaction) -> None:
        assert transaction is not None
        assert context.scope.workspace_id == observation.workspace_id
        assert context.scope.environment_id == observation.environment_id


class _DecisionEffects:
    def __init__(self, *, fail_after_audit: bool = False) -> None:
        self.fail_after_audit = fail_after_audit

    async def persist(self, decision, context, transaction):
        await transaction._execute(  # type: ignore[attr-defined]
            insert(audit_events).values(
                audit_event_id=decision.audit_event_id,
                workspace_id=decision.workspace_id,
                environment_id=decision.environment_id,
                event_type="source.lifecycle.decision",
                schema_version=1,
                origin=context.origin.value,
                acting_subject_id=context.acting_subject_id,
                service_principal_id=context.service_principal_id,
                trace_id=context.trace_id,
                correlation_id=None,
                causation_id=None,
                occurred_at=decision.decided_at,
                appended_at=decision.decided_at,
                target_type="source_object",
                target_id=decision.source_object_id,
                target_schema_version=1,
                outcome="committed",
                reason="source_lifecycle_decision",
                producer_deduplication_id=f"lifecycle-decision-{decision.decision_id}",
                payload={
                    "decision_id": str(decision.decision_id),
                    "generation": decision.generation,
                    "status": decision.status.value,
                },
            )
        )
        if self.fail_after_audit:
            raise LifecycleAuthorityError()
        await transaction._execute(  # type: ignore[attr-defined]
            insert(outbox_intents).values(
                event_id=decision.outbox_event_id,
                workspace_id=decision.workspace_id,
                environment_id=decision.environment_id,
                event_type="source.lifecycle.decision",
                event_schema_version=1,
                aggregate_type="source_object",
                aggregate_id=decision.source_object_id,
                aggregate_schema_version=1,
                producer_deduplication_id=f"lifecycle-decision-{decision.decision_id}",
                payload={
                    "decision_id": str(decision.decision_id),
                    "generation": decision.generation,
                    "status": decision.status.value,
                },
                trace_id=context.trace_id,
                correlation_id=None,
                causation_id=None,
                created_at=decision.decided_at,
            )
        )
        return decision.audit_event_id, decision.outbox_event_id


def _observation(case, base: int) -> LifecycleObservation:
    assert case.source.connection_id is not None
    return LifecycleObservation(
        observation_id=synthetic_uuid(base),
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        source_object_id=case.source.source_object_id,
        connection_id=case.source.connection_id,
        connection_version_id=synthetic_uuid(base + 2),
        authorization_generation=1,
        configuration_generation=1,
        scope_reference=synthetic_uuid(base + 3),
        scope_version_id=synthetic_uuid(base + 4),
        scope_digest="a" * 64,
        provider_identity_reference=synthetic_uuid(base + 5),
        kind=LifecycleObservationKind.SCOPE_WITHDRAWN,
        evidence_reference=synthetic_uuid(base + 6),
        evidence_digest="b" * 64,
        confidence=LifecycleObservationConfidence.QUALIFIED,
        reason_code=LifecycleReasonCode.SCOPE_WITHDRAWN,
        observed_at=datetime(2026, 10, 31, tzinfo=timezone.utc),
    )


def _decision(case, observation: LifecycleObservation, context, base: int) -> LifecycleDecision:
    return LifecycleDecision(
        decision_id=synthetic_uuid(base),
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        source_object_id=case.source.source_object_id,
        observation_id=observation.observation_id,
        connection_id=observation.connection_id,
        connection_version_id=observation.connection_version_id,
        scope_reference=observation.scope_reference,
        scope_version_id=observation.scope_version_id,
        scope_digest=observation.scope_digest,
        effect=LifecycleDecisionEffect.READ_WITHDRAWN,
        authority_reference=synthetic_uuid(base + 1),
        authority_version_id=synthetic_uuid(base + 2),
        authority_digest="c" * 64,
        authority_kind=LifecycleAuthorityKind.SOURCE_GOVERNANCE,
        status=LifecycleEligibilityStatus.READ_WITHDRAWN,
        legal_hold_active=False,
        expected_current_decision_id=None,
        expected_current_generation=0,
        generation=1,
        actor_id=context.service_principal_id,
        audit_event_id=synthetic_uuid(base + 3),
        outbox_event_id=synthetic_uuid(base + 4),
        decided_at=datetime(2026, 10, 31, 1, tzinfo=timezone.utc),
    )


def _query(case, observation: LifecycleObservation) -> LifecycleEligibilityQuery:
    return LifecycleEligibilityQuery(
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        source_object_id=case.source.source_object_id,
        connection_id=observation.connection_id,
        connection_version_id=observation.connection_version_id,
        scope_reference=observation.scope_reference,
        scope_version_id=observation.scope_version_id,
        scope_digest=observation.scope_digest,
    )


async def _prepare_lifecycle_case(adapter: PersistenceAdapter, number: int):
    workspace_id = synthetic_uuid(number)
    environment_id = synthetic_uuid(number + 1)
    source = connector_source(workspace_id, environment_id, "provider1")
    return await prepare_canonical_case(adapter, number, source_object=source)


async def _accept_canonical_tombstone(adapter: PersistenceAdapter, case, number: int) -> None:
    async with adapter.uow_factory(case.context) as uow:
        await uow.canonical.accept(case.command)
        await uow.commit()

    revision = tombstone(case.source, synthetic_uuid(number + 1))
    provenance = source_provenance(case.source, revision.revision_id, "tombstone-event-2")
    source_context = adapter.source_context(
        case.source.workspace_id, case.source.environment_id
    )
    source_command = SourceObservationCommand.create(
        source=case.source,
        revision=revision,
        provenance=provenance,
        idempotency_key=IdempotencyKey(f"lifecycle-tombstone-observation-{number}"),
    )
    async with adapter.uow_factory(source_context) as uow:
        await uow.sources.record_observation(source_command)
        await uow.commit()
    acceptance_id = synthetic_uuid(number + 2)
    audit_id = synthetic_uuid(number + 3)
    outbox_id = synthetic_uuid(number + 4)
    audit = canonical_audit(
        context=case.context,
        source_revision_id=revision.revision_id,
        event_id=audit_id,
        reason="source_tombstoned",
        transition="source.tombstoned",
    )
    outbox = _canonical_outbox(
        adapter,
        context=case.context,
        source_object_id=case.source.source_object_id,
        source_revision_id=revision.revision_id,
        acceptance_id=acceptance_id,
        generation=2,
        audit_event_id=audit_id,
        event_id=outbox_id,
        transition="source.tombstoned",
    )
    command = CanonicalAcceptanceCommand.create(
        source=case.source,
        revision=revision,
        provenance=provenance,
        expected_current_revision_id=case.revision.revision_id,
        idempotency_key=IdempotencyKey(f"lifecycle-tombstone-{number}"),
        canonical_acceptance_id=acceptance_id,
        audit_event_id=audit_id,
        outbox_event_id=outbox_id,
        accepted_at=NOW,
        audit_event=audit,
        outbox_intent=outbox,
        operation=case.context.operation,
    )
    async with adapter.uow_factory(case.context) as uow:
        await uow.canonical.accept(command)
        await uow.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_lifecycle_authority_persists_current_scoped_decision(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    case = await _prepare_lifecycle_case(postgresql_adapter, 73100)
    context = postgresql_adapter.ingestion_context(
        case.source.workspace_id, case.source.environment_id, "lifecycle_decision"
    )
    repository = PostgreSQLLifecycleAuthorityRepository(
        uow_factory=postgresql_adapter.uow_factory,
        observation_authority=_AllowObservationAuthority(),
        decision_authority=_AllowAuthority(),
        decision_effects=_DecisionEffects(),
    )
    observation = _observation(case, 73110)
    decision = _decision(case, observation, context, 73120)

    mismatched_connection = observation.model_copy(
        update={"connection_id": synthetic_uuid(73119)}
    )
    with pytest.raises(LifecycleAuthorityError):
        await repository.record_observation(mismatched_connection, context)
    assert await repository.record_observation(observation, context) == observation
    assert await repository.record_decision(decision, context) == decision
    current = await repository.current_eligibility(_query(case, observation), context)

    assert current.current_decision_id == decision.decision_id
    assert current.generation == 1
    assert current.status is LifecycleEligibilityStatus.READ_WITHDRAWN
    # A legacy acceptance command cannot bypass a current withdrawal simply by
    # omitting the lifecycle binding from its request.
    from spine.application.persistence.errors import CanonicalEligibilityError

    with pytest.raises(CanonicalEligibilityError):
        async with postgresql_adapter.uow_factory(case.context) as uow:
            await uow.canonical.accept(case.command)
            await uow.commit()
    async with postgresql_adapter.uow_factory(context) as uow:
        audit_result = await uow._execute(  # type: ignore[attr-defined]
            select(audit_events.c.audit_event_id).where(
                audit_events.c.audit_event_id == decision.audit_event_id
            )
        )
        assert audit_result.scalar_one_or_none() == decision.audit_event_id
        await uow.rollback()

    foreign_case = await _prepare_lifecycle_case(postgresql_adapter, 73130)
    foreign_context = postgresql_adapter.ingestion_context(
        foreign_case.source.workspace_id,
        foreign_case.source.environment_id,
        "lifecycle_decision",
    )
    async with postgresql_adapter.uow_factory(foreign_context) as uow:
        rows = await uow._execute(  # type: ignore[attr-defined]
            select(lifecycle_decisions.c.decision_id).where(
                lifecycle_decisions.c.decision_id == decision.decision_id
            )
        )
        assert rows.scalar_one_or_none() is None
        await uow.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_lifecycle_authority_cas_allows_only_one_winner(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    case = await _prepare_lifecycle_case(postgresql_adapter, 73200)
    context = postgresql_adapter.ingestion_context(
        case.source.workspace_id, case.source.environment_id, "lifecycle_decision"
    )
    repository = PostgreSQLLifecycleAuthorityRepository(
        uow_factory=postgresql_adapter.uow_factory,
        observation_authority=_AllowObservationAuthority(),
        decision_authority=_AllowAuthority(),
        decision_effects=_DecisionEffects(),
    )
    observation = _observation(case, 73210)
    first = _decision(case, observation, context, 73220)
    second = _decision(case, observation, context, 73230)
    await repository.record_observation(observation, context)

    results = await asyncio.gather(
        repository.record_decision(first, context),
        repository.record_decision(second, context),
        return_exceptions=True,
    )

    assert sum(isinstance(item, LifecycleDecision) for item in results) == 1
    assert sum(isinstance(item, LifecycleConflictError) for item in results) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_lifecycle_effect_failure_rolls_back_audit_and_authority(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    case = await _prepare_lifecycle_case(postgresql_adapter, 73300)
    context = postgresql_adapter.ingestion_context(
        case.source.workspace_id, case.source.environment_id, "lifecycle_decision"
    )
    observation = _observation(case, 73310)
    decision = _decision(case, observation, context, 73320)
    repository = PostgreSQLLifecycleAuthorityRepository(
        uow_factory=postgresql_adapter.uow_factory,
        observation_authority=_AllowObservationAuthority(),
        decision_authority=_AllowAuthority(),
        decision_effects=_DecisionEffects(fail_after_audit=True),
    )
    await repository.record_observation(observation, context)

    with pytest.raises(LifecycleAuthorityError):
        await repository.record_decision(decision, context)

    async with postgresql_adapter.uow_factory(context) as uow:
        pointer = await repository.current_eligibility(_query(case, observation), context)
        audit_result = await uow._execute(  # type: ignore[attr-defined]
            select(audit_events.c.audit_event_id).where(
                audit_events.c.audit_event_id == decision.audit_event_id
            )
        )
        assert pointer.current_decision_id is None
        assert audit_result.scalar_one_or_none() is None
        await uow.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_current_eligibility_uses_tombstone_and_exact_decision_scope(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    case = await _prepare_lifecycle_case(postgresql_adapter, 73400)
    context = postgresql_adapter.ingestion_context(
        case.source.workspace_id, case.source.environment_id, "lifecycle_decision"
    )
    repository = PostgreSQLLifecycleAuthorityRepository(
        uow_factory=postgresql_adapter.uow_factory,
        observation_authority=_AllowObservationAuthority(),
        decision_authority=_AllowAuthority(),
        decision_effects=_DecisionEffects(),
    )
    observation = _observation(case, 73410)
    decision = _decision(case, observation, context, 73420)
    await repository.record_observation(observation, context)
    await repository.record_decision(decision, context)
    await _accept_canonical_tombstone(postgresql_adapter, case, 73430)

    current = await repository.current_eligibility(_query(case, observation), context)
    stale_scope = await repository.current_eligibility(
        replace(_query(case, observation), scope_digest="d" * 64), context
    )
    stale_connection_version = await repository.current_eligibility(
        replace(
            _query(case, observation),
            connection_version_id=synthetic_uuid(73419),
        ),
        context,
    )

    assert current.current_decision_id == decision.decision_id
    assert current.status is LifecycleEligibilityStatus.TOMBSTONED
    assert stale_scope.status is LifecycleEligibilityStatus.AUTHORITY_UNAVAILABLE
    assert stale_connection_version.status is LifecycleEligibilityStatus.AUTHORITY_UNAVAILABLE
