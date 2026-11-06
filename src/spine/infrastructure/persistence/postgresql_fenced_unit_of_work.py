"""Bounded PostgreSQL Unit of Work ownership for external commit permits.

The permit is acquired before PostgreSQL work starts.  The database transaction
is then given an explicit server-side ``transaction_timeout`` that fits wholly
inside the permit's safe validity window.  Permit release happens only after the
database Unit of Work has rolled back or committed and closed its session.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import TracebackType
from typing import Any, Awaitable, Callable, Protocol

from spine.application.commit_fence import CommitFenceBinding
from spine.application.commit_fence_provider_port import (
    CommitFenceOperationResult,
    CommitFenceOperationState,
    CommitFencePermit,
    ExternalCommitFencePort,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.errors import (
    CommitFenceInvalidatedError,
    CommitFenceReleaseError,
    InvalidCommitFenceConfigurationError,
    UnknownCommitOutcomeError,
    UnitOfWorkLifecycleError,
)


@dataclass(frozen=True, slots=True)
class PostgreSQLCommitFenceTiming:
    """Deployment-qualified timing values; deliberately has no defaults."""

    transaction_timeout: timedelta
    provider_operation_timeout: timedelta
    renewal_threshold: timedelta
    clock_uncertainty_margin: timedelta

    def __post_init__(self) -> None:
        durations = (
            self.transaction_timeout,
            self.provider_operation_timeout,
            self.renewal_threshold,
            self.clock_uncertainty_margin,
        )
        if any(not isinstance(value, timedelta) for value in durations):
            raise InvalidCommitFenceConfigurationError(
                "Commit-fence timing configuration is invalid."
            )
        if self.transaction_timeout <= timedelta(0):
            raise InvalidCommitFenceConfigurationError(
                "Commit-fence timing configuration is invalid."
            )
        if self.provider_operation_timeout <= timedelta(0):
            raise InvalidCommitFenceConfigurationError(
                "Commit-fence timing configuration is invalid."
            )
        if self.renewal_threshold < timedelta(0):
            raise InvalidCommitFenceConfigurationError(
                "Commit-fence timing configuration is invalid."
            )
        if self.clock_uncertainty_margin <= timedelta(0):
            raise InvalidCommitFenceConfigurationError(
                "Commit-fence timing configuration is invalid."
            )
        if self.transaction_timeout.microseconds % 1000:
            raise InvalidCommitFenceConfigurationError(
                "Commit-fence transaction timeout must use whole milliseconds."
            )


class _BoundedPostgreSQLUnitOfWork(Protocol):
    async def __aenter__(self) -> Any: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class BoundedPostgreSQLUnitOfWorkFactory(Protocol):
    def create_bounded(
        self,
        context: TrustedPersistenceContext,
        *,
        transaction_timeout: timedelta,
    ) -> _BoundedPostgreSQLUnitOfWork: ...


class PostgreSQLFencedUnitOfWork:
    """Own one external permit and one shorter PostgreSQL transaction."""

    def __init__(
        self,
        *,
        uow_factory: BoundedPostgreSQLUnitOfWorkFactory,
        context: TrustedPersistenceContext,
        binding: CommitFenceBinding,
        provider: ExternalCommitFencePort,
        timing: PostgreSQLCommitFenceTiming,
        clock: Callable[[], datetime],
    ) -> None:
        self._uow_factory = uow_factory
        self._context = context
        self._binding = binding
        self._provider = provider
        self._timing = timing
        self._clock = clock
        self._uow: _BoundedPostgreSQLUnitOfWork | None = None
        self._permit: CommitFencePermit | None = None
        self._entered = False
        self._commit_outcome_unknown = False
        self._committed = False
        self._closed = False
        self._recovery_pending = False

    async def __aenter__(self) -> PostgreSQLFencedUnitOfWork:
        if self._entered or self._closed:
            raise UnitOfWorkLifecycleError(
                "Fenced Unit of Work cannot be entered more than once."
            )
        self._entered = True
        try:
            acquired = await self._provider_call(self._provider.acquire(self._binding))
            self._permit = self._require_acquired(acquired)
            self._permit = await self._renew_if_needed(self._permit)
            self._require_safe_window(self._permit)

            self._uow = self._uow_factory.create_bounded(
                self._context,
                transaction_timeout=self._timing.transaction_timeout,
            )
            await self._uow.__aenter__()
            # Entry and server configuration consume part of the permit.  Check
            # again before any protected repository is exposed to the caller.
            self._require_safe_window(self._permit)
            return self
        except BaseException as error:
            await self._cleanup_failed_entry(error)
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._closed:
            return
        self._closed = True
        uow = self._uow
        permit = self._permit
        if uow is None or permit is None:
            return

        # The database session must be closed before the provider is told that
        # its permit can no longer protect a commit.
        await uow.__aexit__(exc_type, exc_value, traceback)
        if self._commit_outcome_unknown:
            # A possibly durable commit cannot truthfully be released as either
            # FINISH or ABORT.  Keep the permit unresolved for expiry and the
            # canonical reconciliation owned by the retry/recovery layer.
            return
        operation = (
            self._provider.finish(permit)
            if self._committed
            else self._provider.abort(permit)
        )
        try:
            await self._release_permit(operation, permit)
        except BaseException:
            self._recovery_pending = True
            if exc_value is None:
                raise

    @property
    def recovery_pending(self) -> bool:
        """Whether the permit still needs an explicit, safe settlement."""

        return self._recovery_pending

    @property
    def commit_outcome_unknown(self) -> bool:
        """Whether PostgreSQL commit may have become durable."""

        return self._commit_outcome_unknown

    @property
    def committed(self) -> bool:
        """Whether this UoW observed a successful database commit."""

        return self._committed

    async def settle_recovery(self, *, committed: bool) -> None:
        """Resolve a pending permit only after the canonical outcome is known.

        An unresolved database commit must first be classified by the canonical
        idempotency receipt.  This method deliberately accepts that decision as
        an explicit argument and never infers it from a lost response.
        """

        if not self._recovery_pending and not self._commit_outcome_unknown:
            return
        if not self._closed or self._permit is None:
            raise UnitOfWorkLifecycleError(
                "Commit-fence recovery requires a closed Unit of Work."
            )
        operation = (
            self._provider.finish(self._permit)
            if committed
            else self._provider.abort(self._permit)
        )
        await self._release_permit(operation, self._permit)
        self._recovery_pending = False
        self._commit_outcome_unknown = False

    async def commit(self) -> None:
        uow, permit = self._require_active()
        checked = await self._provider_call(
            self._provider.validate_before_commit(
                permit,
                evaluated_at=self._now(),
            )
        )
        if (
            not checked.permits_commit
            or checked.binding != self._binding
            or checked.permit is None
            or checked.permit.permit_reference != permit.permit_reference
        ):
            raise CommitFenceInvalidatedError(
                "Commit-fence permit is not valid for PostgreSQL commit."
            )
        self._permit = checked.permit
        # A provider may return the same opaque reference with a shorter expiry.
        # Conservatively require the complete configured DB bound again; merely
        # clearing the uncertainty margin could let PostgreSQL commit later than
        # the newly validated permit.
        self._require_safe_window(self._permit)
        try:
            await uow.commit()
        except (asyncio.CancelledError, UnknownCommitOutcomeError):
            self._commit_outcome_unknown = True
            self._recovery_pending = True
            raise
        self._committed = True

    async def rollback(self) -> None:
        uow, _ = self._require_active()
        await uow.rollback()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        uow, _ = self._require_active()
        return getattr(uow, name)

    def _require_active(
        self,
    ) -> tuple[_BoundedPostgreSQLUnitOfWork, CommitFencePermit]:
        if self._closed or self._uow is None or self._permit is None:
            raise UnitOfWorkLifecycleError("Fenced Unit of Work is not active.")
        if self._committed:
            raise UnitOfWorkLifecycleError("Fenced Unit of Work is not active.")
        return self._uow, self._permit

    async def _renew_if_needed(self, permit: CommitFencePermit) -> CommitFencePermit:
        remaining = permit.valid_until - self._now()
        renewal_boundary = (
            self._timing.transaction_timeout
            + self._timing.clock_uncertainty_margin
            + self._timing.renewal_threshold
        )
        if remaining > renewal_boundary:
            return permit
        renew = getattr(self._provider, "renew", None)
        if not callable(renew):
            raise CommitFenceInvalidatedError(
                "Commit-fence permit cannot cover the configured transaction bound."
            )
        renewed = await self._provider_call(renew(permit, evaluated_at=self._now()))
        if (
            renewed.state is not CommitFenceOperationState.RENEWED
            or renewed.permit is None
            or renewed.binding != self._binding
            or renewed.permit.permit_reference != permit.permit_reference
        ):
            raise CommitFenceInvalidatedError(
                "Commit-fence renewal did not establish a safe transaction window."
            )
        return renewed.permit

    def _require_acquired(
        self, result: CommitFenceOperationResult
    ) -> CommitFencePermit:
        if (
            result.state is not CommitFenceOperationState.ACQUIRED
            or result.permit is None
            or result.binding != self._binding
            or result.provider_contract_version
            != self._provider.provider_contract_version
        ):
            raise CommitFenceInvalidatedError(
                "Commit-fence provider denied the protected transaction."
            )
        return result.permit

    def _require_safe_window(self, permit: CommitFencePermit) -> None:
        if (
            permit.valid_until - self._now()
            <= self._timing.transaction_timeout + self._timing.clock_uncertainty_margin
        ):
            raise CommitFenceInvalidatedError(
                "Commit-fence permit cannot cover the configured transaction bound."
            )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise InvalidCommitFenceConfigurationError(
                "Commit-fence clock must return a UTC timestamp."
            )
        return now

    async def _provider_call(
        self, operation: Awaitable[CommitFenceOperationResult]
    ) -> CommitFenceOperationResult:
        try:
            return await asyncio.wait_for(
                operation,
                timeout=self._timing.provider_operation_timeout.total_seconds(),
            )
        except asyncio.TimeoutError:
            raise CommitFenceInvalidatedError(
                "Commit-fence provider operation timed out."
            ) from None

    async def _cleanup_failed_entry(self, error: BaseException) -> None:
        uow = self._uow
        permit = self._permit
        self._closed = True
        if uow is not None:
            await uow.__aexit__(type(error), error, error.__traceback__)
        if permit is not None:
            try:
                await self._release_permit(self._provider.abort(permit), permit)
            except BaseException:
                self._recovery_pending = True
                # Entry already failed closed.  An inconclusive release remains
                # unavailable until provider expiry/reconciliation.
                pass

    async def _release_permit(
        self,
        operation: Awaitable[CommitFenceOperationResult],
        permit: CommitFencePermit,
    ) -> CommitFenceOperationResult:
        try:
            result = await self._provider_call(operation)
        except CommitFenceInvalidatedError:
            raise CommitFenceReleaseError(
                "Commit-fence permit release is inconclusive after database close."
            ) from None
        self._require_released(result, permit)
        return result

    @staticmethod
    def _require_released(
        result: CommitFenceOperationResult,
        permit: CommitFencePermit,
    ) -> None:
        if (
            result.state is not CommitFenceOperationState.RELEASED
            or result.released_permit_reference != permit.permit_reference
        ):
            raise CommitFenceReleaseError(
                "Commit-fence permit release is inconclusive after database close."
            )


class PostgreSQLFencedUnitOfWorkFactory:
    """Bind one qualified provider/timing profile to a bounded UoW factory."""

    def __init__(
        self,
        *,
        uow_factory: BoundedPostgreSQLUnitOfWorkFactory,
        provider: ExternalCommitFencePort,
        timing: PostgreSQLCommitFenceTiming,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if timing is None or not isinstance(timing, PostgreSQLCommitFenceTiming):
            raise InvalidCommitFenceConfigurationError(
                "Commit-fence timing configuration is required."
            )
        self._uow_factory = uow_factory
        self._provider = provider
        self._timing = timing
        self._clock = clock

    def __call__(
        self,
        context: TrustedPersistenceContext,
        binding: CommitFenceBinding,
    ) -> PostgreSQLFencedUnitOfWork:
        return PostgreSQLFencedUnitOfWork(
            uow_factory=self._uow_factory,
            context=context,
            binding=binding,
            provider=self._provider,
            timing=self._timing,
            clock=self._clock,
        )


__all__ = [
    "BoundedPostgreSQLUnitOfWorkFactory",
    "PostgreSQLCommitFenceTiming",
    "PostgreSQLFencedUnitOfWork",
    "PostgreSQLFencedUnitOfWorkFactory",
]
