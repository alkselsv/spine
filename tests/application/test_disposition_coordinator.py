from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from spine.application.disposition_coordinator import (
    DispositionCoordinator,
    DispositionConflictError,
    DispositionRequest,
    StoreOutcome,
)
from spine.domain.sources.disposition import (
    DispositionOperation,
    DispositionStatus,
    DispositionStoreState,
    StoreEffectPermitStatus,
    StoreDispositionStatus,
)


WORKSPACE_A = UUID("00000000-0000-0000-0000-000000000001")
WORKSPACE_B = UUID("00000000-0000-0000-0000-000000000002")
ENVIRONMENT = UUID("00000000-0000-0000-0000-000000000010")
OPERATION = UUID("00000000-0000-0000-0000-000000000100")
DIGEST = "a" * 64


class MemoryOperationRepository:
    def __init__(self) -> None:
        self.items: dict[tuple[UUID, UUID, UUID], DispositionOperation] = {}
        self.completed_audits = 0
        self.completion_started: asyncio.Event | None = None
        self.completion_release: asyncio.Event | None = None
        self.lose_completion_response_once = False

    async def create_if_absent(self, operation: DispositionOperation) -> DispositionOperation:
        key = _key(operation)
        current = self.items.get(key)
        if current is not None:
            if current.request_digest != operation.request_digest:
                raise DispositionConflictError("request digest changed")
            if set(current.declared_store_ids) != set(operation.declared_store_ids):
                raise DispositionConflictError("declared store coverage changed")
            if tuple(item.store_id for item in current.stores) != tuple(
                item.store_id for item in operation.stores
            ):
                raise DispositionConflictError("store coverage changed")
            return current
        self.items[key] = operation
        return operation

    async def save(
        self,
        operation: DispositionOperation,
        *,
        expected_state_version: int,
    ) -> DispositionOperation:
        current = self.items[_key(operation)]
        if current.state_version != expected_state_version:
            raise DispositionConflictError("concurrent state update")
        assert operation.state_version == expected_state_version + 1
        self.items[_key(operation)] = operation
        return operation

    async def complete(
        self,
        operation: DispositionOperation,
        *,
        expected_state_version: int,
    ) -> DispositionOperation:
        assert operation.status is DispositionStatus.VERIFIED
        assert operation.fence_active is True
        if self.completion_started is not None:
            self.completion_started.set()
        if self.completion_release is not None:
            await self.completion_release.wait()
        current = self.items[_key(operation)]
        if (
            current.state_version != expected_state_version
            or current.status is DispositionStatus.CANCELLED
        ):
            raise DispositionConflictError("concurrent completion")
        if current.status is not DispositionStatus.VERIFIED:
            self.completed_audits += 1
        stored = operation.model_copy(update={"state_version": expected_state_version + 1})
        self.items[_key(operation)] = stored
        if self.lose_completion_response_once:
            self.lose_completion_response_once = False
            raise TimeoutError("completion committed but response was lost")
        return stored

    async def forget_completed(
        self,
        operation: DispositionOperation,
        *,
        expected_state_version: int,
    ) -> None:
        current = self.items[_key(operation)]
        if (
            current.state_version != expected_state_version
            or current.status is not DispositionStatus.VERIFIED
        ):
            raise DispositionConflictError("completion is not current")
        del self.items[_key(operation)]

    async def expire_completed(
        self, *, workspace_id: UUID, environment_id: UUID, evaluated_at: datetime
    ) -> int:
        expired_keys = [
            key
            for key, operation in self.items.items()
            if (
                operation.workspace_id == workspace_id
                and operation.environment_id == environment_id
                and operation.status is DispositionStatus.VERIFIED
                and operation.completion_ack_expires_at is not None
                and operation.completion_ack_expires_at <= evaluated_at
            )
        ]
        for key in expired_keys:
            del self.items[key]
        return len(expired_keys)

    async def get(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        operation_id: UUID,
    ) -> DispositionOperation | None:
        return self.items.get((workspace_id, environment_id, operation_id))

class CurrentDispositionAdmission:
    def __init__(self, *, current: bool = True) -> None:
        self.current = current
        self.checks = 0

    async def fence_or_get(
        self,
        operation: DispositionOperation,
    ) -> DispositionOperation | None:
        if not self.current:
            return None
        return await self.repository.create_if_absent(operation)

    repository: MemoryOperationRepository

    def bind(self, repository: MemoryOperationRepository) -> "CurrentDispositionAdmission":
        self.repository = repository
        return self

    async def is_current(self, operation: DispositionOperation) -> bool:
        assert operation.fence_active
        self.checks += 1
        return self.current

    async def store_effect_permit(
        self,
        operation: DispositionOperation,
        store_id: str,
    ) -> "FakeStoreEffectPermit":
        del operation, store_id
        return FakeStoreEffectPermit(self.current)

    async def rebind_authority(
        self,
        operation: DispositionOperation,
        new_binding,
    ) -> DispositionOperation | None:
        del operation, new_binding
        return None


class FakeStoreEffectPermit:
    def __init__(self, permitted: bool) -> None:
        self.permitted = permitted

    async def __aenter__(self) -> StoreEffectPermitStatus:
        return (
            StoreEffectPermitStatus.ALLOWED
            if self.permitted
            else StoreEffectPermitStatus.AUTHORITY_NOT_CURRENT
        )

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class ScriptedStore:
    def __init__(self, *outcomes: str) -> None:
        self.outcomes = list(outcomes)
        self.dispose_calls = 0
        self.reconcile_calls = 0

    async def dispose(self, operation: DispositionOperation) -> str:
        assert operation.fence_active
        self.dispose_calls += 1
        return self.outcomes.pop(0) if self.outcomes else StoreOutcome.VERIFIED

    async def reconcile(self, operation: DispositionOperation) -> str:
        assert operation.fence_active
        self.reconcile_calls += 1
        return self.outcomes.pop(0) if self.outcomes else StoreOutcome.VERIFIED


class CommitThenRaiseStore:
    def __init__(self) -> None:
        self.committed_effects = 0
        self.reconcile_calls = 0

    async def dispose(self, operation: DispositionOperation) -> str:
        assert operation.fence_active
        self.committed_effects += 1
        raise TimeoutError("response lost after commit")

    async def reconcile(self, operation: DispositionOperation) -> str:
        assert operation.fence_active
        self.reconcile_calls += 1
        return StoreOutcome.VERIFIED


class CommitThenCancelStore:
    def __init__(self) -> None:
        self.committed_effects = 0
        self.reconcile_calls = 0

    async def dispose(self, operation: DispositionOperation) -> str:
        self.committed_effects += 1
        raise asyncio.CancelledError

    async def reconcile(self, operation: DispositionOperation) -> str:
        self.reconcile_calls += 1
        return StoreOutcome.VERIFIED


class BlockingStore:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def dispose(self, operation: DispositionOperation) -> str:
        self.started.set()
        await self.release.wait()
        return StoreOutcome.VERIFIED

    async def reconcile(self, operation: DispositionOperation) -> str:
        return StoreOutcome.VERIFIED


def _request(
    *,
    workspace_id: UUID = WORKSPACE_A,
    environment_id: UUID = ENVIRONMENT,
    digest: str = DIGEST,
    stores: tuple[str, ...] = ("canonical", "object"),
) -> DispositionRequest:
    return DispositionRequest(
        workspace_id=workspace_id,
        environment_id=environment_id,
        operation_id=OPERATION,
        request_digest=digest,
        store_ids=stores,
    )


def _key(operation: DispositionOperation) -> tuple[UUID, UUID, UUID]:
    return operation.workspace_id, operation.environment_id, operation.operation_id


def _coordinator(
    repository: MemoryOperationRepository,
    stores: Mapping[str, ScriptedStore],
    authority: CurrentDispositionAdmission | None = None,
    clock: FixedClock | None = None,
) -> DispositionCoordinator:
    return DispositionCoordinator(
        repository=repository,
        admission=(authority or CurrentDispositionAdmission()).bind(repository),
        stores=stores,
        clock=clock,
    )


@pytest.mark.asyncio
async def test_disposition_partial_failure_retries_same_operation_idempotently() -> None:
    repository = MemoryOperationRepository()
    canonical = ScriptedStore(StoreOutcome.VERIFIED)
    object_store = ScriptedStore(StoreOutcome.FAILED, StoreOutcome.VERIFIED)
    coordinator = _coordinator(
        repository,
        {"canonical": canonical, "object": object_store},
    )

    first = await coordinator.execute(_request())
    second = await coordinator.execute(_request())

    assert first.status is DispositionStatus.INCOMPLETE
    assert first.fence_active is True
    assert second.status is DispositionStatus.VERIFIED
    assert second.fence_active is True
    assert canonical.dispose_calls == 1
    assert object_store.dispose_calls == 2
    await coordinator.acknowledge_completion(second)
    assert repository.items == {}


@pytest.mark.asyncio
async def test_disposition_operation_identity_isolated_by_workspace() -> None:
    repository = MemoryOperationRepository()
    stores = {"canonical": ScriptedStore(), "object": ScriptedStore()}
    coordinator = _coordinator(repository, stores)

    first = await coordinator.execute(_request(workspace_id=WORKSPACE_A))
    second = await coordinator.execute(_request(workspace_id=WORKSPACE_B))
    third = await coordinator.execute(
        _request(workspace_id=WORKSPACE_A, environment_id=UUID("00000000-0000-0000-0000-000000000011"))
    )

    assert first.status is DispositionStatus.VERIFIED
    assert second.status is DispositionStatus.VERIFIED
    assert third.status is DispositionStatus.VERIFIED
    assert first.fence_active is True
    assert second.fence_active is True
    assert third.fence_active is True
    await coordinator.acknowledge_completion(first)
    await coordinator.acknowledge_completion(second)
    await coordinator.acknowledge_completion(third)
    assert repository.items == {}
    assert repository.completed_audits == 3


@pytest.mark.asyncio
async def test_disposition_unknown_commit_requires_explicit_reconciliation() -> None:
    repository = MemoryOperationRepository()
    canonical = ScriptedStore(StoreOutcome.UNKNOWN, StoreOutcome.VERIFIED)
    object_store = ScriptedStore(StoreOutcome.VERIFIED)
    coordinator = _coordinator(repository, {"canonical": canonical, "object": object_store})

    unknown = await coordinator.execute(_request())
    reconciled = await coordinator.reconcile(_request())

    assert unknown.status is DispositionStatus.UNKNOWN
    assert unknown.fence_active is True
    assert reconciled.status is DispositionStatus.VERIFIED
    assert reconciled.fence_active is True
    assert canonical.dispose_calls == 1
    assert canonical.reconcile_calls == 1
    assert object_store.dispose_calls == 1
    await coordinator.acknowledge_completion(reconciled)


@pytest.mark.asyncio
async def test_disposition_lost_store_response_reconciles_without_repeating_effect() -> None:
    repository = MemoryOperationRepository()
    canonical = CommitThenRaiseStore()
    object_store = ScriptedStore(StoreOutcome.VERIFIED)
    coordinator = _coordinator(repository, {"canonical": canonical, "object": object_store})

    unknown = await coordinator.execute(_request())
    reconciled = await coordinator.reconcile(_request())

    assert unknown.status is DispositionStatus.UNKNOWN
    assert reconciled.status is DispositionStatus.VERIFIED
    assert reconciled.fence_active is True
    assert canonical.committed_effects == 1
    assert canonical.reconcile_calls == 1
    assert object_store.dispose_calls == 1
    await coordinator.acknowledge_completion(reconciled)


@pytest.mark.asyncio
async def test_disposition_lost_completion_response_replays_without_store_work() -> None:
    repository = MemoryOperationRepository()
    repository.lose_completion_response_once = True
    canonical = ScriptedStore()
    object_store = ScriptedStore()
    coordinator = _coordinator(repository, {"canonical": canonical, "object": object_store})

    first = await coordinator.execute(_request())
    replay = await coordinator.execute(_request())

    assert first.status is DispositionStatus.VERIFIED
    assert replay.status is DispositionStatus.VERIFIED
    assert first.fence_active is True
    assert replay.fence_active is True
    assert repository.completed_audits == 1
    assert canonical.dispose_calls == 1
    assert object_store.dispose_calls == 1
    await coordinator.acknowledge_completion(replay)
    assert repository.items == {}


@pytest.mark.asyncio
async def test_disposition_unacknowledged_completion_expires_operation_identity() -> None:
    repository = MemoryOperationRepository()
    clock = FixedClock(datetime(2030, 1, 1, tzinfo=timezone.utc))
    coordinator = _coordinator(
        repository,
        {"canonical": ScriptedStore(), "object": ScriptedStore()},
        clock=clock,
    )

    completed = await coordinator.execute(_request())
    assert completed.completion_ack_expires_at == clock.now + timedelta(hours=24)
    assert repository.items

    clock.now = completed.completion_ack_expires_at + timedelta(microseconds=1)
    purged = await coordinator.expire_completed_operations(
        workspace_id=completed.workspace_id,
        environment_id=completed.environment_id,
    )

    assert purged == 1
    assert repository.items == {}
    assert repository.completed_audits == 1


@pytest.mark.asyncio
async def test_disposition_completion_replay_rechecks_store_registry() -> None:
    repository = MemoryOperationRepository()
    repository.lose_completion_response_once = True
    admission = CurrentDispositionAdmission()
    clock = FixedClock(datetime(2030, 1, 1, tzinfo=timezone.utc))
    canonical = ScriptedStore()
    object_store = ScriptedStore()
    coordinator = _coordinator(
        repository,
        {"canonical": canonical, "object": object_store},
        admission,
        clock,
    )

    completed = await coordinator.execute(_request())
    changed_coordinator = DispositionCoordinator(
        repository=repository,
        admission=admission,
        stores={
            "canonical": canonical,
            "object": object_store,
            "backup": ScriptedStore(),
        },
        clock=clock,
    )
    replay = await changed_coordinator.execute(_request())

    assert completed.status is DispositionStatus.VERIFIED
    assert replay.status is DispositionStatus.UNKNOWN
    assert replay.safe_code == "UNKNOWN_STORE"
    assert replay.fence_active is True
    assert canonical.dispose_calls == 1
    assert object_store.dispose_calls == 1
    with pytest.raises(DispositionConflictError, match="not been verified"):
        await changed_coordinator.acknowledge_completion(replay)
    assert replay.completion_ack_expires_at is not None
    clock.now = replay.completion_ack_expires_at + timedelta(microseconds=1)
    assert await changed_coordinator.expire_completed_operations(
        workspace_id=replay.workspace_id,
        environment_id=replay.environment_id,
    ) == 0
    assert repository.items
    assert repository.completed_audits == 1


@pytest.mark.asyncio
async def test_disposition_reconcile_checks_registry_after_store_failure() -> None:
    repository = MemoryOperationRepository()
    admission = CurrentDispositionAdmission()
    canonical = ScriptedStore(StoreOutcome.FAILED)
    object_store = ScriptedStore()
    coordinator = _coordinator(repository, {"canonical": canonical, "object": object_store}, admission)

    incomplete = await coordinator.execute(_request())
    changed_coordinator = DispositionCoordinator(
        repository=repository,
        admission=admission,
        stores={
            "canonical": canonical,
            "object": object_store,
            "backup": ScriptedStore(),
        },
    )
    reconciled = await changed_coordinator.reconcile(_request())

    assert incomplete.status is DispositionStatus.INCOMPLETE
    assert reconciled.status is DispositionStatus.UNKNOWN
    assert reconciled.safe_code == "UNKNOWN_STORE"
    assert reconciled.fence_active is True


@pytest.mark.asyncio
async def test_disposition_task_cancellation_reconciles_in_progress_store() -> None:
    repository = MemoryOperationRepository()
    canonical = CommitThenCancelStore()
    object_store = ScriptedStore(StoreOutcome.VERIFIED)
    coordinator = _coordinator(repository, {"canonical": canonical, "object": object_store})

    with pytest.raises(asyncio.CancelledError):
        await coordinator.execute(_request())

    unresolved = await repository.get(
        workspace_id=WORKSPACE_A,
        environment_id=ENVIRONMENT,
        operation_id=OPERATION,
    )
    assert unresolved is not None
    assert unresolved.stores[0].status is StoreDispositionStatus.IN_PROGRESS
    assert unresolved.stores[0].attempts == 1

    result = await coordinator.execute(_request())

    assert result.status is DispositionStatus.VERIFIED
    assert canonical.committed_effects == 1
    assert canonical.reconcile_calls == 1
    assert object_store.dispose_calls == 1


@pytest.mark.asyncio
async def test_disposition_reconciliation_rejects_changed_store_registry() -> None:
    repository = MemoryOperationRepository()
    admission = CurrentDispositionAdmission()
    canonical = ScriptedStore(StoreOutcome.UNKNOWN)
    object_store = ScriptedStore()
    first_coordinator = _coordinator(
        repository,
        {"canonical": canonical, "object": object_store},
        admission,
    )

    unknown = await first_coordinator.execute(_request())
    changed_coordinator = DispositionCoordinator(
        repository=repository,
        admission=admission,
        stores={
            "canonical": canonical,
            "object": object_store,
            "backup": ScriptedStore(),
        },
    )
    reconciled = await changed_coordinator.reconcile(_request())

    assert unknown.status is DispositionStatus.UNKNOWN
    assert reconciled.status is DispositionStatus.UNKNOWN
    assert reconciled.safe_code == "UNKNOWN_STORE"
    assert reconciled.fence_active is True
    assert {store.store_id for store in reconciled.stores} == {
        "backup",
        "canonical",
        "object",
    }


@pytest.mark.asyncio
async def test_disposition_unknown_store_never_reports_verified() -> None:
    repository = MemoryOperationRepository()
    coordinator = _coordinator(repository, {"canonical": ScriptedStore()})

    result = await coordinator.execute(_request(stores=("canonical", "object")))

    assert result.status is DispositionStatus.UNKNOWN
    assert result.safe_code == "UNKNOWN_STORE"
    assert result.fence_active is True
    assert {store.store_id for store in result.stores} == {"canonical", "object"}
    assert next(store for store in result.stores if store.store_id == "object").status is StoreDispositionStatus.UNKNOWN


@pytest.mark.asyncio
async def test_disposition_cancellation_keeps_fence_and_stops_retries() -> None:
    repository = MemoryOperationRepository()
    canonical = ScriptedStore(StoreOutcome.FAILED)
    object_store = ScriptedStore()
    coordinator = _coordinator(repository, {"canonical": canonical, "object": object_store})

    incomplete = await coordinator.execute(_request())
    calls_before_cancel = object_store.dispose_calls
    cancelled = await coordinator.cancel(incomplete)
    replayed = await coordinator.execute(_request())

    assert cancelled.status is DispositionStatus.CANCELLED
    assert cancelled.fence_active is True
    assert replayed.status is DispositionStatus.CANCELLED
    assert object_store.dispose_calls == calls_before_cancel


@pytest.mark.asyncio
async def test_disposition_cancellation_racing_store_stops_later_store_work() -> None:
    repository = MemoryOperationRepository()
    canonical = BlockingStore()
    object_store = ScriptedStore()
    coordinator = _coordinator(repository, {"canonical": canonical, "object": object_store})

    task = asyncio.create_task(coordinator.execute(_request()))
    await canonical.started.wait()
    current = await repository.get(
        workspace_id=WORKSPACE_A,
        environment_id=ENVIRONMENT,
        operation_id=OPERATION,
    )
    assert current is not None
    cancelled = await coordinator.cancel(current)
    canonical.release.set()
    result = await task

    assert cancelled.status is DispositionStatus.CANCELLED
    assert result.status is DispositionStatus.CANCELLED
    assert result.fence_active is True
    assert object_store.dispose_calls == 0


@pytest.mark.asyncio
async def test_disposition_cancellation_wins_race_with_finalization() -> None:
    repository = MemoryOperationRepository()
    repository.completion_started = asyncio.Event()
    repository.completion_release = asyncio.Event()
    coordinator = _coordinator(
        repository,
        {"canonical": ScriptedStore(), "object": ScriptedStore()},
    )

    task = asyncio.create_task(coordinator.execute(_request()))
    await repository.completion_started.wait()
    current = await repository.get(
        workspace_id=WORKSPACE_A,
        environment_id=ENVIRONMENT,
        operation_id=OPERATION,
    )
    assert current is not None
    cancelled = await coordinator.cancel(current)
    repository.completion_release.set()
    result = await task

    assert cancelled.status is DispositionStatus.CANCELLED
    assert result.status is DispositionStatus.CANCELLED
    assert result.fence_active is True
    assert repository.completed_audits == 0
    assert await repository.get(
        workspace_id=WORKSPACE_A,
        environment_id=ENVIRONMENT,
        operation_id=OPERATION,
    ) is not None


@pytest.mark.asyncio
async def test_disposition_authority_loss_leaves_persisted_fence_incomplete() -> None:
    repository = MemoryOperationRepository()
    authority = CurrentDispositionAdmission(current=False)
    coordinator = _coordinator(
        repository,
        {"canonical": ScriptedStore(), "object": ScriptedStore()},
        authority,
    )

    result = await coordinator.execute(_request())

    assert result.status is DispositionStatus.BLOCKED
    assert result.safe_code == "AUTHORITY_NOT_CURRENT"
    assert result.fence_active is False
    assert repository.items == {}


@pytest.mark.asyncio
async def test_disposition_revoked_retry_retains_existing_fence() -> None:
    repository = MemoryOperationRepository()
    authority = CurrentDispositionAdmission()
    canonical = ScriptedStore(StoreOutcome.FAILED)
    object_store = ScriptedStore()
    coordinator = _coordinator(repository, {"canonical": canonical, "object": object_store}, authority)

    first = await coordinator.execute(_request())
    authority.current = False
    retry = await coordinator.execute(_request())

    assert first.status is DispositionStatus.INCOMPLETE
    assert retry.status is DispositionStatus.INCOMPLETE
    assert retry.safe_code == "AUTHORITY_NOT_CURRENT"
    assert retry.fence_active is True
    assert canonical.dispose_calls == 1
    assert object_store.dispose_calls == 1


def test_verified_disposition_requires_every_store_to_verify() -> None:
    with pytest.raises(ValueError, match="every store to verify"):
        DispositionOperation(
            workspace_id=WORKSPACE_A,
            environment_id=ENVIRONMENT,
            operation_id=OPERATION,
            request_digest=DIGEST,
        declared_store_ids=("canonical", "object"),
            status=DispositionStatus.VERIFIED,
            fence_active=True,
            safe_code="DISPOSITION_VERIFIED",
            stores=(
                DispositionStoreState(
                    store_id="canonical",
                    status=StoreDispositionStatus.VERIFIED,
                    attempts=1,
                    result_code="STORE_VERIFIED",
                ),
                DispositionStoreState(store_id="object"),
            ),
        )
