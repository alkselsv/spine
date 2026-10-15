"""PostgreSQL adapter for immutable final Q&A disclosure results."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select

from spine.application.answer_disclosure import AnswerDisclosureIdempotencyConflict
from spine.domain.capabilities import AnswerDisclosureRecord
from spine.infrastructure.persistence.postgresql_mappings import qa_answer_disclosure_records


class PostgreSQLAnswerDisclosureRepository:
    def __init__(self, uow: Any) -> None:
        self._uow = uow

    async def resolve(
        self, *, workspace_id: UUID, environment_id: UUID, request_id: UUID
    ) -> AnswerDisclosureRecord | None:
        result = await self._uow._execute(
            select(qa_answer_disclosure_records).where(
                qa_answer_disclosure_records.c.workspace_id == workspace_id,
                qa_answer_disclosure_records.c.environment_id == environment_id,
                qa_answer_disclosure_records.c.request_id == request_id,
            )
        )
        row = result.mappings().first()
        return None if row is None else AnswerDisclosureRecord.model_validate(row["payload"])

    async def save(self, record: AnswerDisclosureRecord) -> AnswerDisclosureRecord:
        current = await self.resolve(
            workspace_id=record.workspace_id,
            environment_id=record.environment_id,
            request_id=record.request_id,
        )
        if current is not None:
            if current.request_digest != record.request_digest:
                raise AnswerDisclosureIdempotencyConflict(
                    "Disclosure request conflicts with an immutable result."
                )
            return current
        await self._uow._execute(
            insert(qa_answer_disclosure_records).values(
                {
                    "request_id": record.request_id,
                    "workspace_id": record.workspace_id,
                    "environment_id": record.environment_id,
                    "outcome": record.outcome.value,
                    "payload": record.model_dump(mode="json"),
                    "created_at": datetime.now(timezone.utc),
                }
            )
        )
        return record


__all__ = ["PostgreSQLAnswerDisclosureRepository"]
