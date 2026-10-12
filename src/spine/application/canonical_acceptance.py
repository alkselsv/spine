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
from spine.application.persistence.errors import (
    CanonicalAcceptanceAuditUnavailableError,
    CanonicalAuthorityConflictError,
    CanonicalEligibilityError,
)


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
        uow_factory: Callable[
            [TrustedPersistenceContext], CanonicalAcceptanceUnitOfWork
        ],
        context_factory: Callable[
            [CanonicalAcceptanceCommand], TrustedPersistenceContext
        ],
        failed_attempt_audit: CanonicalAcceptanceFailedAttemptAudit,
    ) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory
        self._failed_attempt_audit = failed_attempt_audit

    async def accept(
        self, command: CanonicalAcceptanceCommand
    ) -> CanonicalAcceptanceResult:
        try:
            async with self._uow_factory(self._context_factory(command)) as uow:
                result = await uow.canonical.accept(command)
                await uow.commit()
                return result
        except (
            CanonicalAuthorityConflictError,
            CanonicalEligibilityError,
            OptimisticConflictError,
        ) as rejection:
            if isinstance(rejection, CanonicalAuthorityConflictError):
                reason = "canonical_authority_conflict"
            elif isinstance(rejection, CanonicalEligibilityError):
                reason = "canonical_evidence_ineligible"
            else:
                reason = "canonical_order_or_cas_conflict"
            try:
                await self._failed_attempt_audit.record_failed_cas(command, reason)
            except Exception as audit_failure:
                raise CanonicalAcceptanceAuditUnavailableError() from audit_failure
            raise


def build_canonical_acceptance_coordinator(
    *,
    uow_factory: Callable[
        [TrustedPersistenceContext], CanonicalAcceptanceUnitOfWork
    ],
    context_factory: Callable[
        [CanonicalAcceptanceCommand], TrustedPersistenceContext
    ],
    failed_attempt_audit: CanonicalAcceptanceFailedAttemptAudit,
) -> CanonicalAcceptanceCoordinator:
    """Build the only application entry point for canonical acceptance.

    Keeping all three dependencies mandatory makes a production composition that
    omits failed-attempt auditing impossible without an explicit type/runtime
    error.  Raw repository ports remain available only inside the supplied UoW.
    """

    return CanonicalAcceptanceCoordinator(
        uow_factory=uow_factory,
        context_factory=context_factory,
        failed_attempt_audit=failed_attempt_audit,
    )


__all__ = [
    "CanonicalAcceptanceAuditUnavailableError",
    "CanonicalAcceptanceCoordinator",
    "CanonicalAcceptanceFailedAttemptAudit",
    "CanonicalAcceptanceUnitOfWork",
    "build_canonical_acceptance_coordinator",
]
