"""Persistence port for immutable final Q&A disclosure results."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from spine.domain.capabilities.answer_disclosure import AnswerDisclosureRecord


class AnswerDisclosureRepository(Protocol):
    async def resolve(
        self, *, workspace_id: UUID, environment_id: UUID, request_id: UUID
    ) -> AnswerDisclosureRecord | None: ...

    async def save(self, record: AnswerDisclosureRecord) -> AnswerDisclosureRecord: ...


__all__ = ["AnswerDisclosureRepository"]
