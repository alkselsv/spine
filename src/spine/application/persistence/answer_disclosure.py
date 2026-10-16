"""Persistence ports for immutable final Q&A disclosure results and delivery state."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from spine.domain.capabilities.answer_disclosure import AnswerDisclosureRecord
from spine.domain.capabilities.answer_disclosure import AcceptedGroundedAnswer


class AnswerDisclosureRepository(Protocol):
    async def resolve(
        self, *, workspace_id: UUID, environment_id: UUID, request_id: UUID
    ) -> AnswerDisclosureRecord | None: ...

    async def resolve_for_attempt(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        run_id: UUID,
        attempt_id: UUID,
    ) -> AnswerDisclosureRecord | None: ...

    async def save(self, record: AnswerDisclosureRecord) -> AnswerDisclosureRecord: ...

    async def resolve_delivery(
        self, *, workspace_id: UUID, environment_id: UUID, request_id: UUID
    ) -> UUID | None: ...

    async def record_delivery(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        request_id: UUID,
        output_reference: UUID,
    ) -> UUID: ...

    async def resolve_citation(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        citation_id: UUID,
    ) -> AcceptedGroundedAnswer | None: ...


__all__ = ["AnswerDisclosureRepository"]
