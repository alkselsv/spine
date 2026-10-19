"""Application contract for the authorized Q&A command boundary.

The HTTP adapter deliberately stops at this seam.  Implementations own the
answer_question capability, session/message persistence and cancellation race;
the adapter never performs retrieval or answer validation itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol
from uuid import UUID

from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.ask_admission import (
    AskAdmissionApplication,
    AskAdmissionCommand,
    AskAdmissionResult,
)
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.answer_question_runs import (
    AnswerQuestionAttemptAllocationResult,
    AnswerQuestionAttemptTerminalResult,
    CanonicalQATerminalizationReceipt,
)
from spine.application.qa_sessions import (
    QAMessage,
    QAMessageState,
    QAMutationResult,
    QASessionRequester,
    QASessionService,
)
from spine.domain.capabilities import (
    AcceptedGroundedAnswer,
    AnswerQuestionOutcome,
    AnswerQuestionOutput,
)
from spine.domain.capabilities.answer_question_runs import AnswerQuestionAttemptState
from spine.domain.common import ActorKind, ActorRef, EnvironmentKind


class AuthorizedAskServiceUnavailable(RuntimeError):
    """The persisted answer command cannot currently be executed."""


class InvalidAuthorizedAskRequest(ValueError):
    """The authorized ask command could not be formed from trusted input."""


class AnswerQuestionCapabilityAvailability(str, Enum):
    """Whether infrastructure completed the capability request."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class AskRequestContext:
    """Small application-owned request context released by the API adapter."""

    subject_id: UUID
    workspace_id: UUID
    environment_id: UUID
    trace_id: UUID
    authorization_generation: int
    correlation_id: UUID | None = None
    causation_id: UUID | None = None

    def __post_init__(self) -> None:
        identifiers = (
            self.subject_id,
            self.workspace_id,
            self.environment_id,
            self.trace_id,
            self.correlation_id,
            self.causation_id,
        )
        if (
            any(
                identifier is not None and identifier.int == 0
                for identifier in identifiers
            )
            or self.authorization_generation < 1
        ):
            raise ValueError("authorized ask request context is invalid")


@dataclass(frozen=True, slots=True)
class AskCommand:
    """Trusted, bounded input released by the administrator API route."""

    request_context: AskRequestContext
    persistence_context: TrustedPersistenceContext
    session_id: UUID
    question: str
    idempotency_key: IdempotencyKey
    clarification_of_message_id: UUID | None = None

    @property
    def workspace_id(self) -> UUID:
        return self.request_context.workspace_id

    @property
    def environment_id(self) -> UUID:
        return self.request_context.environment_id

    @property
    def subject_id(self) -> UUID:
        return self.request_context.subject_id

    @property
    def trace_id(self) -> UUID:
        return self.request_context.trace_id


@dataclass(frozen=True, slots=True)
class AskCancellationCommand:
    """Trusted cancellation command for one already admitted Q&A run."""

    request_context: AskRequestContext
    persistence_context: TrustedPersistenceContext
    run_id: UUID
    idempotency_key: IdempotencyKey

    @property
    def workspace_id(self) -> UUID:
        return self.request_context.workspace_id

    @property
    def environment_id(self) -> UUID:
        return self.request_context.environment_id

    @property
    def subject_id(self) -> UUID:
        return self.request_context.subject_id

    @property
    def trace_id(self) -> UUID:
        return self.request_context.trace_id


@dataclass(frozen=True, slots=True)
class AskExecutionCommand:
    """The exact admitted identity released to the governed capability."""

    request_context: AskRequestContext
    persistence_context: TrustedPersistenceContext
    command_id: UUID
    session_id: UUID
    message_id: UUID
    run_id: UUID
    question: str
    idempotency_key: IdempotencyKey
    logical_execution_key: str | None = None

    def __post_init__(self) -> None:
        if any(
            value.int == 0
            for value in (
                self.command_id,
                self.session_id,
                self.message_id,
                self.run_id,
            )
        ):
            raise ValueError("admitted Ask identities must be non-zero")
        if not self.question.strip():
            raise ValueError("admitted Ask question must not be blank")
        if self.logical_execution_key is not None and not (
            1 <= len(self.logical_execution_key) <= 255
            and all(
                character.isalnum() or character in "._:-"
                for character in self.logical_execution_key
            )
        ):
            raise ValueError("logical execution key is invalid")


@dataclass(frozen=True, slots=True)
class AnswerQuestionCapabilityCommand:
    """Typed handoff to the already composed answer_question capability."""

    request_context: AskRequestContext
    persistence_context: TrustedPersistenceContext
    session_id: UUID | None
    message_id: UUID | None
    question: str | None
    run_id: UUID | None
    idempotency_key: IdempotencyKey
    clarification_of_message_id: UUID | None = None
    command_id: UUID | None = None
    attempt_id: UUID | None = None
    attempt_number: int | None = None

    def __post_init__(self) -> None:
        if self.run_id is None:
            if self.session_id is None or self.message_id is None or not self.question:
                raise ValueError("answer capability command requires message input")
        elif self.run_id.int == 0:
            raise ValueError("answer capability run identity must be non-zero")
        if self.command_id is not None and self.command_id.int == 0:
            raise ValueError("answer capability command identity must be non-zero")
        if (self.attempt_id is None) != (self.attempt_number is None):
            raise ValueError("answer capability attempt identity is incomplete")
        if self.attempt_id is not None and (
            self.attempt_id.int == 0
            or self.attempt_number is None
            or self.attempt_number < 1
        ):
            raise ValueError("answer capability attempt identity is invalid")


@dataclass(frozen=True, slots=True)
class AnswerQuestionCapabilityResult:
    """Typed terminal output already passed through the disclosure gate."""

    command_id: UUID
    session_id: UUID
    message_id: UUID
    output: AnswerQuestionOutput
    accepted_answer: AcceptedGroundedAnswer | None
    terminalization_receipt: CanonicalQATerminalizationReceipt | None = None
    replay: bool = False
    availability: AnswerQuestionCapabilityAvailability = (
        AnswerQuestionCapabilityAvailability.AVAILABLE
    )

    def __post_init__(self) -> None:
        if self.command_id.int == 0:
            raise ValueError("answer capability command identity must be non-zero")
        if self.session_id.int == 0 or self.message_id.int == 0:
            raise ValueError("answer capability result identities must be non-zero")
        if type(self.output) is not AnswerQuestionOutput:
            raise TypeError("answer capability requires AnswerQuestionOutput")
        if not isinstance(self.availability, AnswerQuestionCapabilityAvailability):
            raise TypeError("answer capability availability is invalid")
        if self.output.outcome is AnswerQuestionOutcome.ANSWERED:
            if (
                self.accepted_answer is None
                or self.accepted_answer.answer != self.output.answer
            ):
                raise ValueError("answered capability result requires accepted content")
        elif self.accepted_answer is not None:
            raise ValueError("safe capability result cannot carry accepted content")
        if self.terminalization_receipt is not None and (
            self.terminalization_receipt.run_id != self.output.correlation.run_id
            or self.terminalization_receipt.attempt_id
            != self.output.correlation.attempt_id
            or self.terminalization_receipt.output != self.output
        ):
            raise ValueError("terminalization receipt does not match capability output")


class AnswerQuestionCapability(Protocol):
    """Application port for the completed answer_question capability."""

    async def answer(
        self, command: AnswerQuestionCapabilityCommand
    ) -> AnswerQuestionCapabilityResult: ...

    async def cancel(
        self, command: AnswerQuestionCapabilityCommand
    ) -> AnswerQuestionCapabilityResult: ...


class AskTerminalPersistence(Protocol):
    """Canonical terminal CAS seam shared by execution and cancellation owners."""

    async def persist_terminal(
        self,
        command: AskExecutionCommand,
        capability: AnswerQuestionCapabilityResult,
    ) -> AuthorizedAskResult: ...


class AnswerQuestionAttemptApplication(Protocol):
    async def allocate_or_resume(
        self,
        *,
        context: TrustedPersistenceContext,
        run_id: UUID,
        logical_execution_key: str,
    ) -> AnswerQuestionAttemptAllocationResult: ...

    async def resolve_terminal_result(
        self,
        *,
        context: TrustedPersistenceContext,
        run_id: UUID,
        attempt_id: UUID,
    ) -> AnswerQuestionAttemptTerminalResult: ...

    async def terminalize(
        self,
        *,
        context: TrustedPersistenceContext,
        session_id: UUID,
        message_id: UUID,
        idempotency_key: str,
        sessions: QASessionService,
        receipt: CanonicalQATerminalizationReceipt | None,
    ) -> AnswerQuestionAttemptTerminalResult: ...


@dataclass(frozen=True, slots=True)
class AuthorizedAskResult:
    """Terminal result after the answer disclosure gate.

    ``accepted_answer`` is mandatory exactly when the typed output is
    ``answered``.  Safe outputs contain no candidate or source content.
    """

    command_id: UUID
    session_id: UUID
    message_id: UUID
    output: AnswerQuestionOutput
    accepted_answer: AcceptedGroundedAnswer | None
    replay: bool = False
    availability: AnswerQuestionCapabilityAvailability = (
        AnswerQuestionCapabilityAvailability.AVAILABLE
    )

    def __post_init__(self) -> None:
        if any(
            identifier.int == 0
            for identifier in (self.command_id, self.session_id, self.message_id)
        ):
            raise ValueError("authorized ask result identities must be non-zero")
        if type(self.output) is not AnswerQuestionOutput:
            raise TypeError("authorized ask result requires AnswerQuestionOutput")
        if self.output.outcome is AnswerQuestionOutcome.ANSWERED:
            if (
                self.accepted_answer is None
                or self.accepted_answer.answer != self.output.answer
            ):
                raise ValueError(
                    "answered result requires the accepted grounded answer"
                )
        elif self.accepted_answer is not None:
            raise ValueError("safe ask result cannot carry accepted answer content")

    @property
    def run_id(self) -> UUID:
        return self.output.correlation.run_id

    @property
    def attempt_id(self) -> UUID:
        return self.output.correlation.attempt_id


class AuthorizedAskService(Protocol):
    """Existing application capability boundary consumed by the HTTP adapter."""

    async def ask(self, command: AskCommand) -> AuthorizedAskResult: ...

    async def execute_admitted(
        self, command: AskExecutionCommand
    ) -> AuthorizedAskResult: ...

    async def cancel(self, command: AskCancellationCommand) -> AuthorizedAskResult: ...


@dataclass(frozen=True, slots=True)
class PersistedAuthorizedAskService:
    """Coordinate capability execution with immutable session/message state."""

    capability: AnswerQuestionCapability
    sessions: QASessionService
    admission: AskAdmissionApplication | None = None
    attempts: AnswerQuestionAttemptApplication | None = None
    environment: EnvironmentKind = EnvironmentKind.PRODUCTION

    async def ask(self, command: AskCommand) -> AuthorizedAskResult:
        if self.admission is not None:
            admitted = await self.admission.admit(
                context=command.persistence_context,
                command=AskAdmissionCommand.create(
                    workspace_id=command.workspace_id,
                    environment_id=command.environment_id,
                    session_id=command.session_id,
                    subject=ActorRef(kind=ActorKind.HUMAN, id=command.subject_id),
                    environment=self.environment,
                    question=command.question,
                    idempotency_key=command.idempotency_key,
                    clarification_of_message_id=command.clarification_of_message_id,
                ),
            )
            return await self.execute_admitted(
                AskExecutionCommand(
                    request_context=command.request_context,
                    persistence_context=command.persistence_context,
                    command_id=admitted.command_id,
                    session_id=admitted.session_id,
                    message_id=admitted.message_id,
                    run_id=admitted.run_id,
                    question=command.question,
                    idempotency_key=command.idempotency_key,
                )
            )
        requester = QASessionRequester(
            subject_id=command.request_context.subject_id,
            authorization_generation=command.request_context.authorization_generation,
        )
        pending = await self.sessions.create_pending_message(
            context=command.persistence_context,
            requester=requester,
            session_id=command.session_id,
            question=command.question,
            run_id=None,
            idempotency_key=_derived_key("message", command.idempotency_key),
        )
        capability = await self.capability.answer(
            AnswerQuestionCapabilityCommand(
                request_context=command.request_context,
                persistence_context=command.persistence_context,
                session_id=command.session_id,
                message_id=pending.value.message_id,
                question=command.question,
                run_id=None,
                idempotency_key=command.idempotency_key,
                clarification_of_message_id=command.clarification_of_message_id,
            )
        )
        return await self._finish(
            command=command,
            message=pending,
            capability=capability,
            operation_prefix="complete",
        )

    async def execute_admitted(
        self, command: AskExecutionCommand
    ) -> AuthorizedAskResult:
        """Execute one persisted admission without creating another message."""

        allocated = None
        if command.logical_execution_key is not None:
            if self.attempts is None:
                raise AuthorizedAskServiceUnavailable()
            allocated = await self.attempts.allocate_or_resume(
                context=command.persistence_context,
                run_id=command.run_id,
                logical_execution_key=command.logical_execution_key,
            )
            if allocated.attempt.state is not AnswerQuestionAttemptState.RUNNING:
                terminal = await self.attempts.resolve_terminal_result(
                    context=command.persistence_context,
                    run_id=command.run_id,
                    attempt_id=allocated.attempt.attempt_id,
                )
                replay = AnswerQuestionCapabilityResult(
                    command_id=command.command_id,
                    session_id=command.session_id,
                    message_id=command.message_id,
                    output=terminal.output,
                    accepted_answer=terminal.accepted_answer,
                    replay=True,
                )
                _validate_capability_scope(command.request_context, replay)
                if replay.output.correlation.attempt_id != allocated.attempt.attempt_id:
                    raise InvalidAuthorizedAskRequest(
                        "persisted terminal result has mismatched attempt identity"
                    )
                return await self.persist_terminal(command, replay)
        capability = await self.capability.answer(
            AnswerQuestionCapabilityCommand(
                request_context=command.request_context,
                persistence_context=command.persistence_context,
                session_id=command.session_id,
                message_id=command.message_id,
                question=command.question,
                run_id=command.run_id,
                idempotency_key=command.idempotency_key,
                command_id=command.command_id,
                attempt_id=(
                    allocated.attempt.attempt_id if allocated is not None else None
                ),
                attempt_number=(
                    allocated.attempt.attempt_number if allocated is not None else None
                ),
            )
        )
        _validate_capability_scope(command.request_context, capability)
        if (
            command.logical_execution_key is not None
            and capability.availability is AnswerQuestionCapabilityAvailability.UNAVAILABLE
        ):
            # Infrastructure-unavailable outputs are retryable, not canonical
            # FAILED outcomes. No terminal transaction is attempted.
            raise AuthorizedAskServiceUnavailable()
        if (
            capability.command_id != command.command_id
            or capability.session_id != command.session_id
            or capability.message_id != command.message_id
            or capability.output.correlation.run_id != command.run_id
            or (
                allocated is not None
                and capability.output.correlation.attempt_id
                != allocated.attempt.attempt_id
            )
        ):
            raise InvalidAuthorizedAskRequest(
                "answer capability returned mismatched admitted identity"
            )
        return await self.persist_terminal(command, capability)

    async def persist_terminal(
        self,
        command: AskExecutionCommand,
        capability: AnswerQuestionCapabilityResult,
    ) -> AuthorizedAskResult:
        """Persist one terminal winner without overwriting a terminal message."""

        _validate_capability_scope(command.request_context, capability)
        if (
            capability.command_id != command.command_id
            or capability.session_id != command.session_id
            or capability.message_id != command.message_id
            or capability.output.correlation.run_id != command.run_id
        ):
            raise InvalidAuthorizedAskRequest(
                "terminal result returned mismatched admitted identity"
            )
        message = await self.sessions.get_message(
            context=command.persistence_context,
            requester=QASessionRequester(
                subject_id=command.request_context.subject_id,
                authorization_generation=command.request_context.authorization_generation,
            ),
            session_id=command.session_id,
            message_id=command.message_id,
        )
        if message is None:
            raise AuthorizedAskServiceUnavailable()
        if command.logical_execution_key is not None:
            if self.attempts is None:
                raise AuthorizedAskServiceUnavailable()
            terminal = await self.attempts.terminalize(
                context=command.persistence_context,
                session_id=command.session_id,
                message_id=command.message_id,
                idempotency_key=command.idempotency_key.value,
                sessions=self.sessions,
                receipt=capability.terminalization_receipt,
            )
            if terminal.output != capability.output:
                raise InvalidAuthorizedAskRequest(
                    "canonical terminal output conflicts with the capability result"
                )
            if terminal.accepted_answer is not None and terminal.disclosure_record is not None:
                committed_message = await self.sessions.get_message(
                    context=command.persistence_context,
                    requester=QASessionRequester(
                        subject_id=command.request_context.subject_id,
                        authorization_generation=command.request_context.authorization_generation,
                    ),
                    session_id=command.session_id,
                    message_id=command.message_id,
                )
                presentation = (
                    None if committed_message is None else committed_message.accepted_presentation
                )
                deliver = getattr(self.capability, "deliver_committed", None)
                if presentation is not None and callable(deliver):
                    # A delivery error escapes to Temporal for retry. The
                    # canonical terminal records are already committed; replay
                    # will redeliver the same disclosure ID without re-running
                    # retrieval/generation or changing the outcome.
                    await deliver(terminal.disclosure_record, presentation)
            return AuthorizedAskResult(
                command_id=command.command_id,
                session_id=command.session_id,
                message_id=command.message_id,
                output=terminal.output,
                accepted_answer=terminal.accepted_answer,
                replay=capability.replay or message.state is QAMessageState.TERMINAL,
                availability=capability.availability,
            )
        if message.state is not QAMessageState.TERMINAL:
            await self.sessions.complete_message(
                context=command.persistence_context,
                session_id=command.session_id,
                message_id=command.message_id,
                answer=(
                    capability.accepted_answer.answer.text
                    if capability.accepted_answer is not None
                    else None
                ),
                outcome=capability.output.outcome,
                cancelled=capability.output.outcome is AnswerQuestionOutcome.CANCELLED,
                idempotency_key=_derived_key(
                    "admitted-complete", command.idempotency_key
                ),
            )
        return AuthorizedAskResult(
            command_id=command.command_id,
            session_id=command.session_id,
            message_id=command.message_id,
            output=capability.output,
            accepted_answer=capability.accepted_answer,
            replay=capability.replay or message.state is QAMessageState.TERMINAL,
            availability=capability.availability,
        )

    async def cancel(self, command: AskCancellationCommand) -> AuthorizedAskResult:
        capability = await self.capability.cancel(
            AnswerQuestionCapabilityCommand(
                request_context=command.request_context,
                persistence_context=command.persistence_context,
                session_id=None,
                message_id=None,
                question=None,
                run_id=command.run_id,
                idempotency_key=command.idempotency_key,
            )
        )
        _validate_capability_scope(command.request_context, capability)
        if self.attempts is not None:
            if capability.output.correlation.run_id != command.run_id:
                raise InvalidAuthorizedAskRequest(
                    "cancellation result returned a different canonical run"
                )
            terminal = await self.attempts.terminalize(
                context=command.persistence_context,
                session_id=capability.session_id,
                message_id=capability.message_id,
                idempotency_key=command.idempotency_key.value,
                sessions=self.sessions,
                receipt=capability.terminalization_receipt,
            )
            if terminal.output != capability.output:
                raise InvalidAuthorizedAskRequest(
                    "canonical cancellation outcome conflicts with the capability result"
                )
            if terminal.accepted_answer is not None and terminal.disclosure_record is not None:
                committed_message = await self.sessions.get_message(
                    context=command.persistence_context,
                    requester=QASessionRequester(
                        subject_id=command.request_context.subject_id,
                        authorization_generation=command.request_context.authorization_generation,
                    ),
                    session_id=capability.session_id,
                    message_id=capability.message_id,
                )
                presentation = (
                    None if committed_message is None else committed_message.accepted_presentation
                )
                deliver = getattr(self.capability, "deliver_committed", None)
                if presentation is not None and callable(deliver):
                    await deliver(terminal.disclosure_record, presentation)
            return AuthorizedAskResult(
                command_id=capability.command_id,
                session_id=capability.session_id,
                message_id=capability.message_id,
                output=terminal.output,
                accepted_answer=terminal.accepted_answer,
                replay=capability.replay,
                availability=capability.availability,
            )
        message = await self.sessions.get_message(
            context=command.persistence_context,
            requester=QASessionRequester(
                subject_id=command.request_context.subject_id,
                authorization_generation=command.request_context.authorization_generation,
            ),
            session_id=capability.session_id,
            message_id=capability.message_id,
        )
        if message is None:
            raise AuthorizedAskServiceUnavailable()
        if message.state is not QAMessageState.TERMINAL:
            if message.run_id is None:
                await self.sessions.link_message_to_run(
                    context=command.persistence_context,
                    session_id=capability.session_id,
                    message_id=capability.message_id,
                    run_id=capability.output.correlation.run_id,
                    idempotency_key=_derived_key(
                        "cancel-link", command.idempotency_key
                    ),
                )
            await self.sessions.complete_message(
                context=command.persistence_context,
                session_id=capability.session_id,
                message_id=capability.message_id,
                answer=(
                    capability.accepted_answer.answer.text
                    if capability.accepted_answer is not None
                    else None
                ),
                outcome=capability.output.outcome,
                cancelled=capability.output.outcome is AnswerQuestionOutcome.CANCELLED,
                idempotency_key=_derived_key(
                    "cancel-complete", command.idempotency_key
                ),
            )
        return AuthorizedAskResult(
            command_id=capability.command_id,
            session_id=capability.session_id,
            message_id=capability.message_id,
            output=capability.output,
            accepted_answer=capability.accepted_answer,
            replay=capability.replay,
            availability=capability.availability,
        )

    async def _finish(
        self,
        *,
        command: AskCommand,
        message: QAMutationResult[QAMessage],
        capability: AnswerQuestionCapabilityResult,
        operation_prefix: str,
    ) -> AuthorizedAskResult:
        _validate_capability_scope(command.request_context, capability)
        if (
            capability.session_id != command.session_id
            or capability.message_id != message.value.message_id
        ):
            raise InvalidAuthorizedAskRequest(
                "answer capability returned mismatched message identity"
            )
        message_id = message.value.message_id
        if message.value.state is not QAMessageState.TERMINAL:
            await self.sessions.link_message_to_run(
                context=command.persistence_context,
                session_id=command.session_id,
                message_id=message_id,
                run_id=capability.output.correlation.run_id,
                idempotency_key=_derived_key("link", command.idempotency_key),
            )
            await self.sessions.complete_message(
                context=command.persistence_context,
                session_id=command.session_id,
                message_id=message_id,
                answer=(
                    capability.accepted_answer.answer.text
                    if capability.accepted_answer is not None
                    else None
                ),
                outcome=capability.output.outcome,
                cancelled=capability.output.outcome is AnswerQuestionOutcome.CANCELLED,
                idempotency_key=_derived_key(operation_prefix, command.idempotency_key),
            )
        return AuthorizedAskResult(
            command_id=capability.command_id,
            session_id=command.session_id,
            message_id=message_id,
            output=capability.output,
            accepted_answer=capability.accepted_answer,
            replay=message.replay or capability.replay,
            availability=capability.availability,
        )


def _validate_capability_scope(
    request_context: AskRequestContext,
    result: AnswerQuestionCapabilityResult,
) -> None:
    if (
        result.output.scope.workspace_id != request_context.workspace_id
        or result.output.scope.environment_id != request_context.environment_id
        or result.output.correlation.trace_id != request_context.trace_id
        or result.output.correlation.correlation_id != request_context.correlation_id
        or result.output.correlation.causation_id != request_context.causation_id
    ):
        raise InvalidAuthorizedAskRequest(
            "answer capability returned an out-of-context result"
        )


def _derived_key(prefix: str, key: IdempotencyKey) -> IdempotencyKey:
    import hashlib

    digest = hashlib.sha256(key.value.encode("utf-8")).hexdigest()
    return IdempotencyKey(f"ask-{prefix}-{digest}")


__all__ = [
    "AnswerQuestionCapabilityAvailability",
    "AnswerQuestionCapability",
    "AnswerQuestionCapabilityCommand",
    "AnswerQuestionCapabilityResult",
    "AnswerQuestionAttemptApplication",
    "AskTerminalPersistence",
    "AskCancellationCommand",
    "AskExecutionCommand",
    "AskCommand",
    "AuthorizedAskResult",
    "AuthorizedAskService",
    "AuthorizedAskServiceUnavailable",
    "AskRequestContext",
    "InvalidAuthorizedAskRequest",
    "PersistedAuthorizedAskService",
]
