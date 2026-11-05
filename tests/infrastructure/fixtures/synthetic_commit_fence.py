"""Deterministic synthetic F2 fence authority for tests only.

This fixture exercises local ordering and failure handling. It does not model or
qualify any real provider guarantee. Keep it under ``tests/`` so production
composition cannot import or register it and Hatchling does not package it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable
from uuid import UUID

from spine.application.commit_fence import CommitFenceBinding
from spine.application.commit_fence_provider_port import (
    CommitFenceFailureReason,
    CommitFenceOperation,
    CommitFenceOperationResult,
    CommitFenceOperationState,
    CommitFencePermit,
    ExternalCommitFencePort,
)


_PROVIDER_CONTRACT_VERSION = "synthetic-test-authority:v1"


class ManualCommitFenceClock:
    """Injectable UTC clock advanced explicitly by a test."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        if delta <= timedelta(0):
            raise ValueError("manual clock advances must be positive")
        self._now += delta


class DeterministicBarrier:
    """An async gate that exposes arrival and explicit release to a test."""

    def __init__(self, *, blocked: bool = False) -> None:
        self._entered = asyncio.Event()
        self._released = asyncio.Event()
        if not blocked:
            self._released.set()

    async def wait(self) -> None:
        self._entered.set()
        await self._released.wait()

    async def wait_until_reached(self) -> None:
        await self._entered.wait()

    def release(self) -> None:
        self._released.set()


@dataclass
class SyntheticCommitFenceBarriers:
    before_acquire: DeterministicBarrier = field(default_factory=DeterministicBarrier)
    before_revocation_acknowledgement: DeterministicBarrier = field(
        default_factory=DeterministicBarrier
    )


@dataclass(frozen=True)
class SyntheticRevocationAcknowledgement:
    binding_digest: str
    revoked_through_epoch: int
    acknowledged_at: datetime


class SyntheticCommitFenceAuthority(ExternalCommitFencePort):
    """Barrier-driven F2 order fixture; never evidence of provider guarantees."""

    provider_contract_version = _PROVIDER_CONTRACT_VERSION

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        permit_ttl: timedelta = timedelta(minutes=1),
        barriers: SyntheticCommitFenceBarriers | None = None,
    ) -> None:
        if permit_ttl <= timedelta(0):
            raise ValueError("synthetic permit TTL must be positive")
        self._clock = clock
        self._permit_ttl = permit_ttl
        self.barriers = barriers or SyntheticCommitFenceBarriers()
        self._condition = asyncio.Condition()
        self._permits: dict[UUID, CommitFencePermit] = {}
        self._revocation_floor: dict[tuple[UUID, UUID, UUID, str, str, UUID], int] = {}
        self._revocation_pending: set[tuple[UUID, UUID, UUID, str, str, UUID]] = set()
        self._revocation_requested: dict[
            tuple[UUID, UUID, UUID, str, str, UUID], asyncio.Event
        ] = {}
        self._ambiguous_releases: set[UUID] = set()
        self._next_reference = 1

    async def acquire(self, binding: CommitFenceBinding) -> CommitFenceOperationResult:
        await self.barriers.before_acquire.wait()
        now = self._clock()
        async with self._condition:
            self._expire_due_locked(now)
            key = _authority_key(binding)
            if not binding.is_valid_at(now):
                return _result(
                    binding,
                    CommitFenceOperation.ACQUIRE,
                    CommitFenceOperationState.EXPIRED,
                    now,
                )
            if (
                key in self._revocation_pending
                or binding.revocation_epoch <= self._revocation_floor.get(key, -1)
            ):
                return _result(
                    binding,
                    CommitFenceOperation.ACQUIRE,
                    CommitFenceOperationState.STALE_HOLDER,
                    now,
                )

            valid_until = min(now + self._permit_ttl, binding.valid_until)
            permit = CommitFencePermit(
                binding=binding,
                provider_contract_version=self.provider_contract_version,
                permit_reference=UUID(int=self._next_reference),
                acquired_at=now,
                valid_until=valid_until,
            )
            self._next_reference += 1
            self._permits[permit.permit_reference] = permit
            return _result(
                binding,
                CommitFenceOperation.ACQUIRE,
                CommitFenceOperationState.ACQUIRED,
                now,
                permit=permit,
            )

    async def renew(
        self, permit: CommitFencePermit, *, evaluated_at: datetime
    ) -> CommitFenceOperationResult:
        async with self._condition:
            now = self._clock()
            live = self._live_permit_locked(permit, now)
            if live is None:
                return _result(
                    permit.binding,
                    CommitFenceOperation.RENEW,
                    _invalid_permit_state(permit, now),
                    now,
                )
            if _authority_key(permit.binding) in self._revocation_pending:
                return _result(
                    permit.binding,
                    CommitFenceOperation.RENEW,
                    CommitFenceOperationState.STALE_HOLDER,
                    now,
                )
            if evaluated_at != now:
                return _result(
                    permit.binding,
                    CommitFenceOperation.RENEW,
                    CommitFenceOperationState.DENIED,
                    now,
                )
            renewed = CommitFencePermit(
                binding=permit.binding,
                provider_contract_version=self.provider_contract_version,
                permit_reference=permit.permit_reference,
                acquired_at=permit.acquired_at,
                valid_until=min(now + self._permit_ttl, permit.binding.valid_until),
            )
            self._permits[renewed.permit_reference] = renewed
            return _result(
                permit.binding,
                CommitFenceOperation.RENEW,
                CommitFenceOperationState.RENEWED,
                now,
                permit=renewed,
            )

    async def validate_before_commit(
        self, permit: CommitFencePermit, *, evaluated_at: datetime
    ) -> CommitFenceOperationResult:
        async with self._condition:
            now = self._clock()
            live = self._live_permit_locked(permit, now)
            if live is None:
                return _result(
                    permit.binding,
                    CommitFenceOperation.PRE_COMMIT,
                    _invalid_permit_state(permit, now),
                    now,
                )
            if evaluated_at != now:
                return _result(
                    permit.binding,
                    CommitFenceOperation.PRE_COMMIT,
                    CommitFenceOperationState.DENIED,
                    now,
                )
            return _result(
                permit.binding,
                CommitFenceOperation.PRE_COMMIT,
                CommitFenceOperationState.VALID,
                now,
                permit=live,
            )

    async def finish(self, permit: CommitFencePermit) -> CommitFenceOperationResult:
        return await self._release(permit, CommitFenceOperation.FINISH)

    async def abort(self, permit: CommitFencePermit) -> CommitFenceOperationResult:
        return await self._release(permit, CommitFenceOperation.ABORT)

    async def _release(
        self, permit: CommitFencePermit, operation: CommitFenceOperation
    ) -> CommitFenceOperationResult:
        async with self._condition:
            now = self._clock()
            live = self._live_permit_locked(permit, now)
            if live is None:
                return _result(
                    permit.binding,
                    operation,
                    _invalid_permit_state(permit, now),
                    now,
                )
            if permit.permit_reference in self._ambiguous_releases:
                return _result(
                    permit.binding,
                    operation,
                    CommitFenceOperationState.INDETERMINATE,
                    now,
                    failure_reason=CommitFenceFailureReason.LOST_RESPONSE,
                )
            del self._permits[permit.permit_reference]
            self._condition.notify_all()
            return _result(
                permit.binding,
                operation,
                CommitFenceOperationState.RELEASED,
                now,
                released_permit_reference=permit.permit_reference,
            )

    async def revoke(
        self, binding: CommitFenceBinding
    ) -> SyntheticRevocationAcknowledgement:
        key = _authority_key(binding)
        async with self._condition:
            self._revocation_pending.add(key)
            requested = self._revocation_requested.setdefault(key, asyncio.Event())
            requested.set()

        try:
            async with self._condition:
                while self._has_live_permit_locked(binding, self._clock()):
                    await self._condition.wait()
            await self.barriers.before_revocation_acknowledgement.wait()
            async with self._condition:
                self._revocation_floor[key] = max(
                    binding.revocation_epoch, self._revocation_floor.get(key, -1)
                )
                self._revocation_pending.discard(key)
                self._condition.notify_all()
                return SyntheticRevocationAcknowledgement(
                    binding_digest=binding.calculate_digest(),
                    revoked_through_epoch=self._revocation_floor[key],
                    acknowledged_at=self._clock(),
                )
        except BaseException:
            async with self._condition:
                self._revocation_pending.discard(key)
                self._condition.notify_all()
            raise

    async def wait_for_revocation_request(self, binding: CommitFenceBinding) -> None:
        event = self._revocation_requested.setdefault(_authority_key(binding), asyncio.Event())
        await event.wait()

    async def notify_clock_advanced(self) -> None:
        """Expire due permits and wake revocation waiters after a manual advance."""

        async with self._condition:
            self._expire_due_locked(self._clock())
            self._condition.notify_all()

    def make_release_ambiguous(self, permit: CommitFencePermit) -> None:
        self._ambiguous_releases.add(permit.permit_reference)

    def _live_permit_locked(
        self, requested: CommitFencePermit, now: datetime
    ) -> CommitFencePermit | None:
        self._expire_due_locked(now)
        live = self._permits.get(requested.permit_reference)
        if live is None or live != requested:
            return None
        return live

    def _has_live_permit_locked(self, binding: CommitFenceBinding, now: datetime) -> bool:
        self._expire_due_locked(now)
        return any(permit.binding == binding for permit in self._permits.values())

    def _expire_due_locked(self, now: datetime) -> None:
        expired = [
            reference
            for reference, permit in self._permits.items()
            if now >= permit.valid_until
        ]
        for reference in expired:
            del self._permits[reference]


def _authority_key(
    binding: CommitFenceBinding,
) -> tuple[UUID, UUID, UUID, str, str, UUID]:
    return (
        binding.issuer_id,
        binding.workspace_id,
        binding.environment_id,
        binding.purpose,
        binding.action,
        binding.subject_id,
    )


def _invalid_permit_state(
    permit: CommitFencePermit, now: datetime
) -> CommitFenceOperationState:
    if now >= permit.valid_until:
        return CommitFenceOperationState.EXPIRED
    return CommitFenceOperationState.STALE_HOLDER


def _result(
    binding: CommitFenceBinding,
    operation: CommitFenceOperation,
    state: CommitFenceOperationState,
    now: datetime,
    *,
    permit: CommitFencePermit | None = None,
    released_permit_reference: UUID | None = None,
    failure_reason: CommitFenceFailureReason | None = None,
) -> CommitFenceOperationResult:
    return CommitFenceOperationResult(
        binding=binding,
        provider_contract_version=_PROVIDER_CONTRACT_VERSION,
        operation=operation,
        state=state,
        evaluated_at=now,
        permit=permit,
        released_permit_reference=released_permit_reference,
        failure_reason=failure_reason,
    )


__all__ = [
    "DeterministicBarrier",
    "ManualCommitFenceClock",
    "SyntheticCommitFenceAuthority",
    "SyntheticCommitFenceBarriers",
    "SyntheticRevocationAcknowledgement",
]
