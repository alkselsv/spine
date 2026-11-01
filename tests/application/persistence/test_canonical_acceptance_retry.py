from __future__ import annotations

from types import SimpleNamespace, TracebackType

import pytest

from spine.application.canonical_acceptance import CanonicalAcceptanceCoordinator
from spine.application.persistence.errors import (
    CanonicalEligibilityError,
    TransactionDeadlockError,
    TransactionSerializationError,
    UnknownCommitOutcomeError,
)


class Repository:
    def __init__(self, failures: list[Exception], result: object) -> None:
        self.failures = failures
        self.result = result
        self.attempts = 0

    async def accept(self, command: object) -> object:
        del command
        self.attempts += 1
        if self.failures:
            raise self.failures.pop(0)
        return self.result


class UnitOfWork:
    def __init__(
        self, repository: Repository, commit_failure: Exception | None = None
    ) -> None:
        self.repository = repository
        self.committed = False
        self.commit_failure = commit_failure

    @property
    def canonical(self) -> Repository:
        return self.repository

    async def __aenter__(self) -> "UnitOfWork":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback

    async def commit(self) -> None:
        if self.commit_failure is not None:
            raise self.commit_failure
        self.committed = True


class FailedAudit:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    async def record_failed_cas(self, command: object, reason_code: str) -> None:
        del command
        self.reasons.append(reason_code)


class ProcessingAuthority:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.transactions: list[object] = []

    async def revalidate(self, admission: object, transaction: object) -> None:
        assert admission is not None
        self.transactions.append(transaction)
        if self.failure is not None:
            raise self.failure


@pytest.mark.asyncio
async def test_serialization_retry_reopens_uow_and_returns_one_result() -> None:
    expected = object()
    repository = Repository(
        [TransactionSerializationError("retry"), TransactionDeadlockError("retry")],
        expected,
    )
    uows: list[UnitOfWork] = []

    def factory(context: object) -> UnitOfWork:
        del context
        uow = UnitOfWork(repository)
        uows.append(uow)
        return uow

    audit = FailedAudit()
    coordinator = CanonicalAcceptanceCoordinator(
        uow_factory=factory,
        context_factory=lambda command: object(),
        failed_attempt_audit=audit,
        maximum_transaction_attempts=3,
    )

    result = await coordinator.accept(object())  # type: ignore[arg-type]

    assert result is expected
    assert repository.attempts == 3
    assert [uow.committed for uow in uows] == [False, False, True]
    assert audit.reasons == []


@pytest.mark.asyncio
async def test_serialization_retry_exhaustion_fails_closed_and_is_audited() -> None:
    repository = Repository(
        [TransactionDeadlockError("retry"), TransactionDeadlockError("retry")],
        object(),
    )
    audit = FailedAudit()
    coordinator = CanonicalAcceptanceCoordinator(
        uow_factory=lambda context: UnitOfWork(repository),
        context_factory=lambda command: object(),
        failed_attempt_audit=audit,
        maximum_transaction_attempts=2,
    )

    with pytest.raises(TransactionDeadlockError):
        await coordinator.accept(object())  # type: ignore[arg-type]

    assert repository.attempts == 2
    assert audit.reasons == ["canonical_serialization_retry_exhausted"]


@pytest.mark.asyncio
async def test_unknown_commit_reconciles_with_same_command_identity() -> None:
    expected = object()
    repository = Repository([], expected)
    attempts = 0

    def factory(context: object) -> UnitOfWork:
        nonlocal attempts
        del context
        attempts += 1
        return UnitOfWork(
            repository,
            UnknownCommitOutcomeError("unknown") if attempts == 1 else None,
        )

    audit = FailedAudit()
    command = object()
    coordinator = CanonicalAcceptanceCoordinator(
        uow_factory=factory,
        context_factory=lambda candidate: object(),
        failed_attempt_audit=audit,
        maximum_transaction_attempts=2,
    )

    result = await coordinator.accept(command)  # type: ignore[arg-type]

    assert result is expected
    assert repository.attempts == 2
    assert attempts == 2
    assert audit.reasons == []


@pytest.mark.asyncio
async def test_v2_processing_authority_is_revalidated_inside_each_uow() -> None:
    expected = object()
    repository = Repository([], expected)
    authority = ProcessingAuthority()
    uow = UnitOfWork(repository)
    coordinator = CanonicalAcceptanceCoordinator(
        uow_factory=lambda context: uow,
        context_factory=lambda command: object(),
        failed_attempt_audit=FailedAudit(),
        processing_authority=authority,  # type: ignore[arg-type]
    )

    result = await coordinator.accept(  # type: ignore[arg-type]
        SimpleNamespace(processing_admission=object())
    )

    assert result is expected
    assert authority.transactions == [uow]


@pytest.mark.asyncio
async def test_v2_missing_processing_authority_fails_closed_before_persistence() -> None:
    repository = Repository([], object())
    audit = FailedAudit()
    coordinator = CanonicalAcceptanceCoordinator(
        uow_factory=lambda context: UnitOfWork(repository),
        context_factory=lambda command: object(),
        failed_attempt_audit=audit,
    )

    with pytest.raises(CanonicalEligibilityError):
        await coordinator.accept(  # type: ignore[arg-type]
            SimpleNamespace(processing_admission=object())
        )

    assert repository.attempts == 0
    assert audit.reasons == ["canonical_evidence_ineligible"]


@pytest.mark.asyncio
async def test_v2_unavailable_mode_authority_fails_closed_in_uow() -> None:
    repository = Repository([], object())
    audit = FailedAudit()
    authority = ProcessingAuthority(
        CanonicalEligibilityError("Processing authority is unavailable.")
    )
    coordinator = CanonicalAcceptanceCoordinator(
        uow_factory=lambda context: UnitOfWork(repository),
        context_factory=lambda command: object(),
        failed_attempt_audit=audit,
        processing_authority=authority,  # type: ignore[arg-type]
    )

    with pytest.raises(CanonicalEligibilityError):
        await coordinator.accept(  # type: ignore[arg-type]
            SimpleNamespace(processing_admission=object())
        )

    assert repository.attempts == 0
    assert len(authority.transactions) == 1
    assert audit.reasons == ["canonical_evidence_ineligible"]
