from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import TracebackType
from uuid import UUID

import pytest

from spine.application.commit_fence import (
    CommitFenceBinding,
    PrivateArtifactSourceLineage,
    canonical_commit_fence_lineage_digest,
)
from spine.application.commit_fence_provider_port import (
    CommitFenceFailureReason,
    CommitFenceOperation,
    CommitFenceOperationResult,
    CommitFenceOperationState,
    CommitFencePermit,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.application.persistence.errors import (
    CommitFenceInvalidatedError,
    InvalidCommitFenceConfigurationError,
    UnknownCommitOutcomeError,
)
from spine.infrastructure.persistence.postgresql_fenced_unit_of_work import (
    PostgreSQLCommitFenceTiming,
    PostgreSQLFencedUnitOfWorkFactory,
)
from spine.infrastructure.persistence.contexts import TrustedContextBoundary

NOW = datetime(2026, 11, 5, 15, tzinfo=timezone.utc)
BOUNDARY = TrustedContextBoundary.for_testing(
    issuer_id=UUID(int=90),
    secret=b"issue-975-fenced-uow-tests-secret-01",
)
CONTEXT = BOUNDARY.worker(
    scope=EnvironmentScope(workspace_id=UUID(int=2), environment_id=UUID(int=3)),
    service_principal_id=UUID(int=91),
    purpose=PersistencePurpose("commit_fence_test"),
    operation=PersistenceOperation("protected_commit"),
    trace_id=UUID(int=92),
)


def _id(value: int) -> UUID:
    return UUID(int=value)


def _binding() -> CommitFenceBinding:
    lineage = (
        PrivateArtifactSourceLineage(
            source_object_id=_id(10),
            source_revision_id=_id(11),
            source_revision_digest="a" * 64,
        ),
    )
    return CommitFenceBinding(
        issuer_id=_id(1),
        approval_version="approval:v1",
        approval_digest="b" * 64,
        workspace_id=_id(2),
        environment_id=_id(3),
        purpose="private_report_publication",
        action="commit_report",
        subject_id=_id(4),
        state_digest="c" * 64,
        source_lineage=lineage,
        lineage_digest=canonical_commit_fence_lineage_digest(
            workspace_id=_id(2),
            environment_id=_id(3),
            source_lineage=lineage,
        ),
        generation=1,
        revocation_epoch=1,
        operation_id=_id(5),
        idempotency_key="commit-fence-operation-1",
        valid_from=NOW - timedelta(minutes=1),
        valid_until=NOW + timedelta(minutes=5),
    )


class ManualClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class FakeBoundedUnitOfWork:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.active = False
        self.committed = False
        self.block_commit = False
        self.commit_started = asyncio.Event()
        self.commit_error: BaseException | None = None
        self.pending_effects = ["audit", "outbox", "state"]

    async def __aenter__(self) -> FakeBoundedUnitOfWork:
        self.active = True
        self.events.append("db_enter")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.active and not self.committed:
            await self.rollback()
        self.active = False
        self.events.append("db_close")

    async def commit(self) -> None:
        self.events.append("db_commit")
        self.commit_started.set()
        if self.block_commit:
            await asyncio.Event().wait()
        if self.commit_error is not None:
            raise self.commit_error
        self.committed = True

    async def rollback(self) -> None:
        self.events.append("db_rollback")
        self.pending_effects.clear()
        self.active = False


class FakeBoundedFactory:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.timeouts: list[timedelta] = []
        self.created: list[FakeBoundedUnitOfWork] = []

    def create_bounded(
        self,
        context: object,
        *,
        transaction_timeout: timedelta,
    ) -> FakeBoundedUnitOfWork:
        assert context is CONTEXT
        self.timeouts.append(transaction_timeout)
        uow = FakeBoundedUnitOfWork(self.events)
        self.created.append(uow)
        return uow


class FakeProvider:
    provider_contract_version = "test-f2:v1"

    def __init__(
        self,
        *,
        clock: ManualClock,
        events: list[str],
        permit_ttl: timedelta = timedelta(seconds=10),
    ) -> None:
        self.clock = clock
        self.events = events
        self.permit_ttl = permit_ttl
        self.validate_started = asyncio.Event()
        self.block_validate = False
        self.ambiguous_renewal = False
        self.validation_ttl: timedelta | None = None
        self._permit: CommitFencePermit | None = None

    async def acquire(self, binding: CommitFenceBinding) -> CommitFenceOperationResult:
        self.events.append("provider_acquire")
        now = self.clock()
        self._permit = CommitFencePermit(
            binding=binding,
            provider_contract_version=self.provider_contract_version,
            permit_reference=_id(50),
            acquired_at=now,
            valid_until=min(now + self.permit_ttl, binding.valid_until),
        )
        return self._result(
            CommitFenceOperation.ACQUIRE,
            CommitFenceOperationState.ACQUIRED,
            permit=self._permit,
        )

    async def renew(
        self, permit: CommitFencePermit, *, evaluated_at: datetime
    ) -> CommitFenceOperationResult:
        self.events.append("provider_renew")
        if self.ambiguous_renewal:
            return self._result(
                CommitFenceOperation.RENEW,
                CommitFenceOperationState.AMBIGUOUS,
                failure_reason=CommitFenceFailureReason.LOST_RESPONSE,
            )
        self._permit = permit.model_copy(
            update={
                "valid_until": min(
                    self.clock() + self.permit_ttl, permit.binding.valid_until
                )
            }
        )
        return self._result(
            CommitFenceOperation.RENEW,
            CommitFenceOperationState.RENEWED,
            permit=self._permit,
        )

    async def validate_before_commit(
        self, permit: CommitFencePermit, *, evaluated_at: datetime
    ) -> CommitFenceOperationResult:
        self.events.append("provider_validate")
        self.validate_started.set()
        if self.block_validate:
            await asyncio.Event().wait()
        if evaluated_at >= permit.valid_until:
            return self._result(
                CommitFenceOperation.PRE_COMMIT,
                CommitFenceOperationState.EXPIRED,
            )
        if self.validation_ttl is not None:
            permit = CommitFencePermit(
                binding=permit.binding,
                provider_contract_version=permit.provider_contract_version,
                permit_reference=permit.permit_reference,
                acquired_at=permit.acquired_at,
                valid_until=min(
                    self.clock() + self.validation_ttl,
                    permit.binding.valid_until,
                ),
            )
        return self._result(
            CommitFenceOperation.PRE_COMMIT,
            CommitFenceOperationState.VALID,
            permit=permit,
        )

    async def finish(self, permit: CommitFencePermit) -> CommitFenceOperationResult:
        self.events.append("provider_finish")
        return self._result(
            CommitFenceOperation.FINISH,
            CommitFenceOperationState.RELEASED,
            released_permit_reference=permit.permit_reference,
        )

    async def abort(self, permit: CommitFencePermit) -> CommitFenceOperationResult:
        self.events.append("provider_abort")
        return self._result(
            CommitFenceOperation.ABORT,
            CommitFenceOperationState.RELEASED,
            released_permit_reference=permit.permit_reference,
        )

    def _result(
        self,
        operation: CommitFenceOperation,
        state: CommitFenceOperationState,
        *,
        permit: CommitFencePermit | None = None,
        released_permit_reference: UUID | None = None,
        failure_reason: CommitFenceFailureReason | None = None,
    ) -> CommitFenceOperationResult:
        assert self._permit is not None
        return CommitFenceOperationResult(
            binding=self._permit.binding,
            provider_contract_version=self.provider_contract_version,
            operation=operation,
            state=state,
            evaluated_at=self.clock(),
            permit=permit,
            released_permit_reference=released_permit_reference,
            failure_reason=failure_reason,
        )


def _timing(**updates: timedelta) -> PostgreSQLCommitFenceTiming:
    values = {
        "transaction_timeout": timedelta(seconds=2),
        "provider_operation_timeout": timedelta(seconds=1),
        "renewal_threshold": timedelta(seconds=1),
        "clock_uncertainty_margin": timedelta(milliseconds=500),
    }
    values.update(updates)
    return PostgreSQLCommitFenceTiming(**values)


def _factory(
    *,
    clock: ManualClock,
    provider: FakeProvider,
    bounded: FakeBoundedFactory,
    timing: PostgreSQLCommitFenceTiming | None = None,
) -> PostgreSQLFencedUnitOfWorkFactory:
    return PostgreSQLFencedUnitOfWorkFactory(
        uow_factory=bounded,
        provider=provider,
        timing=timing or _timing(),
        clock=clock,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("transaction_timeout", timedelta(0)),
        ("provider_operation_timeout", timedelta(0)),
        ("renewal_threshold", timedelta(microseconds=-1)),
        ("clock_uncertainty_margin", timedelta(0)),
        ("transaction_timeout", timedelta(microseconds=1500)),
    ),
)
def test_invalid_explicit_timing_configuration_denies(
    field: str, value: timedelta
) -> None:
    with pytest.raises(InvalidCommitFenceConfigurationError):
        _timing(**{field: value})


def test_missing_timing_configuration_denies() -> None:
    events: list[str] = []
    clock = ManualClock(NOW)
    with pytest.raises(InvalidCommitFenceConfigurationError):
        PostgreSQLFencedUnitOfWorkFactory(
            uow_factory=FakeBoundedFactory(events),
            provider=FakeProvider(clock=clock, events=events),
            timing=None,  # type: ignore[arg-type]
            clock=clock,
        )


@pytest.mark.asyncio
async def test_commit_closes_database_before_finishing_permit() -> None:
    events: list[str] = []
    clock = ManualClock(NOW)
    bounded = FakeBoundedFactory(events)
    provider = FakeProvider(clock=clock, events=events)

    async with _factory(clock=clock, provider=provider, bounded=bounded)(
        CONTEXT, _binding()
    ) as uow:
        await uow.commit()

    assert bounded.timeouts == [timedelta(seconds=2)]
    assert events == [
        "provider_acquire",
        "db_enter",
        "provider_validate",
        "db_commit",
        "db_close",
        "provider_finish",
    ]


@pytest.mark.asyncio
async def test_expired_permit_rolls_back_atomic_effects_before_release() -> None:
    events: list[str] = []
    clock = ManualClock(NOW)
    bounded = FakeBoundedFactory(events)
    provider = FakeProvider(clock=clock, events=events)

    with pytest.raises(CommitFenceInvalidatedError):
        async with _factory(clock=clock, provider=provider, bounded=bounded)(
            CONTEXT, _binding()
        ) as uow:
            clock.advance(timedelta(seconds=10))
            await uow.commit()

    assert "db_commit" not in events
    assert bounded.created[0].pending_effects == []
    assert events[-3:] == ["db_rollback", "db_close", "provider_abort"]


@pytest.mark.asyncio
async def test_ambiguous_renewal_never_opens_database_transaction() -> None:
    events: list[str] = []
    clock = ManualClock(NOW)
    bounded = FakeBoundedFactory(events)
    provider = FakeProvider(
        clock=clock,
        events=events,
        permit_ttl=timedelta(seconds=3),
    )
    provider.ambiguous_renewal = True

    with pytest.raises(CommitFenceInvalidatedError):
        async with _factory(clock=clock, provider=provider, bounded=bounded)(
            CONTEXT, _binding()
        ):
            pytest.fail("ambiguous renewal must fail during entry")

    assert bounded.created == []
    assert events == ["provider_acquire", "provider_renew", "provider_abort"]


@pytest.mark.asyncio
async def test_cancellation_awaiting_provider_rolls_back_and_closes_before_release() -> (
    None
):
    events: list[str] = []
    clock = ManualClock(NOW)
    bounded = FakeBoundedFactory(events)
    provider = FakeProvider(clock=clock, events=events)
    provider.block_validate = True

    async def protected_work() -> None:
        async with _factory(clock=clock, provider=provider, bounded=bounded)(
            CONTEXT, _binding()
        ) as uow:
            await uow.commit()

    task = asyncio.create_task(protected_work())
    await provider.validate_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "db_commit" not in events
    assert events[-3:] == ["db_rollback", "db_close", "provider_abort"]


@pytest.mark.asyncio
async def test_provider_timeout_cannot_commit_protected_effects() -> None:
    events: list[str] = []
    clock = ManualClock(NOW)
    bounded = FakeBoundedFactory(events)
    provider = FakeProvider(clock=clock, events=events)
    provider.block_validate = True

    with pytest.raises(CommitFenceInvalidatedError, match="timed out"):
        async with _factory(
            clock=clock,
            provider=provider,
            bounded=bounded,
            timing=_timing(provider_operation_timeout=timedelta(milliseconds=1)),
        )(CONTEXT, _binding()) as uow:
            await uow.commit()

    assert "db_commit" not in events
    assert events[-3:] == ["db_rollback", "db_close", "provider_abort"]


@pytest.mark.asyncio
async def test_shortened_precommit_permit_cannot_outlive_database_bound() -> None:
    events: list[str] = []
    clock = ManualClock(NOW)
    bounded = FakeBoundedFactory(events)
    provider = FakeProvider(clock=clock, events=events)
    provider.validation_ttl = timedelta(milliseconds=600)

    with pytest.raises(CommitFenceInvalidatedError, match="transaction bound"):
        async with _factory(clock=clock, provider=provider, bounded=bounded)(
            CONTEXT, _binding()
        ) as uow:
            await uow.commit()

    assert "db_commit" not in events
    assert events[-3:] == ["db_rollback", "db_close", "provider_abort"]


@pytest.mark.asyncio
async def test_cancellation_during_commit_never_false_releases_unknown_outcome() -> (
    None
):
    events: list[str] = []
    clock = ManualClock(NOW)
    bounded = FakeBoundedFactory(events)
    provider = FakeProvider(clock=clock, events=events)

    async def protected_work() -> None:
        async with _factory(clock=clock, provider=provider, bounded=bounded)(
            CONTEXT, _binding()
        ) as uow:
            bounded.created[0].block_commit = True
            await uow.commit()

    task = asyncio.create_task(protected_work())
    while not bounded.created:
        await asyncio.sleep(0)
    await bounded.created[0].commit_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert events[-2:] == ["db_rollback", "db_close"]
    assert "provider_abort" not in events
    assert "provider_finish" not in events


@pytest.mark.asyncio
async def test_unknown_commit_outcome_keeps_permit_unresolved_for_reconciliation() -> (
    None
):
    events: list[str] = []
    clock = ManualClock(NOW)
    bounded = FakeBoundedFactory(events)
    provider = FakeProvider(clock=clock, events=events)

    with pytest.raises(UnknownCommitOutcomeError):
        async with _factory(clock=clock, provider=provider, bounded=bounded)(
            CONTEXT, _binding()
        ) as uow:
            bounded.created[0].commit_error = UnknownCommitOutcomeError(
                "Commit outcome requires idempotent reconciliation."
            )
            await uow.commit()

    assert events[-2:] == ["db_rollback", "db_close"]
    assert "provider_abort" not in events
    assert "provider_finish" not in events
