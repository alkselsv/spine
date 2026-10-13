"""Application orchestration for reproducible canonical boundary capture."""

from __future__ import annotations

from collections.abc import Callable

from spine.application.persistence.boundary import (
    CanonicalBoundaryCaptureCommand,
    CanonicalBoundaryCaptureResult,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.retry import (
    RetryEligibility,
    TransactionRetryPolicy,
    run_transaction_with_retry,
)
from spine.application.persistence.unit_of_work import UnitOfWork, UnitOfWorkFactory


class CanonicalBoundaryCoordinator:
    """Capture a boundary in a fresh UoW for every retryable attempt."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        context_factory: Callable[
            [CanonicalBoundaryCaptureCommand], TrustedPersistenceContext
        ],
        retry_policy: TransactionRetryPolicy | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory
        self._retry_policy = retry_policy

    async def capture(
        self, command: CanonicalBoundaryCaptureCommand
    ) -> CanonicalBoundaryCaptureResult:
        context = self._context_factory(command)

        async def operation(
            uow: UnitOfWork, _attempt: int
        ) -> CanonicalBoundaryCaptureResult:
            return await uow.boundaries.capture(command)

        return await run_transaction_with_retry(
            self._uow_factory,
            context,
            operation,
            eligibility=RetryEligibility(
                idempotent=True,
                has_idempotency_key=True,
                reproducible=True,
            ),
            policy=self._retry_policy,
        )


def build_canonical_boundary_coordinator(
    *,
    uow_factory: UnitOfWorkFactory,
    context_factory: Callable[
        [CanonicalBoundaryCaptureCommand], TrustedPersistenceContext
    ],
    retry_policy: TransactionRetryPolicy | None = None,
) -> CanonicalBoundaryCoordinator:
    return CanonicalBoundaryCoordinator(
        uow_factory=uow_factory,
        context_factory=context_factory,
        retry_policy=retry_policy,
    )


__all__ = [
    "CanonicalBoundaryCoordinator",
    "build_canonical_boundary_coordinator",
]
