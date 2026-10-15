"""PostgreSQL persistence for immutable disclosure validation facts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select

from spine.application.disclosure_revalidation import DisclosureIdempotencyConflict
from spine.domain.disclosure import DisclosureRevalidationResult
from spine.infrastructure.persistence.postgresql_mappings import (
    qa_disclosure_validation_results,
)


class PostgreSQLDisclosureValidationRepository:
    """Tenant-scoped append-only result repository."""

    def __init__(self, uow: Any) -> None:
        self._uow = uow

    async def resolve(
        self, *, workspace_id: UUID, environment_id: UUID, request_id: UUID
    ) -> DisclosureRevalidationResult | None:
        row = (
            await self._uow._execute(
                select(qa_disclosure_validation_results).where(
                    qa_disclosure_validation_results.c.workspace_id == workspace_id,
                    qa_disclosure_validation_results.c.environment_id == environment_id,
                    qa_disclosure_validation_results.c.request_id == request_id,
                )
            )
        ).mappings().first()
        return None if row is None else self._from_row(row)

    async def save(
        self, result: DisclosureRevalidationResult
    ) -> DisclosureRevalidationResult:
        current = await self.resolve(
            workspace_id=result.workspace_id,
            environment_id=result.environment_id,
            request_id=result.request_id,
        )
        if current is not None:
            if current.request_digest != result.request_digest:
                raise DisclosureIdempotencyConflict(
                    "Disclosure validation request conflicts with an immutable result."
                )
            return current
        await self._uow._execute(
            insert(qa_disclosure_validation_results).values(
                {
                    "request_id": result.request_id,
                    "workspace_id": result.workspace_id,
                    "environment_id": result.environment_id,
                    "request_digest": result.request_digest,
                    "checkpoint": result.checkpoint.value,
                    "disposition": result.disposition.value,
                    "failure_codes": [item.value for item in result.failure_codes],
                    "safe_reason": result.safe_reason,
                    "facts": [item.model_dump(mode="json") for item in result.facts],
                    "created_at": datetime.now(timezone.utc),
                }
            )
        )
        return result

    @staticmethod
    def _from_row(row: Any) -> DisclosureRevalidationResult:
        values = dict(row)
        values.pop("created_at", None)
        return DisclosureRevalidationResult.model_validate(values)


__all__ = ["PostgreSQLDisclosureValidationRepository"]
