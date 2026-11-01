"""Persistence port for immutable requirements acceptance decisions."""

from typing import Protocol
from uuid import UUID

from spine.application.requirements_acceptance import RequirementsAcceptanceReceipt


class RequirementsAcceptanceRepository(Protocol):
    async def get(
        self, *, command_id: UUID
    ) -> RequirementsAcceptanceReceipt | None: ...

    async def create(
        self, receipt: RequirementsAcceptanceReceipt
    ) -> RequirementsAcceptanceReceipt: ...


__all__ = ["RequirementsAcceptanceRepository"]
