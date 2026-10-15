"""Persistence port for immutable stored-answer disclosure validations."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from spine.domain.disclosure import DisclosureRevalidationResult


class DisclosureValidationRepository(Protocol):
    async def resolve(
        self, *, workspace_id: UUID, environment_id: UUID, request_id: UUID
    ) -> DisclosureRevalidationResult | None: ...

    async def save(
        self, result: DisclosureRevalidationResult
    ) -> DisclosureRevalidationResult: ...


__all__ = ["DisclosureValidationRepository"]
