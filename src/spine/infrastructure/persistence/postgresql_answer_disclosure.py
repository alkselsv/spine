"""PostgreSQL adapter for immutable final Q&A disclosure results."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from spine.application.answer_disclosure import AnswerDisclosureIdempotencyConflict
from spine.domain.capabilities import AnswerDisclosureRecord
from spine.infrastructure.persistence.postgresql_mappings import (
    qa_answer_disclosure_deliveries,
    qa_answer_disclosure_records,
)


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
        if row is None:
            return None
        payload = AnswerDisclosureRecord.model_validate(row["payload"])
        if (
            payload.workspace_id != row["workspace_id"]
            or payload.environment_id != row["environment_id"]
            or payload.request_id != row["request_id"]
            or payload.outcome.value != row["outcome"]
        ):
            raise ValueError("answer disclosure record relational identity is inconsistent")
        return payload

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
            insert(qa_answer_disclosure_records)
            .values(
                {
                    "request_id": record.request_id,
                    "workspace_id": record.workspace_id,
                    "environment_id": record.environment_id,
                    "outcome": record.outcome.value,
                    "payload": record.model_dump(mode="json"),
                    "created_at": datetime.now(timezone.utc),
                }
            )
            .on_conflict_do_nothing(
                index_elements=[
                    qa_answer_disclosure_records.c.workspace_id,
                    qa_answer_disclosure_records.c.environment_id,
                    qa_answer_disclosure_records.c.request_id,
                ]
            )
        )
        stored = await self.resolve(
            workspace_id=record.workspace_id,
            environment_id=record.environment_id,
            request_id=record.request_id,
        )
        assert stored is not None
        if stored.request_digest != record.request_digest:
            raise AnswerDisclosureIdempotencyConflict(
                "Disclosure request conflicts with an immutable result."
            )
        return stored

    async def resolve_delivery(
        self, *, workspace_id: UUID, environment_id: UUID, request_id: UUID
    ) -> UUID | None:
        result = await self._uow._execute(
            select(qa_answer_disclosure_deliveries.c.output_reference).where(
                qa_answer_disclosure_deliveries.c.workspace_id == workspace_id,
                qa_answer_disclosure_deliveries.c.environment_id == environment_id,
                qa_answer_disclosure_deliveries.c.request_id == request_id,
            )
        )
        return result.scalar_one_or_none()

    async def record_delivery(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        request_id: UUID,
        output_reference: UUID,
    ) -> UUID:
        await self._uow._execute(
            insert(qa_answer_disclosure_deliveries)
            .values(
                {
                    "request_id": request_id,
                    "workspace_id": workspace_id,
                    "environment_id": environment_id,
                    "output_reference": output_reference,
                    "published_at": datetime.now(timezone.utc),
                }
            )
            .on_conflict_do_nothing(
                index_elements=[
                    qa_answer_disclosure_deliveries.c.workspace_id,
                    qa_answer_disclosure_deliveries.c.environment_id,
                    qa_answer_disclosure_deliveries.c.request_id,
                ]
            )
        )
        stored = await self.resolve_delivery(
            workspace_id=workspace_id,
            environment_id=environment_id,
            request_id=request_id,
        )
        assert stored is not None
        if stored != output_reference:
            raise AnswerDisclosureIdempotencyConflict(
                "Disclosure publication was recorded with a different output reference."
            )
        return stored


__all__ = ["PostgreSQLAnswerDisclosureRepository"]
