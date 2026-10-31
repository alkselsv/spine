from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from dataclasses import replace

import pytest
from sqlalchemy import insert, select, text

from spine.application.lifecycle import (
    LifecycleAuthorityError,
    LifecycleConflictError,
    LifecycleEligibilityQuery,
    require_current_read_eligibility,
)
from spine.application.persistence.canonical import CanonicalAcceptanceCommand
from spine.application.persistence.errors import CanonicalEligibilityError
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

_LIFECYCLE_ID_OFFSET = 1_000_000_000


def _lifecycle_id(number: int):
    return synthetic_uuid(_LIFECYCLE_ID_OFFSET + number)


class _LockAttemptObservedUnitOfWork:
    def __init__(
        self,
        unit_of_work,
        lock_attempted: asyncio.Event,
        backend_pids: list[int],
    ) -> None:
        self._unit_of_work = unit_of_work
        self._lock_attempted = lock_attempted
        self._backend_pids = backend_pids

    async def _execute(self, statement: object, parameters: object | None = None):
        if "pg_advisory_xact_lock" in str(statement):
            pid_result = await self._unit_of_work._execute(
                text("SELECT pg_backend_pid()")
            )
            self._backend_pids.append(pid_result.scalar_one())
            self._lock_attempted.set()
        return await self._unit_of_work._execute(statement, parameters)

    def __getattr__(self, name: str):
        return getattr(self._unit_of_work, name)


def _observe_lock_attempt(
    uow_factory,
    lock_attempted: asyncio.Event,
    backend_pids: list[int],
):
    @asynccontextmanager
    async def observed_uow_factory(context):
        async with uow_factory(context) as uow:
            yield _LockAttemptObservedUnitOfWork(
                uow, lock_attempted, backend_pids
            )

    return observed_uow_factory


async def _wait_for_postgresql_lock_wait(
    adapter: PersistenceAdapter,
    context,
    backend_pid: int,
    operation: asyncio.Task[object],
) -> bool:
    while not operation.done():
        async with adapter.uow_factory(context) as uow:
            result = await uow.execute_statement(
                text(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"
                ),
                {"pid": backend_pid},
            )
            wait_event_type = result.scalar_one_or_none()
            await uow.rollback()
        if wait_event_type == "Lock":
            return True
        await asyncio.sleep(0)
    return False


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
        observation_id=_lifecycle_id(base),
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        source_object_id=case.source.source_object_id,
        connection_id=case.source.connection_id,
        connection_version_id=_lifecycle_id(base + 2),
        authorization_generation=1,
        configuration_generation=1,
        scope_reference=_lifecycle_id(base + 3),
        scope_version_id=_lifecycle_id(base + 4),
        scope_digest="a" * 64,
        provider_identity_reference=_lifecycle_id(base + 5),
        kind=LifecycleObservationKind.SCOPE_WITHDRAWN,
        evidence_reference=_lifecycle_id(base + 6),
        evidence_digest="b" * 64,
        confidence=LifecycleObservationConfidence.QUALIFIED,
        reason_code=LifecycleReasonCode.SCOPE_WITHDRAWN,
        observed_at=datetime(2026, 10, 31, tzinfo=timezone.utc),
    )


def _decision(case, observation: LifecycleObservation, context, base: int) -> LifecycleDecision:
    return LifecycleDecision(
        decision_id=_lifecycle_id(base),
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
        authority_reference=_lifecycle_id(base + 1),
        authority_version_id=_lifecycle_id(base + 2),
        authority_digest="c" * 64,
        authority_kind=LifecycleAuthorityKind.SOURCE_GOVERNANCE,
        status=LifecycleEligibilityStatus.READ_WITHDRAWN,
        legal_hold_active=False,
        expected_current_decision_id=None,
        expected_current_generation=0,
        generation=1,
        actor_id=context.service_principal_id,
        audit_event_id=_lifecycle_id(base + 3),
        outbox_event_id=_lifecycle_id(base + 4),
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
        source_external_namespace=case.source.external_namespace,
        source_external_generation=case.source.external_generation,
        source_external_object_id=case.source.external_object_id,
    )


async def _prepare_lifecycle_case(adapter: PersistenceAdapter, number: int):
    workspace_id = synthetic_uuid(number)
    environment_id = synthetic_uuid(number + 1)
    source = connector_source(workspace_id, environment_id, "provider1")
    return await prepare_canonical_case(adapter, number, source_object=source)


async def _prepare_scope_authority(
    adapter: PersistenceAdapter,
    child_case,
    child_observation: LifecycleObservation,
    repository: PostgreSQLLifecycleAuthorityRepository,
    context,
    base: int,
):
    root_source = connector_source(
        child_case.source.workspace_id,
        child_case.source.environment_id,
        f"provider{base % 10}",
    )
    root_case = await prepare_canonical_case(
        adapter,
        base + 1_000_000_001,
        scope=(child_case.source.workspace_id, child_case.source.environment_id),
        source_object=root_source,
        create_scope=False,
        provenance_event="event-2",
    )
    root_observation = _observation(root_case, base + 1).model_copy(
        update={
            "connection_id": child_observation.connection_id,
            "connection_version_id": child_observation.connection_version_id,
            "scope_reference": child_observation.scope_reference,
            "scope_version_id": child_observation.scope_version_id,
            "scope_digest": child_observation.scope_digest,
            "kind": LifecycleObservationKind.MOVE_WITHIN_SCOPE,
            "reason_code": LifecycleReasonCode.MOVE_WITHIN_SCOPE,
        }
    )
    root_decision = _decision(root_case, root_observation, context, base + 2).model_copy(
        update={
            "effect": LifecycleDecisionEffect.ELIGIBLE,
            "status": LifecycleEligibilityStatus.ELIGIBLE,
        }
    )
    await repository.record_observation(root_observation, context)
    await repository.record_decision(root_decision, context)
    return root_case, root_decision


def _bind_scope_authority(query: LifecycleEligibilityQuery, root_case) -> LifecycleEligibilityQuery:
    return replace(
        query,
        scope_authority_source_object_id=root_case.source.source_object_id,
        scope_authority_external_namespace=root_case.source.external_namespace,
        scope_authority_external_generation=root_case.source.external_generation,
        scope_authority_external_object_id=root_case.source.external_object_id,
    )


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
async def test_postgresql_withdrawal_serializes_after_inflight_canonical_acceptance(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    case = await _prepare_lifecycle_case(postgresql_adapter, 73240)
    context = postgresql_adapter.ingestion_context(
        case.source.workspace_id, case.source.environment_id, "lifecycle_decision"
    )
    observation = _observation(case, 73250)
    lock_attempted = asyncio.Event()
    backend_pids: list[int] = []
    repository = PostgreSQLLifecycleAuthorityRepository(
        uow_factory=_observe_lock_attempt(
            postgresql_adapter.uow_factory, lock_attempted, backend_pids
        ),
        observation_authority=_AllowObservationAuthority(),
        decision_authority=_AllowAuthority(),
        decision_effects=_DecisionEffects(),
    )
    await repository.record_observation(observation, context)
    root_case, root_eligible_decision = await _prepare_scope_authority(
        postgresql_adapter, case, observation, repository, context, 73270
    )
    root_observation = _observation(root_case, 73280).model_copy(
        update={
            "connection_id": observation.connection_id,
            "connection_version_id": observation.connection_version_id,
            "scope_reference": observation.scope_reference,
            "scope_version_id": observation.scope_version_id,
            "scope_digest": observation.scope_digest,
            "kind": LifecycleObservationKind.SCOPE_WITHDRAWN,
            "reason_code": LifecycleReasonCode.SCOPE_WITHDRAWN,
        }
    )
    root_decision = _decision(root_case, root_observation, context, 73290).model_copy(
        update={
            "expected_current_decision_id": root_eligible_decision.decision_id,
            "expected_current_generation": root_eligible_decision.generation,
            "generation": root_eligible_decision.generation + 1,
        }
    )
    await repository.record_observation(root_observation, context)
    child_query = _bind_scope_authority(_query(case, observation), root_case)

    async with postgresql_adapter.uow_factory(case.context) as uow:
        acceptance_command = CanonicalAcceptanceCommand.create(
            source=case.command.source,
            revision=case.command.revision,
            provenance=case.command.provenance,
            admission_decision_id=case.command.admission_decision_id,
            access_policy_version_id=case.command.access_policy_version_id,
            expected_current_revision_id=case.command.expected_current_revision_id,
            idempotency_key=case.command.idempotency_key,
            canonical_acceptance_id=case.command.canonical_acceptance_id,
            audit_event_id=case.command.audit_event_id,
            outbox_event_id=case.command.outbox_event_id,
            accepted_at=case.command.accepted_at,
            audit_event=case.command.audit_event,
            outbox_intent=case.command.outbox_intent,
            lifecycle_query=child_query,
            operation=case.command.digest.operation,
        )
        acceptance = await uow.canonical.accept(acceptance_command)
        lock_attempted.clear()
        withdrawal = asyncio.create_task(
            repository.record_decision(root_decision, context)
        )
        lock_waiter = asyncio.create_task(lock_attempted.wait())
        completed, _ = await asyncio.wait(
            {withdrawal, lock_waiter}, return_when=asyncio.FIRST_COMPLETED
        )
        assert lock_waiter in completed
        assert await _wait_for_postgresql_lock_wait(
            postgresql_adapter,
            context,
            backend_pids[0],
            withdrawal,
        )
        assert not withdrawal.done()
        await uow.commit()

    assert acceptance.advanced is True
    assert await withdrawal == root_decision
    current = await repository.current_eligibility(child_query, context)
    assert current.status is LifecycleEligibilityStatus.AUTHORITY_UNAVAILABLE
    from spine.application.persistence.errors import CanonicalEligibilityError

    unbound_source = connector_source(
        case.source.workspace_id,
        case.source.environment_id,
        "provider2",
    )
    unbound_case = await prepare_canonical_case(
        postgresql_adapter,
        1_732_400_001,
        scope=(case.source.workspace_id, case.source.environment_id),
        source_object=unbound_source,
        create_scope=False,
        provenance_event="event-3",
    )
    with pytest.raises(CanonicalEligibilityError):
        async with postgresql_adapter.uow_factory(unbound_case.context) as uow:
            await uow.canonical.accept(unbound_case.command)
            await uow.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_new_source_is_eligible_before_its_first_lifecycle_decision(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    case = await _prepare_lifecycle_case(postgresql_adapter, 73500)
    context = postgresql_adapter.ingestion_context(
        case.source.workspace_id, case.source.environment_id, "provider_connection_read"
    )
    repository = PostgreSQLLifecycleAuthorityRepository(
        uow_factory=postgresql_adapter.uow_factory,
        observation_authority=_AllowObservationAuthority(),
        decision_authority=_AllowAuthority(),
        decision_effects=_DecisionEffects(),
    )
    observation = _observation(case, 73510)
    root_case, root_eligible_decision = await _prepare_scope_authority(
        postgresql_adapter,
        case,
        observation,
        repository,
        context,
        73530,
    )
    query = _bind_scope_authority(replace(
        _query(case, observation),
        source_object_id=_lifecycle_id(73520),
        source_external_object_id="provider-object:new-source",
    ), root_case)

    current = await repository.current_eligibility(query, context)
    admitted = await require_current_read_eligibility(repository, query, context)

    assert current.current_decision_id is None
    assert current.generation == 0
    assert current.status is LifecycleEligibilityStatus.ELIGIBLE
    assert admitted == current
    withdrawal_observation = _observation(root_case, 73540).model_copy(
        update={
            "connection_id": observation.connection_id,
            "connection_version_id": observation.connection_version_id,
            "scope_reference": observation.scope_reference,
            "scope_version_id": observation.scope_version_id,
            "scope_digest": observation.scope_digest,
            "kind": LifecycleObservationKind.SCOPE_WITHDRAWN,
            "reason_code": LifecycleReasonCode.SCOPE_WITHDRAWN,
        }
    )
    withdrawal_decision = _decision(
        root_case, withdrawal_observation, context, 73550
    ).model_copy(
        update={
            "expected_current_decision_id": root_eligible_decision.decision_id,
            "expected_current_generation": root_eligible_decision.generation,
            "generation": root_eligible_decision.generation + 1,
        }
    )
    await repository.record_observation(withdrawal_observation, context)
    await repository.record_decision(withdrawal_decision, context)
    withdrawn = await repository.current_eligibility(query, context)
    assert withdrawn.status is LifecycleEligibilityStatus.AUTHORITY_UNAVAILABLE
    child_query = _bind_scope_authority(_query(case, observation), root_case)
    with pytest.raises(CanonicalEligibilityError):
        async with postgresql_adapter.uow_factory(case.context) as uow:
            await uow.canonical.accept(
                replace(
                    case.command,
                    idempotency_key=IdempotencyKey("root-withdrawal-first"),
                    lifecycle_query=child_query,
                )
            )


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
    await _accept_canonical_tombstone(postgresql_adapter, case, 73430)
    await repository.record_observation(observation, context)
    await repository.record_decision(decision, context)

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
    stale_source_identity = await repository.current_eligibility(
        replace(
            _query(case, observation),
            source_external_object_id="provider-object:stale",
        ),
        context,
    )

    assert current.current_decision_id == decision.decision_id
    assert current.status is LifecycleEligibilityStatus.TOMBSTONED
    assert stale_scope.status is LifecycleEligibilityStatus.AUTHORITY_UNAVAILABLE
    assert stale_connection_version.status is LifecycleEligibilityStatus.AUTHORITY_UNAVAILABLE
    assert stale_source_identity.status is LifecycleEligibilityStatus.AUTHORITY_UNAVAILABLE
