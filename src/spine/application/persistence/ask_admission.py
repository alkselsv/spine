"""Typed persistence seam for durable Q&A Ask admission.

Admission is deliberately separate from execution.  It records the durable
identities and a delivery-only dispatch intent in one Unit of Work; a later
owner may consume that intent and start execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from spine.application.persistence.command_digest import CommandDigest, digest_command
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import InvalidPersistenceContextError
from spine.application.persistence.idempotency import IdempotencyKey
from spine.domain.common import ActorRef, EnvironmentKind
from spine.domain.capabilities.answer_question import AnswerQuestionOutcome
from spine.domain.capabilities.answer_question_runs import AnswerQuestionRunState


QA_ASK_ADMISSION_OPERATION = PersistenceOperation("qa.ask-admission-cancellation")
QA_ASK_LEGACY_OPERATION = PersistenceOperation("qa.ask")
QA_ASK_ADMISSION_SCHEMA_VERSION = 1
QA_ASK_ADMISSION_RESULT_TYPE = "qa_ask_admission"
QA_ASK_ADMISSION_EVENT_TYPE = "qa.ask-admitted"
QA_ASK_ADMISSION_EVENT_SCHEMA_VERSION = 1
QA_ASK_CANCELLATION_EVENT_TYPE = "qa.ask-cancellation-requested"
QA_ASK_CANCELLATION_EVENT_SCHEMA_VERSION = 1
QA_ASK_ADMISSION_REQUEST_SCHEMA = "qa.ask-admission-request:v1"


class AskCancellationDispatchPayload(BaseModel):
    """Minimal, versioned intent to signal cancellation to one Temporal run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    schema_version: int = Field(default=1, gt=0)

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("cancellation run_id must be non-zero")
        return value


class AskAdmissionDispatchPayload(BaseModel):
    """Minimal, versioned outbox payload; it contains no question or answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: UUID
    session_id: UUID
    message_id: UUID
    run_id: UUID
    schema_version: int = Field(default=1, gt=0)


@dataclass(frozen=True, slots=True)
class AskAdmissionCommand:
    """The trusted input needed to admit one Q&A command."""

    workspace_id: UUID
    environment_id: UUID
    session_id: UUID
    subject: ActorRef
    environment: EnvironmentKind
    question: str
    idempotency_key: IdempotencyKey
    digest: CommandDigest
    clarification_of_message_id: UUID | None = None

    def __post_init__(self) -> None:
        identifiers = (
            self.workspace_id,
            self.environment_id,
            self.session_id,
            self.subject.id,
            self.clarification_of_message_id,
        )
        if any(value is not None and value.int == 0 for value in identifiers):
            raise ValueError("Ask admission identity must be non-zero")
        if not self.question or not self.question.strip() or len(self.question) > 32768:
            raise ValueError("Ask question must be a bounded non-blank string")
        if self.digest.operation != QA_ASK_ADMISSION_OPERATION:
            raise ValueError("Ask admission digest operation is invalid")
        if self.digest.operation_schema_version != QA_ASK_ADMISSION_SCHEMA_VERSION:
            raise ValueError("Ask admission digest schema version is invalid")

    @classmethod
    def create(
        cls,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        session_id: UUID,
        subject: ActorRef,
        environment: EnvironmentKind,
        question: str,
        idempotency_key: IdempotencyKey,
        clarification_of_message_id: UUID | None = None,
    ) -> "AskAdmissionCommand":
        digest = digest_command(
            operation=QA_ASK_ADMISSION_OPERATION,
            operation_schema_version=QA_ASK_ADMISSION_SCHEMA_VERSION,
            payload={
                "schema": QA_ASK_ADMISSION_REQUEST_SCHEMA,
                "workspace_id": workspace_id,
                "environment_id": environment_id,
                "session_id": session_id,
                "subject": subject.model_dump(mode="python"),
                "environment": environment,
                "question": question,
                "clarification_of_message_id": clarification_of_message_id,
                "idempotency_key": idempotency_key.value,
            },
        )
        return cls(
            workspace_id=workspace_id,
            environment_id=environment_id,
            session_id=session_id,
            subject=subject,
            environment=environment,
            question=question,
            idempotency_key=idempotency_key,
            digest=digest,
            clarification_of_message_id=clarification_of_message_id,
        )


@dataclass(frozen=True, slots=True)
class AskAdmissionResult:
    """Stable server-owned identities returned by admission or replay."""

    command_id: UUID
    session_id: UUID
    message_id: UUID
    run_id: UUID
    dispatch_event_id: UUID
    replay: bool

    def __post_init__(self) -> None:
        if any(
            value.int == 0
            for value in (
                self.command_id,
                self.session_id,
                self.message_id,
                self.run_id,
                self.dispatch_event_id,
            )
        ):
            raise ValueError("Ask admission result identities must be non-zero")


class AskAdmissionLifecycle(str, Enum):
    ADMITTED = "admitted"
    RUNNING = "running"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class AskAdmissionStatus:
    """Safe lifecycle projection; it never contains question or answer text."""

    command_id: UUID
    session_id: UUID
    message_id: UUID
    run_id: UUID
    status: AskAdmissionLifecycle
    outcome: AnswerQuestionOutcome | None = None
    attempt_id: UUID | None = None

    def __post_init__(self) -> None:
        if any(
            identifier.int == 0
            for identifier in (
                self.command_id,
                self.session_id,
                self.message_id,
                self.run_id,
            )
        ):
            raise ValueError("Ask admission status identities must be non-zero")
        if self.status is AskAdmissionLifecycle.TERMINAL and self.outcome is None:
            raise ValueError("terminal Ask admission status requires an outcome")
        if self.status is not AskAdmissionLifecycle.TERMINAL and self.outcome is not None:
            raise ValueError("non-terminal Ask admission status cannot carry an outcome")
        if self.attempt_id is not None and self.attempt_id.int == 0:
            raise ValueError("Ask admission attempt identity must be non-zero")


class AskCancellationLifecycle(str, Enum):
    CANCELLATION_REQUESTED = "cancellation_requested"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class AskCancellationStatus:
    command_id: UUID
    run_id: UUID
    status: AskCancellationLifecycle
    outcome: AnswerQuestionOutcome | None
    replay: bool
    session_id: UUID | None = None
    message_id: UUID | None = None
    attempt_id: UUID | None = None


class AskAdmissionRepository(Protocol):
    async def admit(self, command: AskAdmissionCommand) -> AskAdmissionResult: ...


class AskAdmissionApplication(Protocol):
    async def admit(
        self,
        *,
        context: TrustedPersistenceContext,
        command: AskAdmissionCommand,
    ) -> AskAdmissionResult: ...

    async def status(
        self,
        *,
        context: TrustedPersistenceContext,
        result: AskAdmissionResult,
    ) -> AskAdmissionStatus: ...

    async def request_cancellation(
        self,
        *,
        context: TrustedPersistenceContext,
        run_id: UUID,
    ) -> AskCancellationStatus: ...

    async def wait_for_cancellation(
        self,
        *,
        context: TrustedPersistenceContext,
        run_id: UUID,
        poll_interval: float = 0.05,
        timeout: float = 5.0,
    ) -> AskCancellationStatus: ...


def validate_ask_admission_context(
    context: TrustedPersistenceContext,
    command: AskAdmissionCommand,
) -> None:
    """Bind every command field to the current trusted authorization scope."""

    actor_id = context.acting_subject_id or context.service_principal_id
    if (
        not isinstance(context.scope, EnvironmentScope)
        or context.operation not in {QA_ASK_ADMISSION_OPERATION, QA_ASK_LEGACY_OPERATION}
        or actor_id != command.subject.id
        or context.scope.workspace_id != command.workspace_id
        or context.scope.environment_id != command.environment_id
    ):
        raise InvalidPersistenceContextError("Ask admission context is invalid")


def register_ask_admission_outbox_event(registry: object) -> None:
    """Register both delivery-only events with an adapter-owned registry."""

    register = getattr(registry, "register")
    register(
        event_type=QA_ASK_ADMISSION_EVENT_TYPE,
        schema_version=QA_ASK_ADMISSION_EVENT_SCHEMA_VERSION,
        payload_type=AskAdmissionDispatchPayload,
    )
    register(
        event_type=QA_ASK_CANCELLATION_EVENT_TYPE,
        schema_version=QA_ASK_CANCELLATION_EVENT_SCHEMA_VERSION,
        payload_type=AskCancellationDispatchPayload,
    )


__all__ = [
    "AskAdmissionApplication",
    "AskAdmissionCommand",
    "AskAdmissionDispatchPayload",
    "AskAdmissionRepository",
    "AskAdmissionResult",
    "AskCancellationDispatchPayload",
    "AskAdmissionLifecycle",
    "AskAdmissionStatus",
    "AskCancellationLifecycle",
    "AskCancellationStatus",
    "QA_ASK_ADMISSION_EVENT_SCHEMA_VERSION",
    "QA_ASK_ADMISSION_EVENT_TYPE",
    "QA_ASK_CANCELLATION_EVENT_SCHEMA_VERSION",
    "QA_ASK_CANCELLATION_EVENT_TYPE",
    "QA_ASK_ADMISSION_OPERATION",
    "QA_ASK_LEGACY_OPERATION",
    "QA_ASK_ADMISSION_REQUEST_SCHEMA",
    "QA_ASK_ADMISSION_RESULT_TYPE",
    "QA_ASK_ADMISSION_SCHEMA_VERSION",
    "register_ask_admission_outbox_event",
    "validate_ask_admission_context",
]
