from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from spine.application.disposition_authority import (
    CurrentDispositionAdmission,
    disposition_authority_snapshot_matches,
)
from spine.application.disposition_coordinator import (
    DispositionCoordinator,
    DispositionConflictError,
    DispositionConflictError,
    DispositionRequest,
    StoreOutcome,
)
from spine.domain.sources.disposition import (
    DispositionAuthorityBinding,
    DispositionAuthoritySnapshot,
    DispositionOperation,
    DispositionStatus,
    DispositionStoreState,
    StoreEffectPermitStatus,
)


WORKSPACE = UUID("00000000-0000-0000-0000-000000000001")
ENVIRONMENT = UUID("00000000-0000-0000-0000-000000000002")
OPERATION = UUID("00000000-0000-0000-0000-000000000003")
SOURCE = UUID("00000000-0000-0000-0000-000000000004")
REVISION = UUID("00000000-0000-0000-0000-000000000005")
ACCEPTANCE = UUID("00000000-0000-0000-0000-000000000006")
AUTHORIZATION = UUID("00000000-0000-0000-0000-000000000007")
POLICY = UUID("00000000-0000-0000-0000-000000000008")
POLICY_VERSION = UUID("00000000-0000-0000-0000-000000000009")
ISSUER = UUID("00000000-0000-0000-0000-000000000010")
NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
DIGEST = "a" * 64


def _binding(*, operation_id: UUID = OPERATION) -> DispositionAuthorityBinding:
    return DispositionAuthorityBinding(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        operation_id=operation_id,
        source_object_id=SOURCE,
        expected_revision_id=REVISION,
        expected_acceptance_id=ACCEPTANCE,
        expected_generation=4,
        tombstone_authorization_id=AUTHORIZATION,
        authorization_digest=DIGEST,
        deletion_policy_approval_id=POLICY,
        deletion_policy_version_id=POLICY_VERSION,
        deletion_policy_digest="b" * 64,
        deletion_policy_generation=7,
        retention_eligibility_reference=UUID("00000000-0000-0000-0000-000000000011"),
        retention_eligibility_generation=2,
        issuer_authority_id=ISSUER,
        issuer_generation=3,
        expires_at=NOW + timedelta(hours=1),
    )


def _snapshot(**updates: object) -> DispositionAuthoritySnapshot:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE,
        "environment_id": ENVIRONMENT,
        "source_object_id": SOURCE,
        "current_revision_id": REVISION,
        "current_acceptance_id": ACCEPTANCE,
        "current_generation": 4,
        "tombstone_authorization_id": AUTHORIZATION,
        "authorization_digest": DIGEST,
        "authorization_active": True,
        "deletion_policy_approval_id": POLICY,
        "deletion_policy_version_id": POLICY_VERSION,
        "deletion_policy_digest": "b" * 64,
        "deletion_policy_generation": 7,
        "deletion_policy_active": True,
        "retention_eligibility_reference": UUID("00000000-0000-0000-0000-000000000011"),
        "retention_eligibility_generation": 2,
        "retention_eligibility_active": True,
        "issuer_authority_id": ISSUER,
        "issuer_generation": 3,
        "issuer_active": True,
        "read_processing_revoked": False,
    }
    values.update(updates)
    return DispositionAuthoritySnapshot(**values)


def _fresh_binding() -> DispositionAuthorityBinding:
    return _binding().model_copy(
        update={
            "tombstone_authorization_id": UUID("00000000-0000-0000-0000-000000000012"),
            "authorization_digest": "d" * 64,
            "deletion_policy_approval_id": UUID("00000000-0000-0000-0000-000000000013"),
            "deletion_policy_version_id": UUID("00000000-0000-0000-0000-000000000014"),
            "deletion_policy_digest": "c" * 64,
            "deletion_policy_generation": 8,
        }
    )


class MemoryDispositionRuntime:
    """Serializes revocation and fence admission under one deterministic lock."""

    def __init__(self, snapshot: DispositionAuthoritySnapshot | None = None) -> None:
        self.snapshot = snapshot or _snapshot()
        self.now = NOW
        self.operations: dict[tuple[UUID, UUID, UUID], DispositionOperation] = {}
        self.lock = asyncio.Lock()
        self.pause_admission = False
        self.admission_checked: asyncio.Event | None = None
        self.admission_release: asyncio.Event | None = None
        self.pause_store_permit = False
        self.store_permit_checked: asyncio.Event | None = None
        self.store_permit_release: asyncio.Event | None = None
        self.pause_rebind = False
        self.rebind_checked: asyncio.Event | None = None
        self.rebind_release: asyncio.Event | None = None
        self.lose_admission_response_once = False

    async def admit_if_current(
        self,
        operation: DispositionOperation,
        binding: DispositionAuthorityBinding,
    ) -> DispositionOperation | None:
        async with self.lock:
            if self.pause_admission:
                assert self.admission_checked is not None
                assert self.admission_release is not None
                self.admission_checked.set()
                await self.admission_release.wait()
            if not disposition_authority_snapshot_matches(
                binding,
                self.snapshot,
                evaluated_at=self.now,
            ):
                return None
            key = _key(operation)
            existing = self.operations.get(key)
            if existing is not None:
                if (
                    existing.request_digest != operation.request_digest
                    or existing.authority_binding != binding
                ):
                    return None
                return existing
            self.operations[key] = operation
            if self.lose_admission_response_once:
                self.lose_admission_response_once = False
                raise TimeoutError("fence committed but response was lost")
            return operation

    async def is_current(
        self,
        binding: DispositionAuthorityBinding,
        *,
        evaluated_at: datetime,
    ) -> bool:
        async with self.lock:
            return disposition_authority_snapshot_matches(
                binding,
                self.snapshot,
                evaluated_at=evaluated_at,
            )

    async def revoke_policy(self) -> None:
        async with self.lock:
            self.snapshot = self.snapshot.model_copy(update={"deletion_policy_active": False})

    async def revoke_read_processing(self) -> None:
        async with self.lock:
            self.snapshot = self.snapshot.model_copy(update={"read_processing_revoked": True})

    async def advance_generation(self) -> None:
        async with self.lock:
            self.snapshot = self.snapshot.model_copy(
                update={"current_generation": self.snapshot.current_generation + 1}
            )

    async def store_effect_permit(
        self,
        operation: DispositionOperation,
        store_id: str,
    ) -> "MemoryStoreEffectPermit":
        del store_id
        return MemoryStoreEffectPermit(self, operation)

    async def rebind_if_current(
        self,
        operation: DispositionOperation,
        new_binding: DispositionAuthorityBinding,
    ) -> DispositionOperation | None:
        async with self.lock:
            if self.pause_rebind:
                assert self.rebind_checked is not None
                assert self.rebind_release is not None
                self.rebind_checked.set()
                await self.rebind_release.wait()
            key = _key(operation)
            current = self.operations.get(key)
            if (
                current is None
                or current.state_version != operation.state_version
                or current.authority_binding != operation.authority_binding
                or not disposition_authority_snapshot_matches(
                    new_binding,
                    self.snapshot,
                    evaluated_at=self.now,
                )
            ):
                return None
            rebound = current.model_copy(
                update={
                    "authority_binding": new_binding,
                    "status": DispositionStatus.INCOMPLETE,
                    "safe_code": "AUTHORITY_REBOUND",
                    "state_version": current.state_version + 1,
                }
            )
            self.operations[key] = rebound
            return rebound

    async def get_operation(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        operation_id: UUID,
    ) -> DispositionOperation | None:
        return await self.get(
            workspace_id=workspace_id,
            environment_id=environment_id,
            operation_id=operation_id,
        )

    async def save(
        self,
        operation: DispositionOperation,
        *,
        expected_state_version: int,
    ) -> DispositionOperation:
        async with self.lock:
            key = _key(operation)
            current = self.operations[key]
            if current.state_version != expected_state_version:
                raise DispositionConflictError("concurrent disposition update")
            self.operations[key] = operation
            return operation

    async def complete(
        self,
        operation: DispositionOperation,
        *,
        expected_state_version: int,
    ) -> DispositionOperation:
        async with self.lock:
            current = self.operations[_key(operation)]
            if current.state_version != expected_state_version:
                raise RuntimeError("concurrent disposition update")
            completed = operation.model_copy(update={"state_version": expected_state_version + 1})
            self.operations[_key(operation)] = completed
            return completed

    async def forget_completed(
        self,
        operation: DispositionOperation,
        *,
        expected_state_version: int,
    ) -> None:
        async with self.lock:
            key = _key(operation)
            current = self.operations[key]
            if current.state_version != expected_state_version:
                raise RuntimeError("concurrent disposition update")
            del self.operations[key]

    async def expire_completed(
        self, *, workspace_id: UUID, environment_id: UUID, evaluated_at: datetime
    ) -> int:
        async with self.lock:
            expired = [
                key
                for key, operation in self.operations.items()
                if (
                    operation.workspace_id == workspace_id
                    and operation.environment_id == environment_id
                    and operation.status is DispositionStatus.VERIFIED
                    and operation.completion_ack_expires_at is not None
                    and operation.completion_ack_expires_at <= evaluated_at
                )
            ]
            for key in expired:
                del self.operations[key]
            return len(expired)

    async def get(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        operation_id: UUID,
    ) -> DispositionOperation | None:
        async with self.lock:
            return self.operations.get((workspace_id, environment_id, operation_id))


class MemoryStoreEffectPermit:
    def __init__(
        self,
        runtime: MemoryDispositionRuntime,
        operation: DispositionOperation,
    ) -> None:
        self.runtime = runtime
        self.operation = operation
        self.status = StoreEffectPermitStatus.UNAVAILABLE

    async def __aenter__(self) -> StoreEffectPermitStatus:
        if self.runtime.pause_store_permit:
            assert self.runtime.store_permit_checked is not None
            assert self.runtime.store_permit_release is not None
            self.runtime.store_permit_checked.set()
            await self.runtime.store_permit_release.wait()
        await self.runtime.lock.acquire()
        current = self.runtime.operations.get(_key(self.operation))
        if (
            current is None
            or current.state_version != self.operation.state_version
            or current.status is DispositionStatus.CANCELLED
        ):
            self.status = StoreEffectPermitStatus.OPERATION_NOT_CURRENT
        elif self.operation.authority_binding is None or not disposition_authority_snapshot_matches(
            self.operation.authority_binding,
            self.runtime.snapshot,
            evaluated_at=self.runtime.now,
        ):
            self.status = StoreEffectPermitStatus.AUTHORITY_NOT_CURRENT
        else:
            self.status = StoreEffectPermitStatus.ALLOWED
        return self.status

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.runtime.lock.release()


class FailingStore:
    def __init__(self) -> None:
        self.calls = 0

    async def dispose(self, operation: DispositionOperation) -> str:
        self.calls += 1
        return StoreOutcome.FAILED

    async def reconcile(self, operation: DispositionOperation) -> str:
        self.calls += 1
        return StoreOutcome.FAILED


class OutcomeStore:
    def __init__(self, *outcomes: str) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0
        self.dispose_calls = 0
        self.reconcile_calls = 0

    async def dispose(self, operation: DispositionOperation) -> str:
        self.calls += 1
        self.dispose_calls += 1
        return self.outcomes.pop(0)

    async def reconcile(self, operation: DispositionOperation) -> str:
        self.calls += 1
        self.reconcile_calls += 1
        return self.outcomes.pop(0)


def _request(binding: DispositionAuthorityBinding | None = None) -> DispositionRequest:
    return DispositionRequest(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        operation_id=OPERATION if binding is None else binding.operation_id,
        request_digest=DIGEST,
        store_ids=("canonical",),
        authority_binding=binding,
    )


def _key(operation: DispositionOperation) -> tuple[UUID, UUID, UUID]:
    return operation.workspace_id, operation.environment_id, operation.operation_id


@pytest.mark.parametrize(
    "updates",
    [
        {"current_generation": 5},
        {"current_revision_id": UUID("00000000-0000-0000-0000-000000000099")},
        {"deletion_policy_active": False},
        {"retention_eligibility_active": False},
        {"authorization_active": False},
        {"authorization_digest": "e" * 64},
        {"issuer_active": False},
        {"deletion_policy_generation": 8},
        {"workspace_id": UUID("00000000-0000-0000-0000-000000000099")},
    ],
)
def test_stale_or_revoked_disposition_snapshot_is_denied(updates: dict[str, object]) -> None:
    assert not disposition_authority_snapshot_matches(
        _binding(), _snapshot(**updates), evaluated_at=NOW
    )


def test_read_revocation_does_not_revoke_deletion_only_authority() -> None:
    assert disposition_authority_snapshot_matches(
        _binding(), _snapshot(read_processing_revoked=True), evaluated_at=NOW
    )


@pytest.mark.asyncio
async def test_disposition_authority_revoked_before_fence_denies_without_record() -> None:
    runtime = MemoryDispositionRuntime(_snapshot(deletion_policy_active=False))
    admission = CurrentDispositionAdmission(runtime, clock=lambda: NOW)
    request = _request(_binding())
    operation = DispositionOperation(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        operation_id=OPERATION,
        request_digest=DIGEST,
        declared_store_ids=("canonical",),
        authority_binding=request.authority_binding,
        stores=(DispositionStoreState(store_id="canonical"),),
    )

    admitted = await admission.fence_or_get(operation)

    assert admitted is None
    assert runtime.operations == {}


@pytest.mark.asyncio
async def test_disposition_current_authority_denies_unbound_operation() -> None:
    runtime = MemoryDispositionRuntime()
    admission = CurrentDispositionAdmission(runtime, clock=lambda: NOW)
    operation = DispositionOperation(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        operation_id=OPERATION,
        request_digest=DIGEST,
        declared_store_ids=("canonical",),
        stores=(DispositionStoreState(store_id="canonical"),),
    )

    assert await admission.fence_or_get(operation) is None
    assert runtime.operations == {}


@pytest.mark.asyncio
async def test_disposition_admission_serializes_policy_revocation_with_fence() -> None:
    runtime = MemoryDispositionRuntime()
    runtime.pause_admission = True
    runtime.admission_checked = asyncio.Event()
    runtime.admission_release = asyncio.Event()
    admission = CurrentDispositionAdmission(runtime, clock=lambda: NOW)
    binding = _binding()
    operation = DispositionOperation(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        operation_id=OPERATION,
        request_digest=DIGEST,
        declared_store_ids=("canonical",),
        authority_binding=binding,
        stores=(DispositionStoreState(store_id="canonical"),),
    )

    admission_task = asyncio.create_task(admission.fence_or_get(operation))
    await runtime.admission_checked.wait()
    revoke_task = asyncio.create_task(runtime.revoke_policy())
    runtime.admission_release.set()
    admitted = await admission_task
    await revoke_task

    assert admitted is not None
    assert admitted.status is DispositionStatus.DELETION_PENDING
    assert admitted.fence_active is True
    assert await admission.is_current(admitted) is False
    assert runtime.operations[_key(admitted)].fence_active is True


@pytest.mark.asyncio
async def test_disposition_admission_recovers_commit_with_lost_response() -> None:
    runtime = MemoryDispositionRuntime()
    runtime.lose_admission_response_once = True
    admission = CurrentDispositionAdmission(runtime, clock=lambda: NOW)
    binding = _binding()
    operation = DispositionOperation(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        operation_id=OPERATION,
        request_digest=DIGEST,
        declared_store_ids=("canonical",),
        authority_binding=binding,
        stores=(DispositionStoreState(store_id="canonical"),),
    )

    admitted = await admission.fence_or_get(operation)

    assert admitted is not None
    assert admitted.status is DispositionStatus.DELETION_PENDING
    assert admitted.fence_active is True
    assert runtime.operations[_key(operation)] == admitted


@pytest.mark.asyncio
async def test_expiry_during_admission_wait_prevents_fence() -> None:
    runtime = MemoryDispositionRuntime()
    runtime.pause_admission = True
    runtime.admission_checked = asyncio.Event()
    runtime.admission_release = asyncio.Event()
    admission = CurrentDispositionAdmission(runtime, clock=lambda: NOW)
    store = OutcomeStore(StoreOutcome.VERIFIED)
    coordinator = DispositionCoordinator(
        repository=runtime,
        admission=admission,
        stores={"canonical": store},
        clock=lambda: NOW,
    )

    task = asyncio.create_task(coordinator.execute(_request(_binding())))
    await runtime.admission_checked.wait()
    runtime.now = _binding().expires_at
    runtime.admission_release.set()
    result = await task

    assert result.fence_active is False
    assert result.status is DispositionStatus.BLOCKED
    assert store.calls == 0
    assert runtime.operations == {}


@pytest.mark.asyncio
async def test_expiry_during_authority_rebind_wait_denies_resume() -> None:
    runtime = MemoryDispositionRuntime()
    admission = CurrentDispositionAdmission(runtime, clock=lambda: NOW)
    store = FailingStore()
    coordinator = DispositionCoordinator(
        repository=runtime,
        admission=admission,
        stores={"canonical": store},
        clock=lambda: NOW,
    )
    prior = _binding()
    operation = await coordinator.execute(_request(prior))
    fresh = _fresh_binding()
    runtime.snapshot = runtime.snapshot.model_copy(
        update={
            "tombstone_authorization_id": fresh.tombstone_authorization_id,
            "authorization_digest": fresh.authorization_digest,
            "deletion_policy_approval_id": fresh.deletion_policy_approval_id,
            "deletion_policy_version_id": fresh.deletion_policy_version_id,
            "deletion_policy_digest": fresh.deletion_policy_digest,
            "deletion_policy_generation": fresh.deletion_policy_generation,
        }
    )
    runtime.pause_rebind = True
    runtime.rebind_checked = asyncio.Event()
    runtime.rebind_release = asyncio.Event()

    task = asyncio.create_task(coordinator.resume_with_current_authority(_request(fresh)))
    await runtime.rebind_checked.wait()
    runtime.now = fresh.expires_at
    runtime.rebind_release.set()
    resumed = await task

    assert operation.status is DispositionStatus.INCOMPLETE
    assert resumed.status is DispositionStatus.INCOMPLETE
    assert resumed.safe_code == "AUTHORITY_NOT_CURRENT"
    assert resumed.authority_binding == prior
    assert store.calls == 1


@pytest.mark.asyncio
async def test_revocation_winning_store_permit_race_prevents_side_effect() -> None:
    runtime = MemoryDispositionRuntime()
    runtime.pause_store_permit = True
    runtime.store_permit_checked = asyncio.Event()
    runtime.store_permit_release = asyncio.Event()
    admission = CurrentDispositionAdmission(runtime, clock=lambda: NOW)
    store = OutcomeStore(StoreOutcome.VERIFIED)
    coordinator = DispositionCoordinator(
        repository=runtime,
        admission=admission,
        stores={"canonical": store},
        clock=lambda: NOW,
    )

    task = asyncio.create_task(coordinator.execute(_request(_binding())))
    await runtime.store_permit_checked.wait()
    await runtime.revoke_policy()
    runtime.store_permit_release.set()
    result = await task

    assert result.status is DispositionStatus.INCOMPLETE
    assert result.safe_code == "AUTHORITY_NOT_CURRENT"
    assert result.fence_active is True
    assert store.calls == 0


@pytest.mark.asyncio
async def test_expiry_during_store_permit_wait_denies_side_effect() -> None:
    runtime = MemoryDispositionRuntime()
    runtime.pause_store_permit = True
    runtime.store_permit_checked = asyncio.Event()
    runtime.store_permit_release = asyncio.Event()
    admission = CurrentDispositionAdmission(runtime, clock=lambda: NOW)
    store = OutcomeStore(StoreOutcome.VERIFIED)
    coordinator = DispositionCoordinator(
        repository=runtime,
        admission=admission,
        stores={"canonical": store},
        clock=lambda: NOW,
    )

    task = asyncio.create_task(coordinator.execute(_request(_binding())))
    await runtime.store_permit_checked.wait()
    runtime.now = _binding().expires_at
    runtime.store_permit_release.set()
    result = await task

    assert result.safe_code == "AUTHORITY_NOT_CURRENT"
    assert result.fence_active is True
    assert store.calls == 0


@pytest.mark.asyncio
async def test_cancellation_winning_store_permit_race_prevents_side_effect() -> None:
    runtime = MemoryDispositionRuntime()
    runtime.pause_store_permit = True
    runtime.store_permit_checked = asyncio.Event()
    runtime.store_permit_release = asyncio.Event()
    admission = CurrentDispositionAdmission(runtime, clock=lambda: NOW)
    store = OutcomeStore(StoreOutcome.VERIFIED)
    coordinator = DispositionCoordinator(
        repository=runtime,
        admission=admission,
        stores={"canonical": store},
        clock=lambda: NOW,
    )

    task = asyncio.create_task(coordinator.execute(_request(_binding())))
    await runtime.store_permit_checked.wait()
    current = await runtime.get(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        operation_id=OPERATION,
    )
    assert current is not None
    await coordinator.cancel(current)
    runtime.store_permit_release.set()
    result = await task

    assert result.status is DispositionStatus.CANCELLED
    assert store.calls == 0


@pytest.mark.asyncio
async def test_denied_permit_preserves_unknown_commit_for_reconciliation() -> None:
    runtime = MemoryDispositionRuntime()
    admission = CurrentDispositionAdmission(runtime, clock=lambda: NOW)
    store = OutcomeStore(StoreOutcome.UNKNOWN, StoreOutcome.VERIFIED)
    coordinator = DispositionCoordinator(
        repository=runtime,
        admission=admission,
        stores={"canonical": store},
        clock=lambda: NOW,
    )
    original_binding = _binding()
    first = await coordinator.execute(_request(original_binding))
    assert first.stores[0].status.value == "unknown"

    runtime.pause_store_permit = True
    runtime.store_permit_checked = asyncio.Event()
    runtime.store_permit_release = asyncio.Event()
    retry = asyncio.create_task(coordinator.execute(_request(original_binding)))
    await runtime.store_permit_checked.wait()
    await runtime.revoke_policy()
    runtime.store_permit_release.set()
    denied = await retry

    assert denied.stores[0].status.value == "unknown"
    assert denied.stores[0].attempts == first.stores[0].attempts
    assert denied.safe_code == "AUTHORITY_NOT_CURRENT"

    fresh = _fresh_binding()
    runtime.snapshot = runtime.snapshot.model_copy(
        update={
            "tombstone_authorization_id": fresh.tombstone_authorization_id,
            "authorization_digest": fresh.authorization_digest,
            "authorization_active": True,
            "deletion_policy_approval_id": fresh.deletion_policy_approval_id,
            "deletion_policy_version_id": fresh.deletion_policy_version_id,
            "deletion_policy_digest": fresh.deletion_policy_digest,
            "deletion_policy_generation": fresh.deletion_policy_generation,
            "deletion_policy_active": True,
        }
    )
    runtime.pause_store_permit = False
    resumed = await coordinator.resume_with_current_authority(_request(fresh))

    assert resumed.status is DispositionStatus.VERIFIED
    assert store.dispose_calls == 1
    assert store.reconcile_calls == 1


@pytest.mark.asyncio
async def test_disposition_revocation_after_fence_stops_stores_and_retains_fence() -> None:
    runtime = MemoryDispositionRuntime()
    admission = CurrentDispositionAdmission(runtime, clock=lambda: NOW)
    store = FailingStore()
    coordinator = DispositionCoordinator(
        repository=runtime,
        admission=admission,
        stores={"canonical": store},
        clock=lambda: NOW,
    )
    request = _request(_binding())

    first = await coordinator.execute(request)
    await runtime.revoke_policy()
    retry = await coordinator.execute(request)
    stored = await runtime.get(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        operation_id=OPERATION,
    )

    assert first.status is DispositionStatus.INCOMPLETE
    assert retry.status is DispositionStatus.INCOMPLETE
    assert retry.safe_code == "AUTHORITY_NOT_CURRENT"
    assert retry.fence_active is True
    assert stored is not None and stored.fence_active is True
    assert store.calls == 1


@pytest.mark.asyncio
async def test_fresh_current_approval_resumes_same_fenced_operation() -> None:
    runtime = MemoryDispositionRuntime()
    admission = CurrentDispositionAdmission(runtime, clock=lambda: NOW)
    store = OutcomeStore(StoreOutcome.FAILED, StoreOutcome.VERIFIED)
    coordinator = DispositionCoordinator(
        repository=runtime,
        admission=admission,
        stores={"canonical": store},
        clock=lambda: NOW,
    )
    prior_binding = _binding()
    first = await coordinator.execute(_request(prior_binding))
    new_binding = _fresh_binding()
    runtime.snapshot = runtime.snapshot.model_copy(
        update={
            "tombstone_authorization_id": new_binding.tombstone_authorization_id,
            "authorization_digest": new_binding.authorization_digest,
            "deletion_policy_approval_id": new_binding.deletion_policy_approval_id,
            "deletion_policy_version_id": new_binding.deletion_policy_version_id,
            "deletion_policy_digest": new_binding.deletion_policy_digest,
            "deletion_policy_generation": new_binding.deletion_policy_generation,
        }
    )

    resumed = await coordinator.resume_with_current_authority(_request(new_binding))

    assert first.status is DispositionStatus.INCOMPLETE
    assert resumed.status is DispositionStatus.VERIFIED
    assert resumed.operation_id == first.operation_id
    assert resumed.authority_binding == new_binding
    assert store.calls == 2
    with pytest.raises(DispositionConflictError):
        await coordinator.resume_with_current_authority(_request(_binding()))
    await coordinator.acknowledge_completion(resumed)


@pytest.mark.asyncio
async def test_stale_generation_rejects_worker_without_clearing_fence() -> None:
    runtime = MemoryDispositionRuntime()
    admission = CurrentDispositionAdmission(runtime, clock=lambda: NOW)
    store = FailingStore()
    coordinator = DispositionCoordinator(
        repository=runtime,
        admission=admission,
        stores={"canonical": store},
        clock=lambda: NOW,
    )
    request = _request(_binding())

    first = await coordinator.execute(request)
    await runtime.advance_generation()
    retry = await coordinator.execute(request)

    assert first.status is DispositionStatus.INCOMPLETE
    assert retry.safe_code == "AUTHORITY_NOT_CURRENT"
    assert retry.fence_active is True
    assert store.calls == 1


@pytest.mark.asyncio
async def test_read_revocation_does_not_block_separately_authorized_cleanup() -> None:
    runtime = MemoryDispositionRuntime(_snapshot(read_processing_revoked=True))
    admission = CurrentDispositionAdmission(runtime, clock=lambda: NOW)
    store = FailingStore()
    coordinator = DispositionCoordinator(
        repository=runtime,
        admission=admission,
        stores={"canonical": store},
        clock=lambda: NOW,
    )

    result = await coordinator.execute(_request(_binding()))

    assert result.status is DispositionStatus.INCOMPLETE
    assert result.fence_active is True
    assert store.calls == 1
