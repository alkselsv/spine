"""Use-case orchestration for canonical source acceptance."""

from __future__ import annotations

from types import TracebackType
from typing import Callable, Protocol

from spine.domain.sources import SourceProcessingAdmission

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
    TransactionDeadlockError,
    TransactionSerializationError,
    UnknownCommitOutcomeError,
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


class CurrentProcessingAdmissionAuthority(Protocol):
    """Revalidate mode-specific authority while the canonical UoW is active."""

    async def revalidate(
        self,
        admission: SourceProcessingAdmission,
        transaction: CanonicalAcceptanceUnitOfWork,
    ) -> None: ...


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
        maximum_transaction_attempts: int = 3,
        processing_authority: CurrentProcessingAdmissionAuthority | None = None,
    ) -> None:
        if uow_factory is None or context_factory is None or failed_attempt_audit is None:
            raise TypeError(
                "Canonical acceptance requires UoW, trusted-context, and failed-audit dependencies."
            )
        self._uow_factory = uow_factory
        self._context_factory = context_factory
        self._failed_attempt_audit = failed_attempt_audit
        if maximum_transaction_attempts < 1:
            raise ValueError("maximum_transaction_attempts must be positive")
        self._maximum_transaction_attempts = maximum_transaction_attempts
        self._processing_authority = processing_authority

    async def accept(
        self, command: CanonicalAcceptanceCommand
    ) -> CanonicalAcceptanceResult:
        retry_failure: (
            TransactionDeadlockError
            | TransactionSerializationError
            | UnknownCommitOutcomeError
            | None
        ) = None
        for attempt in range(1, self._maximum_transaction_attempts + 1):
            try:
                async with self._uow_factory(self._context_factory(command)) as uow:
                    processing_admission = getattr(command, "processing_admission", None)
                    if processing_admission is not None:
                        if self._processing_authority is None:
                            raise CanonicalEligibilityError(
                                "Current processing authority is unavailable."
                            )
                        await self._processing_authority.revalidate(
                            processing_admission, uow
                        )
                    result = await uow.canonical.accept(command)
                    await uow.commit()
                    return result
            except (
                TransactionDeadlockError,
                TransactionSerializationError,
                UnknownCommitOutcomeError,
            ) as failure:
                retry_failure = failure
                if attempt < self._maximum_transaction_attempts:
                    continue
                reason = (
                    "canonical_unknown_commit_reconciliation_exhausted"
                    if isinstance(failure, UnknownCommitOutcomeError)
                    else "canonical_serialization_retry_exhausted"
                )
                await self._record_rejection(command, reason)
                raise
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
                await self._record_rejection(command, reason)
                raise
        assert retry_failure is not None
        raise retry_failure

    async def _record_rejection(
        self, command: CanonicalAcceptanceCommand, reason: str
    ) -> None:
        try:
            await self._failed_attempt_audit.record_failed_cas(command, reason)
        except Exception as audit_failure:
            raise CanonicalAcceptanceAuditUnavailableError() from audit_failure


def build_canonical_acceptance_coordinator(
    *,
    uow_factory: Callable[
        [TrustedPersistenceContext], CanonicalAcceptanceUnitOfWork
    ],
    context_factory: Callable[
        [CanonicalAcceptanceCommand], TrustedPersistenceContext
    ],
    failed_attempt_audit: CanonicalAcceptanceFailedAttemptAudit,
    maximum_transaction_attempts: int = 3,
    processing_authority: CurrentProcessingAdmissionAuthority | None = None,
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
        maximum_transaction_attempts=maximum_transaction_attempts,
        processing_authority=processing_authority,
    )


__all__ = [
    "CanonicalAcceptanceAuditUnavailableError",
    "CanonicalAcceptanceCoordinator",
    "CanonicalAcceptanceFailedAttemptAudit",
    "CanonicalAcceptanceUnitOfWork",
    "CurrentProcessingAdmissionAuthority",
    "build_canonical_acceptance_coordinator",
]
