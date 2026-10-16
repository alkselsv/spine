"""In-memory feedback adapter and reference-only target registry."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from spine.application.feedback import (
    FeedbackRequester,
    FeedbackSubmissionResult,
    FeedbackTargetReference,
    FeedbackTargetResolver,
)
from spine.application.persistence.command_digest import CommandDigest
from spine.application.persistence.errors import IdempotencyConflictError
from spine.application.persistence.idempotency import IdempotencyKey
from spine.domain.feedback import FeedbackRecord, FeedbackTargetKind


@dataclass(frozen=True, slots=True)
class _Receipt:
    digest: str
    command_id: UUID
    record: FeedbackRecord


class InMemoryFeedbackTargetResolver(FeedbackTargetResolver):
    """Fake owning authority used by contract tests; it stores no content."""

    def __init__(self, targets: tuple[FeedbackTargetReference, ...] = ()) -> None:
        self._targets = {
            (item.workspace_id, item.environment_id, item.target_kind, item.target_id): item
            for item in targets
        }

    def add(self, target: FeedbackTargetReference) -> None:
        self._targets[(target.workspace_id, target.environment_id, target.target_kind, target.target_id)] = target

    async def resolve(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        requester: FeedbackRequester,
        target_kind: FeedbackTargetKind,
        target_id: UUID,
        run_id: UUID,
        message_id: UUID | None,
        citation_id: UUID | None,
    ) -> FeedbackTargetReference | None:
        del requester, run_id, message_id, citation_id
        return self._targets.get((workspace_id, environment_id, target_kind, target_id))


class InMemoryFeedbackRepository:
    """Scope-bound idempotent repository for immutable feedback records."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.records: dict[UUID, FeedbackRecord] = {}
        self._receipts: dict[tuple[UUID, UUID, str, str], _Receipt] = {}

    async def create(
        self,
        record: FeedbackRecord,
        *,
        command_id: UUID,
        idempotency_key: IdempotencyKey,
        digest: CommandDigest,
    ) -> FeedbackSubmissionResult:
        async with self._lock:
            key = (
                record.workspace_id,
                record.environment_id,
                digest.operation.value,
                idempotency_key.value,
            )
            existing = self._receipts.get(key)
            if existing is not None:
                if existing.digest != digest.value:
                    raise IdempotencyConflictError(
                        "The idempotency key conflicts with existing command."
                    )
                return _result(existing.command_id, existing.record, replay=True)
            self.records[record.feedback_id] = record
            self._receipts[key] = _Receipt(digest.value, command_id, record)
            return _result(command_id, record, replay=False)


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


__all__ = [
    "InMemoryFeedbackRepository",
    "InMemoryFeedbackTargetResolver",
]
