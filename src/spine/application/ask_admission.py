"""Application use case for durable, execution-free Ask admission."""

from __future__ import annotations

from dataclasses import dataclass

from spine.application.persistence.ask_admission import (
    AskAdmissionCommand,
    AskAdmissionResult,
    validate_ask_admission_context,
)
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.application.persistence.context import TrustedPersistenceContext


@dataclass(frozen=True, slots=True)
class PersistedAskAdmissionService:
    """Commit one admission and dispatch intent as one database transaction."""

    uow_factory: UnitOfWorkFactory

    async def admit(
        self,
        *,
        context: TrustedPersistenceContext,
        command: AskAdmissionCommand,
    ) -> AskAdmissionResult:
        validate_ask_admission_context(context, command)
        async with self.uow_factory(context) as uow:
            result = await uow.ask_admissions.admit(command)
            await uow.commit()
        return result


__all__ = ["PersistedAskAdmissionService"]
