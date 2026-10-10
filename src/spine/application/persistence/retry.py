"""Pure bounded retry policy for eligible persistence transactions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.errors import (
    TransactionDeadlockError,
    TransactionSerializationError,
)
from spine.application.persistence.unit_of_work import UnitOfWork, UnitOfWorkFactory


_ResultT = TypeVar("_ResultT")


@dataclass(frozen=True, slots=True)
class RetryEligibility:
    """Whether a complete transaction may be retried automatically."""

    idempotent: bool
    has_idempotency_key: bool
    reproducible: bool
    external_side_effects: bool = False

    def permits_retry(self) -> bool:
        return (
            self.idempotent
            and self.has_idempotency_key
            and self.reproducible
            and not self.external_side_effects
        )


@dataclass(frozen=True, slots=True)
class TransactionRetryPolicy:
    max_attempts: int = 3
    delay: Callable[[int], float] = lambda attempt: 0.0
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    def __post_init__(self) -> None:
        if not isinstance(self.max_attempts, int) or self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one.")

    def should_retry(
        self,
        *,
        error: BaseException,
        attempt: int,
        eligibility: RetryEligibility,
    ) -> bool:
        return (
            isinstance(error, (TransactionDeadlockError, TransactionSerializationError))
            and eligibility.permits_retry()
            and attempt < self.max_attempts
        )


async def run_with_transaction_retry(
    operation: Callable[[int], Awaitable[_ResultT]],
    *,
    eligibility: RetryEligibility,
    policy: TransactionRetryPolicy | None = None,
) -> _ResultT:
    """Run a complete transaction operation with deterministic bounded retries."""

    effective_policy = policy or TransactionRetryPolicy()
    attempt = 1
    while True:
        try:
            return await operation(attempt)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            if not effective_policy.should_retry(
                error=error,
                attempt=attempt,
                eligibility=eligibility,
            ):
                raise
            await effective_policy.sleep(effective_policy.delay(attempt))
            attempt += 1


async def run_transaction_with_retry(
    uow_factory: UnitOfWorkFactory,
    context: TrustedPersistenceContext,
    operation: Callable[[UnitOfWork, int], Awaitable[_ResultT]],
    *,
    eligibility: RetryEligibility,
    policy: TransactionRetryPolicy | None = None,
) -> _ResultT:
    """Retry an operation by recreating and committing its complete Unit of Work."""

    async def execute_attempt(attempt: int) -> _ResultT:
        async with uow_factory(context) as uow:
            result = await operation(uow, attempt)
            await uow.commit()
            return result

    return await run_with_transaction_retry(
        execute_attempt,
        eligibility=eligibility,
        policy=policy,
    )
