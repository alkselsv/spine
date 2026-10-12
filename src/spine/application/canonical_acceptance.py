"""Use-case orchestration for canonical source acceptance."""

from __future__ import annotations

from types import TracebackType
from typing import Callable, Protocol

from spine.application.persistence.canonical import (
    CanonicalAcceptanceCommand,
    CanonicalAcceptanceRepository,
    CanonicalAcceptanceResult,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.errors import OptimisticConflictError


class CanonicalAcceptanceFailedAttemptAudit(Protocol):
    async def record_failed_cas(
        self, command: CanonicalAcceptanceCommand, reason_code: str
    ) -> None: ...


class CanonicalAcceptanceUnitOfWork(Protocol):
    @property
    def canonical(self) -> CanonicalAcceptanceRepository: ...

    async def __aenter__(self) -> "CanonicalAcceptanceUnitOfWork": ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...


class CanonicalAcceptanceCoordinator:
    """Run one acceptance in a single UoW and audit failed CAS separately."""

    def __init__(
        self,
        uow_factory: Callable[[TrustedPersistenceContext], CanonicalAcceptanceUnitOfWork],
        context_factory: Callable[[CanonicalAcceptanceCommand], TrustedPersistenceContext],
        failed_attempt_audit: CanonicalAcceptanceFailedAttemptAudit | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory
        self._failed_attempt_audit = failed_attempt_audit

    async def accept(self, command: CanonicalAcceptanceCommand) -> CanonicalAcceptanceResult:
        try:
            async with self._uow_factory(self._context_factory(command)) as uow:
                result = await uow.canonical.accept(command)
                await uow.commit()
                return result
        except OptimisticConflictError:
            if self._failed_attempt_audit is not None:
                await self._failed_attempt_audit.record_failed_cas(
                    command, "stale_source_head"
                )
            raise


__all__ = [
    "CanonicalAcceptanceCoordinator",
    "CanonicalAcceptanceFailedAttemptAudit",
    "CanonicalAcceptanceUnitOfWork",
]
