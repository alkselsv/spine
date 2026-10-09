from __future__ import annotations

import asyncio

import pytest

from spine.application.persistence.errors import (
    ConstraintConflictError,
    IdempotencyConflictError,
    InvalidPersistenceContextError,
    RetryablePersistenceError,
    TransactionDeadlockError,
    TransactionSerializationError,
    UnexpectedPersistenceError,
)
from spine.application.persistence.retry import (
    RetryEligibility,
    TransactionRetryPolicy,
    run_with_transaction_retry,
)


def eligible() -> RetryEligibility:
    return RetryEligibility(
        idempotent=True,
        has_idempotency_key=True,
        reproducible=True,
    )


@pytest.mark.asyncio
async def test_retryable_failures_retry_complete_transaction_until_success() -> None:
    attempts: list[int] = []

    async def operation(attempt: int) -> str:
        attempts.append(attempt)
        if attempt < 3:
            raise TransactionDeadlockError("deadlock")
        return "committed"

    result = await run_with_transaction_retry(operation, eligibility=eligible())

    assert result == "committed"
    assert attempts == [1, 2, 3]


@pytest.mark.asyncio
async def test_retry_exhaustion_raises_last_retryable_error() -> None:
    attempts: list[int] = []

    async def operation(attempt: int) -> None:
        attempts.append(attempt)
        raise TransactionSerializationError("serialization conflict")

    with pytest.raises(RetryablePersistenceError):
        await run_with_transaction_retry(operation, eligibility=eligible())

    assert attempts == [1, 2, 3]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ConstraintConflictError("constraint"),
        IdempotencyConflictError("idempotency"),
        InvalidPersistenceContextError("context"),
        PermissionError("authorization"),
        RetryablePersistenceError("generic retryable category"),
        UnexpectedPersistenceError("unknown"),
    ],
)
async def test_non_retry_categories_are_attempted_once(error: BaseException) -> None:
    attempts: list[int] = []

    async def operation(attempt: int) -> None:
        attempts.append(attempt)
        raise error

    with pytest.raises(type(error)):
        await run_with_transaction_retry(operation, eligibility=eligible())

    assert attempts == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "retry_eligibility",
    [
        RetryEligibility(idempotent=False, has_idempotency_key=True, reproducible=True),
        RetryEligibility(idempotent=True, has_idempotency_key=False, reproducible=True),
        RetryEligibility(idempotent=True, has_idempotency_key=True, reproducible=False),
        RetryEligibility(
            idempotent=True,
            has_idempotency_key=True,
            reproducible=True,
            external_side_effects=True,
        ),
    ],
)
async def test_ineligible_work_is_attempted_once(
    retry_eligibility: RetryEligibility,
) -> None:
    attempts: list[int] = []

    async def operation(attempt: int) -> None:
        attempts.append(attempt)
        raise TransactionDeadlockError("deadlock")

    with pytest.raises(RetryablePersistenceError):
        await run_with_transaction_retry(operation, eligibility=retry_eligibility)

    assert attempts == [1]


@pytest.mark.asyncio
async def test_custom_attempt_limit_and_injected_backoff_are_deterministic() -> None:
    attempts: list[int] = []
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    policy = TransactionRetryPolicy(
        max_attempts=2,
        delay=lambda attempt: attempt / 10,
        sleep=sleep,
    )

    async def operation(attempt: int) -> None:
        attempts.append(attempt)
        raise TransactionDeadlockError("deadlock")

    with pytest.raises(RetryablePersistenceError):
        await run_with_transaction_retry(
            operation,
            eligibility=eligible(),
            policy=policy,
        )

    assert attempts == [1, 2]
    assert sleeps == [0.1]


@pytest.mark.asyncio
async def test_cancellation_propagates_without_retry() -> None:
    attempts: list[int] = []

    async def operation(attempt: int) -> None:
        attempts.append(attempt)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_with_transaction_retry(operation, eligibility=eligible())

    assert attempts == [1]
