"""Retry and reconciliation policy for fenced, idempotent mutations.

The coordinator keeps the retry identity stable while requiring a new
authorization binding and permit for every attempt.  A lost commit response is
resolved only from the canonical idempotency receipt; an unresolved receipt is
never replayed blindly.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Protocol

from spine.application.commit_fence import CommitFenceBinding
from spine.application.persistence.command_digest import CommandDigest
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.errors import (
    CommitFenceInvalidatedError,
    CommitFenceReleaseError,
    UnknownCommitOutcomeError,
)
from spine.application.persistence.idempotency import (
    IdempotencyKey,
    IdempotencyReplay,
    IdempotencyRepository,
    IdempotencyResolutionState,
    OpaqueResultReference,
    OwnedIdempotencyClaim,
)
from spine.application.persistence.unit_of_work import TenantUnitOfWork


class CommitFenceRecoveryState(str, Enum):
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    UNRESOLVED = "unresolved"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class CommitFenceRecovery:
    state: CommitFenceRecoveryState
    result: OpaqueResultReference | None = None

    def __post_init__(self) -> None:
        if self.state is CommitFenceRecoveryState.COMMITTED:
            if self.result is None:
                raise ValueError("committed recovery requires a canonical result")
        elif self.result is not None:
            raise ValueError("only committed recovery carries a result")


class CommitFenceCanonicalReconciler(Protocol):
    async def reconcile(
        self,
        *,
        operation_schema_version: int,
        key: IdempotencyKey,
        digest: CommandDigest,
    ) -> CommitFenceRecovery: ...


class IdempotencyCommitFenceReconciler:
    """Resolve uncertain commit state from a fresh, authorized UoW."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[TrustedPersistenceContext], TenantUnitOfWork],
        context_factory: Callable[[], TrustedPersistenceContext],
    ) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory

    async def reconcile(
        self,
        *,
        operation_schema_version: int,
        key: IdempotencyKey,
        digest: CommandDigest,
    ) -> CommitFenceRecovery:
        try:
            async with self._uow_factory(self._context_factory()) as uow:
                resolution = await uow.idempotency.resolve(
                    operation_schema_version=operation_schema_version,
                    key=key,
                    digest=digest,
                )
                if resolution.state is IdempotencyResolutionState.COMPLETED:
                    assert resolution.result is not None
                    return CommitFenceRecovery(
                        CommitFenceRecoveryState.COMMITTED,
                        resolution.result,
                    )
                if resolution.state is IdempotencyResolutionState.ABSENT:
                    return CommitFenceRecovery(CommitFenceRecoveryState.ROLLED_BACK)
                if resolution.state is IdempotencyResolutionState.CONFLICT:
                    return CommitFenceRecovery(CommitFenceRecoveryState.DENIED)
                return CommitFenceRecovery(CommitFenceRecoveryState.UNRESOLVED)
        except asyncio.CancelledError:
            raise
        except CommitFenceInvalidatedError:
            return CommitFenceRecovery(CommitFenceRecoveryState.DENIED)
        except Exception:
            # Reconciliation is fail-closed and deliberately content-free.
            return CommitFenceRecovery(CommitFenceRecoveryState.UNRESOLVED)


class FencedUnitOfWork(Protocol):
    idempotency: IdempotencyRepository
    recovery_pending: bool
    commit_outcome_unknown: bool
    committed: bool

    async def __aenter__(self) -> "FencedUnitOfWork": ...

    async def __aexit__(self, exc_type, exc_value, traceback) -> None: ...

    async def commit(self) -> None: ...

    async def settle_recovery(self, *, committed: bool) -> None: ...


class FencedIdempotentOperationCoordinator:
    """Run one mutation with fresh fencing and canonical recovery on retry."""

    def __init__(
        self,
        *,
        uow_factory: Callable[
            [TrustedPersistenceContext, CommitFenceBinding], FencedUnitOfWork
        ],
        context_factory: Callable[[], TrustedPersistenceContext],
        binding_factory: Callable[[int], Awaitable[CommitFenceBinding]],
        reconciler: CommitFenceCanonicalReconciler,
        maximum_attempts: int = 3,
    ) -> None:
        if maximum_attempts < 1:
            raise ValueError("maximum_attempts must be positive")
        self._uow_factory = uow_factory
        self._context_factory = context_factory
        self._binding_factory = binding_factory
        self._reconciler = reconciler
        self._maximum_attempts = maximum_attempts

    async def execute(
        self,
        *,
        operation_schema_version: int,
        key: IdempotencyKey,
        digest: CommandDigest,
        operation: Callable[
            [FencedUnitOfWork, OwnedIdempotencyClaim],
            Awaitable[OpaqueResultReference],
        ],
    ) -> OpaqueResultReference:
        for attempt in range(1, self._maximum_attempts + 1):
            binding = await self._binding_factory(attempt)
            fenced = self._uow_factory(self._context_factory(), binding)
            result: OpaqueResultReference | None = None
            try:
                async with fenced as uow:
                    repository = uow.idempotency
                    claim = await repository.claim(
                        operation_schema_version=operation_schema_version,
                        key=key,
                        digest=digest,
                    )
                    if isinstance(claim, IdempotencyReplay):
                        return claim.result
                    result = await operation(uow, claim)
                    await repository.complete(claim, result)
                    await uow.commit()
                    return result
            except asyncio.CancelledError:
                try:
                    await self._recover(
                        fenced,
                        operation_schema_version=operation_schema_version,
                        key=key,
                        digest=digest,
                        known_result=result,
                    )
                except BaseException:
                    # Preserve the caller's cancellation.  A failed settlement
                    # remains visible through the fenced UoW's pending state.
                    pass
                raise
            except (UnknownCommitOutcomeError, CommitFenceReleaseError):
                recovery = await self._recover(
                    fenced,
                    operation_schema_version=operation_schema_version,
                    key=key,
                    digest=digest,
                    known_result=result,
                )
                if recovery.state is CommitFenceRecoveryState.COMMITTED:
                    assert recovery.result is not None
                    return recovery.result
                if (
                    recovery.state is CommitFenceRecoveryState.ROLLED_BACK
                    and attempt < self._maximum_attempts
                ):
                    continue
                if recovery.state is CommitFenceRecoveryState.DENIED:
                    raise CommitFenceInvalidatedError(
                        "Commit-fence recovery was denied."
                    ) from None
                raise UnknownCommitOutcomeError(
                    "Commit-fence outcome remains unresolved."
                ) from None
        raise UnknownCommitOutcomeError("Commit-fence retry policy was exhausted.")

    async def _recover(
        self,
        fenced: FencedUnitOfWork,
        *,
        operation_schema_version: int,
        key: IdempotencyKey,
        digest: CommandDigest,
        known_result: OpaqueResultReference | None,
    ) -> CommitFenceRecovery:
        if not fenced.recovery_pending:
            return CommitFenceRecovery(CommitFenceRecoveryState.ROLLED_BACK)
        if fenced.commit_outcome_unknown:
            recovery = await self._reconciler.reconcile(
                operation_schema_version=operation_schema_version,
                key=key,
                digest=digest,
            )
        else:
            recovery = CommitFenceRecovery(
                (
                    CommitFenceRecoveryState.COMMITTED
                    if fenced.committed
                    else CommitFenceRecoveryState.ROLLED_BACK
                ),
                known_result if fenced.committed else None,
            )
        if recovery.state in {
            CommitFenceRecoveryState.COMMITTED,
            CommitFenceRecoveryState.ROLLED_BACK,
        }:
            await fenced.settle_recovery(
                committed=recovery.state is CommitFenceRecoveryState.COMMITTED
            )
        return recovery


__all__ = [
    "CommitFenceCanonicalReconciler",
    "CommitFenceRecovery",
    "CommitFenceRecoveryState",
    "FencedIdempotentOperationCoordinator",
    "FencedUnitOfWork",
    "IdempotencyCommitFenceReconciler",
]
