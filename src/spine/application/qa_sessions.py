"""Application seam for tenant-scoped Q&A session navigation state.

This module stores no retrieval authority.  A session is useful for restoring
the user interaction only; every future answer run must establish fresh
authorization and evidence independently.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Generic, Protocol, TypeVar
from uuid import UUID, uuid4

from spine.application.persistence.command_digest import CommandDigest, digest_command
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import (
    IdempotencyConflictError,
    InvalidPersistenceContextError,
    OptimisticConflictError,
)
from spine.application.persistence.idempotency import IdempotencyKey
from spine.domain.capabilities import AnswerQuestionOutcome
from spine.domain.disclosure import DisclosureDependencySet
from spine.domain.qa_sessions import QAMessage, QAMessageDisclosureEligibility, QAMessageState, QASession


SESSION_SCHEMA_VERSION = 1
MESSAGE_SCHEMA_VERSION = 1
_MAX_PAGE_SIZE = 100


class QASessionNotFound(LookupError):
    """The requested session is not visible in the trusted scope."""


class QAMessageNotFound(LookupError):
    """The requested message is not visible in the trusted scope."""


class QASessionVersionConflict(OptimisticConflictError):
    """A restore or message transition used a stale immutable version."""


class InvalidQASessionRequest(ValueError):
    """The transport could not form a valid session command."""


@dataclass(frozen=True, slots=True)
class QASessionRequester:
    subject_id: UUID
    authorization_generation: int = 1

    def __post_init__(self) -> None:
        if self.subject_id.int == 0 or self.authorization_generation < 1:
            raise ValueError("Q&A requester identity is invalid")


@dataclass(frozen=True, slots=True)
class QASessionPosition:
    created_at: datetime
    session_id: UUID


@dataclass(frozen=True, slots=True)
class QAMessagePosition:
    ordinal: int
    message_id: UUID


SnapshotT = TypeVar("SnapshotT")


@dataclass(frozen=True, slots=True)
class QASessionPage(Generic[SnapshotT]):
    items: tuple[SnapshotT, ...]
    has_more: bool


QAMessagePage = QASessionPage


@dataclass(frozen=True, slots=True)
class QAMutationResult(Generic[SnapshotT]):
    command_id: UUID
    value: SnapshotT
    replay: bool


@dataclass(frozen=True, slots=True)
class QAMessageRead:
    """Safe message read whose content was released by a fresh decision."""

    message_id: UUID
    session_id: UUID
    workspace_id: UUID
    environment_id: UUID
    version: int
    ordinal: int
    trace_id: UUID
    run_id: UUID | None
    state: QAMessageState
    terminal_outcome: AnswerQuestionOutcome | None
    cancelled: bool
    disclosure_eligibility: QAMessageDisclosureEligibility
    question: str | None
    answer: str | None
    created_at: datetime
    updated_at: datetime


class QASessionRepository(Protocol):
    async def create_session(
        self,
        session: QASession,
        *,
        command_id: UUID,
        idempotency_key: IdempotencyKey,
        digest: CommandDigest,
    ) -> QAMutationResult[QASession]: ...

    async def resolve_session(
        self, *, workspace_id: UUID, environment_id: UUID, session_id: UUID
    ) -> QASession | None: ...

    async def list_sessions(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        limit: int,
        after: QASessionPosition | None,
    ) -> QASessionPage[QASession]: ...

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
    ) -> QAMutationResult[QASession]: ...

    async def create_pending_message(
        self,
        message: QAMessage,
        *,
        command_id: UUID,
        idempotency_key: IdempotencyKey,
        digest: CommandDigest,
    ) -> QAMutationResult[QAMessage]: ...

    async def resolve_message(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        session_id: UUID,
        message_id: UUID,
    ) -> QAMessage | None: ...

    async def list_messages(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        session_id: UUID,
        limit: int,
        after: QAMessagePosition | None,
    ) -> QAMessagePage[QAMessage]: ...

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
    ) -> QAMutationResult[QAMessage]: ...

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
        disclosure_dependencies: DisclosureDependencySet | None,
        digest: CommandDigest,
    ) -> QAMutationResult[QAMessage]: ...


class QAMessageDisclosureEligibilityPort(Protocol):
    async def is_eligible(
        self,
        *,
        message: QAMessage,
        requester: QASessionRequester,
    ) -> bool: ...


class DenyAllQAMessageDisclosure:
    """Safe default when no content authorization adapter is composed."""

    async def is_eligible(self, *, message: QAMessage, requester: QASessionRequester) -> bool:
        del message, requester
        return False


class QASessionService(Protocol):
    async def create_session(
        self, *, context: TrustedPersistenceContext, requester: QASessionRequester,
        idempotency_key: IdempotencyKey,
    ) -> QAMutationResult[QASession]: ...

    async def list_sessions(
        self, *, context: TrustedPersistenceContext, limit: int,
        after: QASessionPosition | None,
    ) -> QASessionPage[QASession]: ...

    async def get_session(
        self, *, context: TrustedPersistenceContext, session_id: UUID
    ) -> QASession | None: ...

    async def restore_session(
        self, *, context: TrustedPersistenceContext, session_id: UUID,
        expected_version: int, idempotency_key: IdempotencyKey,
    ) -> QAMutationResult[QASession]: ...

    async def create_pending_message(
        self, *, context: TrustedPersistenceContext, requester: QASessionRequester,
        session_id: UUID, question: str, run_id: UUID | None,
        idempotency_key: IdempotencyKey,
    ) -> QAMutationResult[QAMessage]: ...

    async def list_messages(
        self, *, context: TrustedPersistenceContext, requester: QASessionRequester,
        session_id: UUID, limit: int, after: QAMessagePosition | None,
    ) -> QAMessagePage[QAMessageRead] | None: ...

    async def get_message(
        self, *, context: TrustedPersistenceContext, requester: QASessionRequester,
        session_id: UUID, message_id: UUID,
    ) -> QAMessageRead | None: ...

    async def link_message_to_run(
        self, *, context: TrustedPersistenceContext, session_id: UUID,
        message_id: UUID, run_id: UUID, idempotency_key: IdempotencyKey,
    ) -> QAMutationResult[QAMessage]: ...

    async def complete_message(
        self, *, context: TrustedPersistenceContext, session_id: UUID,
        message_id: UUID, answer: str | None, outcome: AnswerQuestionOutcome,
        cancelled: bool, idempotency_key: IdempotencyKey,
    ) -> QAMutationResult[QAMessage]: ...


@dataclass(frozen=True, slots=True)
class PersistedQASessionService:
    repository: QASessionRepository
    disclosure_eligibility: QAMessageDisclosureEligibilityPort
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    session_id_factory: Callable[[], UUID] = uuid4
    message_id_factory: Callable[[], UUID] = uuid4
    command_id_factory: Callable[[], UUID] = uuid4

    async def create_session(
        self, *, context: TrustedPersistenceContext, requester: QASessionRequester,
        idempotency_key: IdempotencyKey,
    ) -> QAMutationResult[QASession]:
        workspace_id, environment_id = _scope(context)
        now = self.clock()
        session = QASession(
            session_id=self.session_id_factory(),
            workspace_id=workspace_id,
            environment_id=environment_id,
            subject_id=requester.subject_id,
            version=1,
            status="active",
            created_at=now,
            updated_at=now,
        )
        return await self.repository.create_session(
            session,
            command_id=self.command_id_factory(),
            idempotency_key=idempotency_key,
            digest=_digest(context.operation, {"subject_id": requester.subject_id}),
        )

    async def list_sessions(
        self,
        *,
        context: TrustedPersistenceContext,
        limit: int,
        after: QASessionPosition | None,
    ) -> QASessionPage[QASession]:
        workspace_id, environment_id = _scope(context)
        _validate_limit(limit)
        return await self.repository.list_sessions(
            workspace_id=workspace_id, environment_id=environment_id, limit=limit, after=after
        )

    async def get_session(
        self, *, context: TrustedPersistenceContext, session_id: UUID
    ) -> QASession | None:
        workspace_id, environment_id = _scope(context)
        return await self.repository.resolve_session(
            workspace_id=workspace_id, environment_id=environment_id, session_id=session_id
        )

    async def restore_session(
        self,
        *,
        context: TrustedPersistenceContext,
        session_id: UUID,
        expected_version: int,
        idempotency_key: IdempotencyKey,
    ) -> QAMutationResult[QASession]:
        workspace_id, environment_id = _scope(context)
        if expected_version < 1:
            raise ValueError("expected session version must be positive")
        return await self.repository.restore_session(
            workspace_id=workspace_id,
            environment_id=environment_id,
            session_id=session_id,
            expected_version=expected_version,
            restored_at=self.clock(),
            command_id=self.command_id_factory(),
            idempotency_key=idempotency_key,
            digest=_digest(
                context.operation,
                {"session_id": session_id, "expected_version": expected_version},
            ),
        )

    async def create_pending_message(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: QASessionRequester,
        session_id: UUID,
        question: str,
        run_id: UUID | None,
        idempotency_key: IdempotencyKey,
    ) -> QAMutationResult[QAMessage]:
        workspace_id, environment_id = _scope(context)
        now = self.clock()
        message = QAMessage(
            message_id=self.message_id_factory(),
            session_id=session_id,
            workspace_id=workspace_id,
            environment_id=environment_id,
            version=1,
            ordinal=1,
            trace_id=context.trace_id,
            run_id=run_id,
            question=question,
            state=QAMessageState.RUNNING if run_id is not None else QAMessageState.PENDING,
            created_at=now,
            updated_at=now,
        )
        return await self.repository.create_pending_message(
            message,
            command_id=self.command_id_factory(),
            idempotency_key=idempotency_key,
            digest=_digest(
                context.operation,
                {"session_id": session_id, "question": question, "run_id": run_id},
            ),
        )

    async def list_messages(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: QASessionRequester,
        session_id: UUID,
        limit: int,
        after: QAMessagePosition | None,
    ) -> QAMessagePage[QAMessageRead] | None:
        workspace_id, environment_id = _scope(context)
        _validate_limit(limit)
        session = await self.repository.resolve_session(
            workspace_id=workspace_id, environment_id=environment_id, session_id=session_id
        )
        if session is None:
            return None
        page = await self.repository.list_messages(
            workspace_id=workspace_id, environment_id=environment_id,
            session_id=session_id, limit=limit, after=after,
        )
        return QAMessagePage(
            items=tuple(await self._read(message, requester) for message in page.items),
            has_more=page.has_more,
        )

    async def get_message(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: QASessionRequester,
        session_id: UUID,
        message_id: UUID,
    ) -> QAMessageRead | None:
        workspace_id, environment_id = _scope(context)
        message = await self.repository.resolve_message(
            workspace_id=workspace_id, environment_id=environment_id,
            session_id=session_id, message_id=message_id,
        )
        return None if message is None else await self._read(message, requester)

    async def link_message_to_run(
        self,
        *,
        context: TrustedPersistenceContext,
        session_id: UUID,
        message_id: UUID,
        run_id: UUID,
        idempotency_key: IdempotencyKey,
    ) -> QAMutationResult[QAMessage]:
        workspace_id, environment_id = _scope(context)
        return await self.repository.link_message_to_run(
            workspace_id=workspace_id, environment_id=environment_id,
            session_id=session_id, message_id=message_id, run_id=run_id,
            command_id=self.command_id_factory(), idempotency_key=idempotency_key,
            digest=_digest(
                context.operation,
                {"session_id": session_id, "message_id": message_id, "run_id": run_id},
            ),
        )

    async def complete_message(
        self,
        *,
        context: TrustedPersistenceContext,
        session_id: UUID,
        message_id: UUID,
        answer: str | None,
        outcome: AnswerQuestionOutcome,
        cancelled: bool,
        idempotency_key: IdempotencyKey,
        disclosure_dependencies: DisclosureDependencySet | None = None,
    ) -> QAMutationResult[QAMessage]:
        workspace_id, environment_id = _scope(context)
        return await self.repository.complete_message(
            workspace_id=workspace_id, environment_id=environment_id,
            session_id=session_id, message_id=message_id, answer=answer,
            disclosure_dependencies=disclosure_dependencies,
            outcome=outcome, cancelled=cancelled, completed_at=self.clock(),
            command_id=self.command_id_factory(), idempotency_key=idempotency_key,
            digest=_digest(
                context.operation,
                {
                    "session_id": session_id,
                    "message_id": message_id,
                    "answer": answer,
                    "disclosure_dependencies": (
                        disclosure_dependencies.model_dump(mode="json")
                        if disclosure_dependencies is not None
                        else None
                    ),
                    "outcome": outcome,
                    "cancelled": cancelled,
                },
            ),
        )

    async def _read(self, message: QAMessage, requester: QASessionRequester) -> QAMessageRead:
        try:
            allowed = await self.disclosure_eligibility.is_eligible(
                message=message, requester=requester
            )
        except Exception:
            allowed = False
        eligibility = (
            QAMessageDisclosureEligibility.ELIGIBLE
            if allowed
            else QAMessageDisclosureEligibility.INELIGIBLE
        )
        return QAMessageRead(
            message_id=message.message_id,
            session_id=message.session_id,
            workspace_id=message.workspace_id,
            environment_id=message.environment_id,
            version=message.version,
            ordinal=message.ordinal,
            trace_id=message.trace_id,
            run_id=message.run_id,
            state=message.state,
            terminal_outcome=message.terminal_outcome,
            cancelled=message.cancelled,
            disclosure_eligibility=eligibility,
            question=message.question if allowed else None,
            answer=message.answer if allowed else None,
            created_at=message.created_at,
            updated_at=message.updated_at,
        )


def _scope(context: TrustedPersistenceContext) -> tuple[UUID, UUID]:
    try:
        context.validate_shape()
        scope = context.scope
        if not isinstance(scope, EnvironmentScope):
            raise InvalidPersistenceContextError("Q&A sessions require an environment scope")
        return scope.workspace_id, scope.environment_id
    except InvalidPersistenceContextError:
        raise
    except Exception:
        raise InvalidPersistenceContextError("Persistence context is invalid.") from None


def _validate_limit(limit: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _MAX_PAGE_SIZE:
        raise ValueError("Q&A page limit is outside the supported bound")


def _digest(operation: PersistenceOperation, payload: dict[str, object]) -> CommandDigest:
    return digest_command(
        operation=operation,
        operation_schema_version=1,
        payload=payload,
    )


__all__ = [
    "DenyAllQAMessageDisclosure",
    "MESSAGE_SCHEMA_VERSION",
    "PersistedQASessionService",
    "QAMessageDisclosureEligibility",
    "QAMessageDisclosureEligibilityPort",
    "QAMessageNotFound",
    "QAMessagePage",
    "QAMessagePosition",
    "QAMessageRead",
    "QAMessageState",
    "QAMutationResult",
    "QASessionNotFound",
    "InvalidQASessionRequest",
    "QASessionPage",
    "QASessionPosition",
    "QASessionRequester",
    "QASessionRepository",
    "QASessionService",
    "QASessionVersionConflict",
    "SESSION_SCHEMA_VERSION",
]
