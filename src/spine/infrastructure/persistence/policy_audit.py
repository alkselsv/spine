"""Durable failed Access Policy attempt auditing."""

from __future__ import annotations

from collections.abc import Callable

from spine.application.access_policies import AccessPolicyActivationCommand
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.domain.audit import AuditEvent


class UnitOfWorkPolicyFailedAttemptAudit:
    """Append a failed-CAS event in a transaction separate from the failed command."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        context_factory: Callable[
            [AccessPolicyActivationCommand], TrustedPersistenceContext
        ],
        event_factory: Callable[
            [AccessPolicyActivationCommand, str], AuditEvent
        ],
    ) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory
        self._event_factory = event_factory

    async def record_failed_cas(
        self, command: AccessPolicyActivationCommand, reason_code: str
    ) -> None:
        context = self._context_factory(command)
        event = self._event_factory(command, reason_code)
        async with self._uow_factory(context) as uow:
            await uow.audit.append(event)
            await uow.commit()


__all__ = ["UnitOfWorkPolicyFailedAttemptAudit"]
