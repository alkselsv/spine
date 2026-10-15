"""PostgreSQL adapter for immutable Q&A session and message versions."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select

from spine.application.persistence.command_digest import CommandDigest
from spine.application.persistence.idempotency import (
    IdempotencyKey,
    IdempotencyReplay,
    OpaqueResultReference,
    OwnedIdempotencyClaim,
)
from spine.application.qa_sessions import (
    QAMessageNotFound,
    QAMessagePage,
    QAMessagePosition,
    QAMutationResult,
    QASessionNotFound,
    QASessionPage,
    QASessionPosition,
    QASessionVersionConflict,
)
from spine.domain.qa_sessions import QAMessage, QAMessageState, QASession, QASessionStatus
from spine.infrastructure.persistence.postgresql_mappings import qa_message_versions, qa_session_versions


class PostgreSQLQASessionRepository:
    def __init__(self, uow: Any) -> None:
        self._uow = uow
        self._owned_claims: dict[str, OwnedIdempotencyClaim] = {}

    async def create_session(
        self,
        session: QASession,
        *,
        command_id: UUID,
        idempotency_key: IdempotencyKey,
        digest: CommandDigest,
    ) -> QAMutationResult[QASession]:
        replay = await self._claim(idempotency_key, digest)
        if replay is not None:
            result_id = replay.result.result_id
            value = await self.resolve_session(workspace_id=session.workspace_id, environment_id=session.environment_id, session_id=result_id)
            if value is None:
                raise QASessionNotFound()
            return QAMutationResult(replay.receipt_id, value, True)
        await self._uow._execute(insert(qa_session_versions).values(self._session_values(session)))
        stable_command_id = self._owned_claims[digest.value].receipt_id
        await self._complete(digest, session.session_id, "qa_session")
        return QAMutationResult(stable_command_id, session, False)

    async def resolve_session(
        self, *, workspace_id: UUID, environment_id: UUID, session_id: UUID
    ) -> QASession | None:
        result = await self._uow._execute(
            select(qa_session_versions)
            .where(qa_session_versions.c.workspace_id == workspace_id, qa_session_versions.c.environment_id == environment_id, qa_session_versions.c.session_id == session_id)
            .order_by(qa_session_versions.c.version.desc()).limit(1)
        )
        row = result.mappings().first()
        return None if row is None else self._session(row)

    async def list_sessions(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        limit: int,
        after: QASessionPosition | None,
    ) -> QASessionPage[QASession]:
        latest = (
            select(qa_session_versions)
            .where(
                qa_session_versions.c.workspace_id == workspace_id,
                qa_session_versions.c.environment_id == environment_id,
            )
            .distinct(qa_session_versions.c.session_id)
            .order_by(qa_session_versions.c.session_id, qa_session_versions.c.version.desc())
            .subquery()
        )
        query = select(latest).order_by(latest.c.created_at, latest.c.session_id)
        if after is not None:
            query = query.where(
                (latest.c.created_at > after.created_at)
                | ((latest.c.created_at == after.created_at) & (latest.c.session_id > after.session_id))
            )
        result = await self._uow._execute(query.limit(limit + 1))
        values = tuple(self._session(row) for row in result.mappings())
        return QASessionPage(values[:limit], len(values) > limit)

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
        replay = await self._claim(idempotency_key, digest)
        if replay is not None:
            result_id = replay.result.result_id
            value = await self.resolve_session(workspace_id=workspace_id, environment_id=environment_id, session_id=result_id)
            if value is None:
                raise QASessionNotFound()
            return QAMutationResult(replay.receipt_id, value, True)
        current = await self.resolve_session(workspace_id=workspace_id, environment_id=environment_id, session_id=session_id)
        if current is None:
            raise QASessionNotFound()
        if current.version != expected_version:
            raise QASessionVersionConflict()
        updated = current
        if current.status is QASessionStatus.ARCHIVED:
            updated = current.model_copy(update={"version": current.version + 1, "status": QASessionStatus.ACTIVE, "updated_at": restored_at})
            await self._uow._execute(insert(qa_session_versions).values(self._session_values(updated)))
        stable_command_id = self._owned_claims[digest.value].receipt_id
        await self._complete(digest, session_id, "qa_session")
        return QAMutationResult(stable_command_id, updated, False)

    async def create_pending_message(
        self,
        message: QAMessage,
        *,
        command_id: UUID,
        idempotency_key: IdempotencyKey,
        digest: CommandDigest,
    ) -> QAMutationResult[QAMessage]:
        replay = await self._claim(idempotency_key, digest)
        if replay is not None:
            result_id = replay.result.result_id
            value = await self.resolve_message(workspace_id=message.workspace_id, environment_id=message.environment_id, session_id=message.session_id, message_id=result_id)
            if value is None:
                raise QAMessageNotFound()
            return QAMutationResult(replay.receipt_id, value, True)
        if await self.resolve_session(workspace_id=message.workspace_id, environment_id=message.environment_id, session_id=message.session_id) is None:
            raise QASessionNotFound()
        session_lock = await self._uow._execute(
            select(qa_session_versions.c.session_id)
            .where(
                qa_session_versions.c.workspace_id == message.workspace_id,
                qa_session_versions.c.environment_id == message.environment_id,
                qa_session_versions.c.session_id == message.session_id,
            )
            .order_by(qa_session_versions.c.version.desc())
            .limit(1)
            .with_for_update()
        )
        if session_lock.scalar_one_or_none() is None:
            raise QASessionNotFound()
        result = await self._uow._execute(
            select(qa_message_versions.c.ordinal)
            .where(qa_message_versions.c.workspace_id == message.workspace_id, qa_message_versions.c.environment_id == message.environment_id, qa_message_versions.c.session_id == message.session_id)
            .order_by(qa_message_versions.c.ordinal.desc()).limit(1)
        )
        ordinal = (result.scalar_one_or_none() or 0) + 1
        if message.ordinal != ordinal:
            message = message.model_copy(update={"ordinal": ordinal})
        await self._uow._execute(insert(qa_message_versions).values(self._message_values(message)))
        stable_command_id = self._owned_claims[digest.value].receipt_id
        await self._complete(digest, message.message_id, "qa_message")
        return QAMutationResult(stable_command_id, message, False)

    async def resolve_message(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        session_id: UUID,
        message_id: UUID,
    ) -> QAMessage | None:
        result = await self._uow._execute(
            select(qa_message_versions)
            .where(qa_message_versions.c.workspace_id == workspace_id, qa_message_versions.c.environment_id == environment_id, qa_message_versions.c.session_id == session_id, qa_message_versions.c.message_id == message_id)
            .order_by(qa_message_versions.c.version.desc()).limit(1)
        )
        row = result.mappings().first()
        return None if row is None else self._message(row)

    async def list_messages(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        session_id: UUID,
        limit: int,
        after: QAMessagePosition | None,
    ) -> QAMessagePage[QAMessage]:
        latest = (
            select(qa_message_versions)
            .where(
                qa_message_versions.c.workspace_id == workspace_id,
                qa_message_versions.c.environment_id == environment_id,
                qa_message_versions.c.session_id == session_id,
            )
            .distinct(qa_message_versions.c.message_id)
            .order_by(qa_message_versions.c.message_id, qa_message_versions.c.version.desc())
            .subquery()
        )
        query = select(latest).order_by(latest.c.ordinal, latest.c.message_id)
        if after is not None:
            query = query.where(
                (latest.c.ordinal > after.ordinal)
                | ((latest.c.ordinal == after.ordinal) & (latest.c.message_id > after.message_id))
            )
        result = await self._uow._execute(query.limit(limit + 1))
        values = tuple(self._message(row) for row in result.mappings())
        return QAMessagePage(values[:limit], len(values) > limit)

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
        replay = await self._claim(idempotency_key, digest)
        if replay is not None:
            result_id = replay.result.result_id
            value = await self.resolve_message(workspace_id=workspace_id, environment_id=environment_id, session_id=session_id, message_id=result_id)
            if value is None:
                raise QAMessageNotFound()
            return QAMutationResult(replay.receipt_id, value, True)
        current = await self._lock_message(workspace_id, environment_id, session_id, message_id)
        if current.state is QAMessageState.TERMINAL:
            raise QASessionVersionConflict()
        if current.run_id is not None and current.run_id != run_id:
            raise QASessionVersionConflict()
        updated = current.model_copy(update={"version": current.version + 1, "run_id": run_id, "state": QAMessageState.RUNNING})
        await self._uow._execute(insert(qa_message_versions).values(self._message_values(updated)))
        stable_command_id = self._owned_claims[digest.value].receipt_id
        await self._complete(digest, message_id, "qa_message")
        return QAMutationResult(stable_command_id, updated, False)

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
        replay = await self._claim(idempotency_key, digest)
        if replay is not None:
            result_id = replay.result.result_id
            value = await self.resolve_message(workspace_id=workspace_id, environment_id=environment_id, session_id=session_id, message_id=result_id)
            if value is None:
                raise QAMessageNotFound()
            return QAMutationResult(replay.receipt_id, value, True)
        current = await self._lock_message(workspace_id, environment_id, session_id, message_id)
        if current.state is QAMessageState.TERMINAL:
            raise QASessionVersionConflict()
        updated = current.model_copy(update={"version": current.version + 1, "state": QAMessageState.TERMINAL, "answer": answer, "terminal_outcome": outcome, "cancelled": cancelled, "updated_at": completed_at})
        await self._uow._execute(insert(qa_message_versions).values(self._message_values(updated)))
        stable_command_id = self._owned_claims[digest.value].receipt_id
        await self._complete(digest, message_id, "qa_message")
        return QAMutationResult(stable_command_id, updated, False)

    async def _require_message(
        self, workspace_id: UUID, environment_id: UUID, session_id: UUID, message_id: UUID
    ) -> QAMessage:
        value = await self.resolve_message(workspace_id=workspace_id, environment_id=environment_id, session_id=session_id, message_id=message_id)
        if value is None:
            raise QAMessageNotFound()
        return value

    async def _lock_message(
        self, workspace_id: UUID, environment_id: UUID, session_id: UUID, message_id: UUID
    ) -> QAMessage:
        result = await self._uow._execute(
            select(qa_message_versions)
            .where(
                qa_message_versions.c.workspace_id == workspace_id,
                qa_message_versions.c.environment_id == environment_id,
                qa_message_versions.c.session_id == session_id,
                qa_message_versions.c.message_id == message_id,
            )
            .order_by(qa_message_versions.c.version.desc())
            .limit(1)
            .with_for_update()
        )
        row = result.mappings().first()
        if row is None:
            raise QAMessageNotFound()
        return self._message(row)

    async def _claim(
        self, key: IdempotencyKey, digest: CommandDigest
    ) -> IdempotencyReplay | None:
        claim = await self._uow.idempotency.claim(operation_schema_version=1, key=key, digest=digest)
        if isinstance(claim, IdempotencyReplay):
            return claim
        self._owned_claims[digest.value] = claim
        return None

    async def _complete(self, digest: CommandDigest, result_id: UUID, result_type: str) -> None:
        claim = self._owned_claims.pop(digest.value)
        await self._uow.idempotency.complete(
            claim, OpaqueResultReference(result_type, result_id, 1)
        )

    @staticmethod
    def _session_values(value: QASession) -> dict[str, Any]:
        return {
            "session_id": value.session_id,
            "workspace_id": value.workspace_id,
            "environment_id": value.environment_id,
            "subject_id": value.subject_id,
            "version": value.version,
            "status": value.status.value,
            "created_at": value.created_at,
            "updated_at": value.updated_at,
        }

    @staticmethod
    def _message_values(value: QAMessage) -> dict[str, Any]:
        return {
            "message_id": value.message_id,
            "session_id": value.session_id,
            "workspace_id": value.workspace_id,
            "environment_id": value.environment_id,
            "version": value.version,
            "ordinal": value.ordinal,
            "trace_id": value.trace_id,
            "run_id": value.run_id,
            "question": value.question,
            "answer": value.answer,
            "state": value.state.value,
            "terminal_outcome": value.terminal_outcome.value if value.terminal_outcome else None,
            "cancelled": value.cancelled,
            "disclosure_dependency_ids": [str(item) for item in value.disclosure_dependency_ids],
            "disclosure_eligibility": value.disclosure_eligibility.value,
            "created_at": value.created_at,
            "updated_at": value.updated_at,
        }

    @staticmethod
    def _session(row: Any) -> QASession:
        return QASession.model_validate(dict(row))

    @staticmethod
    def _message(row: Any) -> QAMessage:
        return QAMessage.model_validate(dict(row))


__all__ = ["PostgreSQLQASessionRepository"]
