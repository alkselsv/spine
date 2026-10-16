"""PostgreSQL adapter for immutable feedback records."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import insert, select

from spine.application.feedback import FeedbackSubmissionResult
from spine.application.persistence.command_digest import CommandDigest
from spine.application.persistence.idempotency import (
    IdempotencyKey,
    IdempotencyReplay,
    OpaqueResultReference,
    OwnedIdempotencyClaim,
)
from spine.domain.feedback import FeedbackRecord
from spine.infrastructure.persistence.postgresql_mappings import feedback_records


class PostgreSQLFeedbackRepository:
    """Persist feedback through the caller's already scoped Unit of Work."""

    def __init__(self, uow: Any) -> None:
        self._uow = uow
        self._owned_claims: dict[str, OwnedIdempotencyClaim] = {}

    async def create(
        self,
        record: FeedbackRecord,
        *,
        command_id: UUID,
        idempotency_key: IdempotencyKey,
        digest: CommandDigest,
    ) -> FeedbackSubmissionResult:
        claim = await self._uow.idempotency.claim(
            operation_schema_version=1,
            key=idempotency_key,
            digest=digest,
        )
        if isinstance(claim, IdempotencyReplay):
            existing = await self.resolve_feedback(
                workspace_id=record.workspace_id,
                environment_id=record.environment_id,
                feedback_id=claim.result.result_id,
            )
            if existing is None:
                raise LookupError("Feedback result is unavailable.")
            return _result(claim.receipt_id, existing, replay=True)
        self._owned_claims[digest.value] = claim
        await self._uow._execute(insert(feedback_records).values(self._values(record)))
        await self._uow.idempotency.complete(
            claim,
            OpaqueResultReference("feedback", record.feedback_id, 1),
        )
        self._owned_claims.pop(digest.value, None)
        return _result(claim.receipt_id, record, replay=False)

    async def resolve_feedback(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        feedback_id: UUID,
    ) -> FeedbackRecord | None:
        result = await self._uow._execute(
            select(feedback_records)
            .where(
                feedback_records.c.workspace_id == workspace_id,
                feedback_records.c.environment_id == environment_id,
                feedback_records.c.feedback_id == feedback_id,
            )
            .order_by(feedback_records.c.version.desc())
            .limit(1)
        )
        row = result.mappings().first()
        return None if row is None else FeedbackRecord.model_validate(dict(row))

    @staticmethod
    def _values(record: FeedbackRecord) -> dict[str, object]:
        return {
            "feedback_id": record.feedback_id,
            "workspace_id": record.workspace_id,
            "environment_id": record.environment_id,
            "version": record.version,
            "subject_id": record.subject_id,
            "trace_id": record.trace_id,
            "run_id": record.run_id,
            "message_id": record.message_id,
            "target_kind": record.target_kind.value,
            "target_id": record.target_id,
            "citation_id": record.citation_id,
            "outcome": record.outcome.value,
            "comment": record.comment,
            "reason": record.reason,
            "created_at": record.created_at,
        }


def _result(command_id: UUID, record: FeedbackRecord, *, replay: bool) -> FeedbackSubmissionResult:
    return FeedbackSubmissionResult(
        command_id=command_id,
        feedback_id=record.feedback_id,
        feedback_version=record.version,
        target_kind=record.target_kind,
        target_id=record.target_id,
        run_id=record.run_id,
        message_id=record.message_id,
        citation_id=record.citation_id,
        outcome=record.outcome,
        replay=replay,
    )


__all__ = ["PostgreSQLFeedbackRepository"]
