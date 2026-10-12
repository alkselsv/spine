"""Durable failed canonical-acceptance attempt auditing."""

from __future__ import annotations

from collections.abc import Callable

from spine.application.persistence.canonical import CanonicalAcceptanceCommand
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.domain.audit import AuditEvent


class UnitOfWorkCanonicalAcceptanceFailedAttemptAudit:
    """Append one failed-attempt event after the rejected UoW has rolled back."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        context_factory: Callable[
            [CanonicalAcceptanceCommand], TrustedPersistenceContext
        ],
        event_factory: Callable[[CanonicalAcceptanceCommand, str], AuditEvent],
    ) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory
        self._event_factory = event_factory

    async def record_failed_cas(
        self, command: CanonicalAcceptanceCommand, reason_code: str
    ) -> None:
        context = self._context_factory(command)
        event = self._event_factory(command, reason_code)
        async with self._uow_factory(context) as uow:
            await uow.audit.append(event)
            await uow.commit()


__all__ = ["UnitOfWorkCanonicalAcceptanceFailedAttemptAudit"]
