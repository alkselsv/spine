"""In-memory Q&A session repository for offline tests and local composition."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from spine.application.persistence.command_digest import CommandDigest
from spine.application.persistence.errors import IdempotencyConflictError
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.qa_sessions import (
    QAMessagePage,
    QAMessageNotFound,
    QAMessagePosition,
    QAMutationResult,
    QASessionPage,
    QASessionNotFound,
    QASessionPosition,
    QASessionRepository,
    QASessionVersionConflict,
)
from spine.domain.capabilities import AnswerQuestionOutcome
from spine.domain.qa_sessions import QAMessage, QAMessageState, QASession, QASessionStatus


@dataclass(frozen=True, slots=True)
class _Receipt:
    operation: str
    workspace_id: UUID
    environment_id: UUID
    key: str
    digest: str
    command_id: UUID
    entity_id: UUID


class InMemoryQASessionRepository(QASessionRepository):
    """Transactional-shape fake with scope-bound idempotency semantics."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._sessions: dict[tuple[UUID, UUID, UUID], QASession] = {}
        self._session_versions: dict[tuple[UUID, UUID, UUID], list[QASession]] = {}
        self._messages: dict[tuple[UUID, UUID, UUID], QAMessage] = {}
        self._message_versions: dict[tuple[UUID, UUID, UUID], list[QAMessage]] = {}
        self._receipts: dict[tuple[UUID, UUID, str, str], _Receipt] = {}

    async def create_session(
        self,
        session: QASession,
        *,
        command_id: UUID,
        idempotency_key: IdempotencyKey,
        digest: CommandDigest,
    ) -> QAMutationResult[QASession]:
        async with self._lock:
            replay = self._replay(digest, session.workspace_id, session.environment_id, idempotency_key)
            if replay is not None:
                current = self._sessions[(session.workspace_id, session.environment_id, replay.entity_id)]
                return QAMutationResult(replay.command_id, current, True)
            scope_key = (session.workspace_id, session.environment_id, session.session_id)
            self._sessions[scope_key] = session
            self._session_versions[scope_key] = [session]
            self._record(digest, session.workspace_id, session.environment_id, idempotency_key, command_id, session.session_id)
            return QAMutationResult(command_id, session, False)

    async def resolve_session(
        self, *, workspace_id: UUID, environment_id: UUID, session_id: UUID
    ) -> QASession | None:
        async with self._lock:
            return self._sessions.get((workspace_id, environment_id, session_id))

    async def list_sessions(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        limit: int,
        after: QASessionPosition | None,
    ) -> QASessionPage[QASession]:
        async with self._lock:
            values = sorted(
                (
                    value
                    for (workspace, environment, _), value in self._sessions.items()
                    if workspace == workspace_id and environment == environment_id
                ),
                key=lambda item: (item.created_at, item.session_id),
            )
            if after is not None:
                values = [
                    item for item in values
                    if (item.created_at, item.session_id) > (after.created_at, after.session_id)
                ]
            return QASessionPage(tuple(values[:limit]), len(values) > limit)

    async def restore_session(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        session_id: UUID,
        expected_version: int,
        restored_at: datetime,
        command_id: UUID,
        idempotency_key: IdempotencyKey,
        digest: CommandDigest,
    ) -> QAMutationResult[QASession]:
        async with self._lock:
            replay = self._replay(digest, workspace_id, environment_id, idempotency_key)
            if replay is not None:
                return QAMutationResult(
                    replay.command_id,
                    self._sessions[(workspace_id, environment_id, replay.entity_id)],
                    True,
                )
            key = (workspace_id, environment_id, session_id)
            current = self._sessions.get(key)
            if current is None:
                return self._not_found_session()
            if current.version != expected_version:
                raise QASessionVersionConflict("The session version is stale.")
            updated = current
            if current.status is QASessionStatus.ARCHIVED:
                updated = current.model_copy(
                    update={"version": current.version + 1, "status": QASessionStatus.ACTIVE, "updated_at": restored_at}
                )
                self._sessions[key] = updated
                self._session_versions[key].append(updated)
            self._record(digest, workspace_id, environment_id, idempotency_key, command_id, session_id)
            return QAMutationResult(command_id, updated, False)

    async def create_pending_message(
        self,
        message: QAMessage,
        *,
        command_id: UUID,
        idempotency_key: IdempotencyKey,
        digest: CommandDigest,
    ) -> QAMutationResult[QAMessage]:
        async with self._lock:
            replay = self._replay(digest, message.workspace_id, message.environment_id, idempotency_key)
            if replay is not None:
                current = self._messages[(message.workspace_id, message.environment_id, replay.entity_id)]
                return QAMutationResult(replay.command_id, current, True)
            session_key = (message.workspace_id, message.environment_id, message.session_id)
            if session_key not in self._sessions:
                raise QASessionNotFound()
            ordinal = 1 + max(
                (item.ordinal for item in self._messages.values() if (item.workspace_id, item.environment_id, item.session_id) == session_key),
                default=0,
            )
            if message.ordinal != ordinal:
                message = message.model_copy(update={"ordinal": ordinal})
            key = (message.workspace_id, message.environment_id, message.message_id)
            self._messages[key] = message
            self._message_versions[key] = [message]
            self._record(digest, message.workspace_id, message.environment_id, idempotency_key, command_id, message.message_id)
            return QAMutationResult(command_id, message, False)

    async def resolve_message(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        session_id: UUID,
        message_id: UUID,
    ) -> QAMessage | None:
        async with self._lock:
            message = self._messages.get((workspace_id, environment_id, message_id))
            return message if message is not None and message.session_id == session_id else None

    async def list_messages(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        session_id: UUID,
        limit: int,
        after: QAMessagePosition | None,
    ) -> QAMessagePage[QAMessage]:
        async with self._lock:
            values = sorted(
                (
                    item for (workspace, environment, _), item in self._messages.items()
                    if workspace == workspace_id and environment == environment_id and item.session_id == session_id
                ),
                key=lambda item: (item.ordinal, item.message_id),
            )
            if after is not None:
                values = [item for item in values if (item.ordinal, item.message_id) > (after.ordinal, after.message_id)]
            return QAMessagePage(tuple(values[:limit]), len(values) > limit)

    async def link_message_to_run(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        session_id: UUID,
        message_id: UUID,
        run_id: UUID,
        command_id: UUID,
        idempotency_key: IdempotencyKey,
        digest: CommandDigest,
    ) -> QAMutationResult[QAMessage]:
        async with self._lock:
            replay = self._replay(digest, workspace_id, environment_id, idempotency_key)
            if replay is not None:
                return QAMutationResult(replay.command_id, self._message_for(workspace_id, environment_id, session_id, replay.entity_id), True)
            current = self._message_for(workspace_id, environment_id, session_id, message_id)
            if current.state is QAMessageState.TERMINAL:
                raise QASessionVersionConflict("The message is already terminal.")
            if current.run_id is not None and current.run_id != run_id:
                raise QASessionVersionConflict("The message is already linked to another run.")
            updated = current.model_copy(update={"version": current.version + 1, "run_id": run_id, "state": QAMessageState.RUNNING, "updated_at": current.updated_at})
            self._messages[(workspace_id, environment_id, message_id)] = updated
            self._message_versions[(workspace_id, environment_id, message_id)].append(updated)
            self._record(digest, workspace_id, environment_id, idempotency_key, command_id, message_id)
            return QAMutationResult(command_id, updated, False)

    async def complete_message(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        session_id: UUID,
        message_id: UUID,
        answer: str | None,
        outcome: AnswerQuestionOutcome,
        cancelled: bool,
        completed_at: datetime,
        command_id: UUID,
        idempotency_key: IdempotencyKey,
        digest: CommandDigest,
    ) -> QAMutationResult[QAMessage]:
        async with self._lock:
            replay = self._replay(digest, workspace_id, environment_id, idempotency_key)
            if replay is not None:
                return QAMutationResult(replay.command_id, self._message_for(workspace_id, environment_id, session_id, replay.entity_id), True)
            current = self._message_for(workspace_id, environment_id, session_id, message_id)
            if current.state is QAMessageState.TERMINAL:
                raise QASessionVersionConflict("The message is already terminal.")
            updated = current.model_copy(
                update={
                    "version": current.version + 1,
                    "state": QAMessageState.TERMINAL,
                    "answer": answer,
                    "terminal_outcome": outcome,
                    "cancelled": cancelled,
                    "updated_at": completed_at,
                }
            )
            self._messages[(workspace_id, environment_id, message_id)] = updated
            self._message_versions[(workspace_id, environment_id, message_id)].append(updated)
            self._record(digest, workspace_id, environment_id, idempotency_key, command_id, message_id)
            return QAMutationResult(command_id, updated, False)

    async def archive_for_testing(self, session_id: UUID, workspace_id: UUID, environment_id: UUID) -> None:
        async with self._lock:
            key = (workspace_id, environment_id, session_id)
            current = self._sessions[key]
            archived = current.model_copy(update={"version": current.version + 1, "status": QASessionStatus.ARCHIVED})
            self._sessions[key] = archived
            self._session_versions[key].append(archived)

    def _message_for(
        self, workspace_id: UUID, environment_id: UUID, session_id: UUID, message_id: UUID
    ) -> QAMessage:
        message = self._messages.get((workspace_id, environment_id, message_id))
        if message is None or message.session_id != session_id:
            raise QAMessageNotFound()
        return message

    def _replay(
        self, digest: CommandDigest, workspace_id: UUID, environment_id: UUID, key: IdempotencyKey
    ) -> _Receipt | None:
        receipt = self._receipts.get((workspace_id, environment_id, digest.operation.value, key.value))
        if receipt is None:
            return None
        if receipt.digest != digest.value:
            raise IdempotencyConflictError("The idempotency key conflicts with existing command.")
        return receipt

    def _record(
        self,
        digest: CommandDigest,
        workspace_id: UUID,
        environment_id: UUID,
        key: IdempotencyKey,
        command_id: UUID,
        entity_id: UUID,
    ) -> None:
        operation = digest.operation.value
        self._receipts[(workspace_id, environment_id, operation, key.value)] = _Receipt(
            operation, workspace_id, environment_id, key.value, digest.value, command_id, entity_id
        )

    @staticmethod
    def _not_found_session():
        raise QASessionNotFound()


__all__ = ["InMemoryQASessionRepository"]
