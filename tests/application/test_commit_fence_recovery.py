from __future__ import annotations

from types import TracebackType
from uuid import UUID

import pytest

from spine.application.commit_fence_recovery import (
    CommitFenceRecovery,
    CommitFenceRecoveryState,
    FencedIdempotentOperationCoordinator,
    IdempotencyCommitFenceReconciler,
)
from spine.application.persistence.command_digest import digest_command
from spine.application.persistence.context import PersistenceOperation
from spine.application.persistence.errors import (
    CommitFenceInvalidatedError,
    UnknownCommitOutcomeError,
)
from spine.application.persistence.idempotency import (
    IdempotencyKey,
    IdempotencyResolution,
    IdempotencyResolutionState,
    OpaqueResultReference,
)

DIGEST = digest_command(
    operation=PersistenceOperation("fenced_mutation"),
    operation_schema_version=1,
    payload={"value": "synthetic"},
)
KEY = IdempotencyKey("fenced-recovery-1")
RESULT = OpaqueResultReference(
    result_type="fenced_result", result_id=UUID(int=10), schema_version=1
)
REPLAY = OpaqueResultReference(
    result_type="fenced_result", result_id=UUID(int=11), schema_version=1
)


class _Repository:
    def __init__(self, *, replay: OpaqueResultReference | None = None) -> None:
        self.replay = replay
        self.claimed = 0
        self.completed: list[OpaqueResultReference] = []

    async def claim(self, **kwargs: object) -> object:
        del kwargs
        if self.replay is not None:
            return type("Replay", (), {"kind": "replay", "result": self.replay})()
        self.claimed += 1
        return type("Claim", (), {"kind": "owned"})()

    async def complete(self, claim: object, result: OpaqueResultReference) -> None:
        del claim
        self.completed.append(result)


class _FencedUow:
    def __init__(
        self, repository: _Repository, *, failure: BaseException | None = None
    ) -> None:
        self.idempotency = repository
        self.failure = failure
        self.recovery_pending = False
        self.commit_outcome_unknown = False
        self.committed = False
        self.settled: list[bool] = []

    async def __aenter__(self) -> "_FencedUow":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback

    async def commit(self) -> None:
        if self.failure is not None:
            self.commit_outcome_unknown = True
            self.recovery_pending = True
            raise self.failure
        self.committed = True

    async def settle_recovery(self, *, committed: bool) -> None:
        self.settled.append(committed)
        self.recovery_pending = False
        self.commit_outcome_unknown = False


class _Reconciler:
    def __init__(self, outcomes: list[CommitFenceRecovery]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    async def reconcile(self, **kwargs: object) -> CommitFenceRecovery:
        del kwargs
        self.calls += 1
        return self.outcomes.pop(0)


def _coordinator(
    uows: list[_FencedUow], reconciler: _Reconciler, *, maximum_attempts: int = 2
) -> FencedIdempotentOperationCoordinator:
    bindings: list[int] = []

    async def binding_factory(attempt: int) -> object:
        bindings.append(attempt)
        return object()

    def factory(context: object, binding: object) -> _FencedUow:
        del context, binding
        return uows.pop(0)

    coordinator = FencedIdempotentOperationCoordinator(
        uow_factory=factory,
        context_factory=lambda: object(),
        binding_factory=binding_factory,
        reconciler=reconciler,
        maximum_attempts=maximum_attempts,
    )
    coordinator.binding_attempts = bindings  # type: ignore[attr-defined]
    return coordinator


@pytest.mark.asyncio
async def test_uncertain_commit_reconciles_rollback_then_retries_with_fresh_binding() -> (
    None
):
    first = _FencedUow(_Repository(), failure=UnknownCommitOutcomeError("unknown"))
    second = _FencedUow(_Repository())
    reconciler = _Reconciler(
        [CommitFenceRecovery(CommitFenceRecoveryState.ROLLED_BACK)]
    )
    coordinator = _coordinator([first, second], reconciler)
    calls = 0

    async def operation(uow: object, claim: object) -> OpaqueResultReference:
        nonlocal calls
        del uow, claim
        calls += 1
        return RESULT

    result = await coordinator.execute(
        operation_schema_version=1,
        key=KEY,
        digest=DIGEST,
        operation=operation,
    )

    assert result == RESULT
    assert calls == 2
    assert first.settled == [False]
    assert reconciler.calls == 1
    assert coordinator.binding_attempts == [1, 2]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_uncertain_commit_replays_canonical_result_without_duplicate_operation() -> (
    None
):
    first = _FencedUow(_Repository(), failure=UnknownCommitOutcomeError("unknown"))
    reconciler = _Reconciler(
        [CommitFenceRecovery(CommitFenceRecoveryState.COMMITTED, REPLAY)]
    )
    coordinator = _coordinator([first], reconciler)
    calls = 0

    async def operation(uow: object, claim: object) -> OpaqueResultReference:
        nonlocal calls
        del uow, claim
        calls += 1
        return RESULT

    result = await coordinator.execute(
        operation_schema_version=1,
        key=KEY,
        digest=DIGEST,
        operation=operation,
    )

    assert result == REPLAY
    assert calls == 1
    assert first.settled == [True]


@pytest.mark.asyncio
async def test_unresolved_commit_blocks_retry() -> None:
    first = _FencedUow(_Repository(), failure=UnknownCommitOutcomeError("unknown"))
    reconciler = _Reconciler([CommitFenceRecovery(CommitFenceRecoveryState.UNRESOLVED)])
    coordinator = _coordinator([first], reconciler)

    async def operation(uow: object, claim: object) -> OpaqueResultReference:
        del uow, claim
        return RESULT

    with pytest.raises(UnknownCommitOutcomeError, match="remains unresolved"):
        await coordinator.execute(
            operation_schema_version=1,
            key=KEY,
            digest=DIGEST,
            operation=operation,
        )
    assert first.settled == []


@pytest.mark.asyncio
async def test_revoked_authority_blocks_retry_after_confirmed_rollback() -> None:
    first = _FencedUow(_Repository(), failure=UnknownCommitOutcomeError("unknown"))
    reconciler = _Reconciler(
        [CommitFenceRecovery(CommitFenceRecoveryState.ROLLED_BACK)]
    )
    calls: list[int] = []

    async def binding_factory(attempt: int) -> object:
        calls.append(attempt)
        if attempt == 2:
            raise CommitFenceInvalidatedError("revoked")
        return object()

    coordinator = FencedIdempotentOperationCoordinator(
        uow_factory=lambda context, binding: first,
        context_factory=lambda: object(),
        binding_factory=binding_factory,
        reconciler=reconciler,
    )

    async def operation(uow: object, claim: object) -> OpaqueResultReference:
        del uow, claim
        return RESULT

    with pytest.raises(CommitFenceInvalidatedError, match="revoked"):
        await coordinator.execute(
            operation_schema_version=1,
            key=KEY,
            digest=DIGEST,
            operation=operation,
        )
    assert calls == [1, 2]


class _ResolutionRepository:
    def __init__(self, resolution: IdempotencyResolution) -> None:
        self.resolution = resolution

    async def resolve(self, **kwargs: object) -> IdempotencyResolution:
        del kwargs
        return self.resolution


class _ReadUow:
    def __init__(self, resolution: IdempotencyResolution) -> None:
        self.idempotency = _ResolutionRepository(resolution)

    async def __aenter__(self) -> "_ReadUow":
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        del exc_type, exc_value, traceback


@pytest.mark.asyncio
async def test_idempotency_reconciler_maps_completed_receipt_to_stable_result() -> None:
    reconciler = IdempotencyCommitFenceReconciler(
        uow_factory=lambda context: _ReadUow(
            IdempotencyResolution(IdempotencyResolutionState.COMPLETED, REPLAY)
        ),
        context_factory=lambda: object(),
    )

    recovery = await reconciler.reconcile(
        operation_schema_version=1, key=KEY, digest=DIGEST
    )

    assert recovery == CommitFenceRecovery(CommitFenceRecoveryState.COMMITTED, REPLAY)


@pytest.mark.asyncio
async def test_idempotency_reconciler_denies_digest_conflict_without_details() -> None:
    reconciler = IdempotencyCommitFenceReconciler(
        uow_factory=lambda context: _ReadUow(
            IdempotencyResolution(IdempotencyResolutionState.CONFLICT)
        ),
        context_factory=lambda: object(),
    )

    recovery = await reconciler.reconcile(
        operation_schema_version=1, key=KEY, digest=DIGEST
    )

    assert recovery == CommitFenceRecovery(CommitFenceRecoveryState.DENIED)
