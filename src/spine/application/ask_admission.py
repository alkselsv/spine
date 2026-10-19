"""Application use case for durable, execution-free Ask admission."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID
from uuid import NAMESPACE_URL, uuid5

from spine.application.persistence.ask_admission import (
    AskAdmissionCommand,
    AskAdmissionLifecycle,
    AskAdmissionResult,
    AskAdmissionStatus,
    AskCancellationLifecycle,
    AskCancellationStatus,
    QA_ASK_ADMISSION_OPERATION,
    QA_ASK_LEGACY_OPERATION,
    validate_ask_admission_context,
)
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import InvalidPersistenceContextError
from spine.application.persistence.errors import AnswerQuestionRunNotFoundError
from spine.domain.capabilities.answer_question import AnswerQuestionOutcome
from spine.domain.capabilities.answer_question_runs import (
    AnswerQuestionAttemptState,
    AnswerQuestionRunState,
)
from spine.domain.qa_sessions import QAMessageState


class AskCancellationNotFound(LookupError):
    """A run is unavailable in the caller's current authorized scope."""


@dataclass(frozen=True, slots=True)
class PersistedAskAdmissionService:
    """Commit one admission and dispatch intent as one database transaction."""

    uow_factory: UnitOfWorkFactory
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    sleeper: Callable[[float], Awaitable[object]] = asyncio.sleep

    async def admit(
        self,
        *,
        context: TrustedPersistenceContext,
        command: AskAdmissionCommand,
    ) -> AskAdmissionResult:
        validate_ask_admission_context(context, command)
        async with self.uow_factory(context) as uow:
            result = await uow.ask_admissions.admit(command)
            await uow.commit()
        return result

    async def status(
        self,
        *,
        context: TrustedPersistenceContext,
        result: AskAdmissionResult,
    ) -> AskAdmissionStatus:
        _validate_status_context(context)
        async with self.uow_factory(context) as uow:
            run = await uow.answer_question_runs.resolve_run_in_scope(
                workspace_id=result_workspace(context),
                environment_id=result_environment(context),
                run_id=result.run_id,
            )
            actor_id = context.acting_subject_id or context.service_principal_id
            if actor_id is None or run.subject.id != actor_id:
                raise InvalidPersistenceContextError("Ask admission status is unavailable")
            link = await uow.answer_question_runs.resolve_message_link(result.run_id)
            if link is None or link.session_id != result.session_id or link.message_id != result.message_id:
                raise ValueError("Ask admission status lineage is invalid")
            attempt_id = None
            attempt_number = None
            if run.state is AnswerQuestionRunState.RUNNING:
                attempts = await uow.answer_question_runs.list_attempts(run.run_id)
                active_attempts = tuple(
                    attempt
                    for attempt in attempts
                    if attempt.state is AnswerQuestionAttemptState.RUNNING
                )
                if len(active_attempts) > 1:
                    raise ValueError("Ask admission status attempt lineage is ambiguous")
                if active_attempts:
                    attempt_id = active_attempts[0].attempt_id
                    attempt_number = active_attempts[0].attempt_number
            elif run.final_attempt_id is not None:
                final_attempt = await uow.answer_question_runs.resolve_attempt(
                    run.final_attempt_id
                )
                if final_attempt.run_id != run.run_id:
                    raise ValueError("Ask admission status attempt lineage is invalid")
                attempt_id = final_attempt.attempt_id
                attempt_number = final_attempt.attempt_number
            await uow.commit()
        if run.state is AnswerQuestionRunState.COMPLETED:
            return AskAdmissionStatus(
                command_id=result.command_id,
                session_id=result.session_id,
                message_id=result.message_id,
                run_id=result.run_id,
                status=AskAdmissionLifecycle.TERMINAL,
                outcome=run.outcome,
                attempt_id=run.final_attempt_id,
                attempt_number=attempt_number,
            )
        return AskAdmissionStatus(
            command_id=result.command_id,
            session_id=result.session_id,
            message_id=result.message_id,
            run_id=result.run_id,
            status=(
                AskAdmissionLifecycle.RUNNING
                if run.state is AnswerQuestionRunState.RUNNING
                else AskAdmissionLifecycle.ADMITTED
            ),
            attempt_id=attempt_id,
            attempt_number=attempt_number,
        )

    async def request_cancellation(
        self,
        *,
        context: TrustedPersistenceContext,
        run_id: UUID,
    ) -> AskCancellationStatus:
        """Commit cancellation intent and its Temporal dispatch intent atomically."""

        _validate_cancellation_context(context)
        actor_id = context.acting_subject_id or context.service_principal_id
        if actor_id is None:
            raise AskCancellationNotFound()
        try:
            async with self.uow_factory(context) as uow:
                run = await uow.answer_question_runs.resolve_run_in_scope(
                    workspace_id=result_workspace(context),
                    environment_id=result_environment(context),
                    run_id=run_id,
                )
                if run.subject.id != actor_id:
                    raise AskCancellationNotFound()
                replay = (
                    run.cancellation_requested_at is not None
                    or run.state is AnswerQuestionRunState.COMPLETED
                )
                link = await uow.answer_question_runs.resolve_message_link(run_id)
                if link is None:
                    raise AskCancellationNotFound()
                message = await uow.qa_sessions.resolve_message(
                    workspace_id=result_workspace(context),
                    environment_id=result_environment(context),
                    session_id=link.session_id,
                    message_id=link.message_id,
                )
                if message is None or message.run_id != run_id:
                    raise AskCancellationNotFound()
                if (
                    run.state is AnswerQuestionRunState.COMPLETED
                    or message.state is QAMessageState.TERMINAL
                ):
                    requested = run
                    terminal_outcome = run.outcome or message.terminal_outcome
                    replay = True
                else:
                    requested = await uow.answer_question_runs.request_cancellation(
                        run_id=run_id,
                        requested_at=self.clock(),
                    )
                    terminal_outcome = requested.outcome
                await uow.commit()
        except AnswerQuestionRunNotFoundError:
            raise AskCancellationNotFound() from None
        is_terminal = (
            requested.state is AnswerQuestionRunState.COMPLETED
            or message.state is QAMessageState.TERMINAL
        )
        return AskCancellationStatus(
            command_id=uuid5(NAMESPACE_URL, f"spine.qa.cancel-command:v1:{run_id}"),
            run_id=run_id,
            status=(
                AskCancellationLifecycle.TERMINAL
                if is_terminal
                else AskCancellationLifecycle.CANCELLATION_REQUESTED
            ),
            outcome=terminal_outcome if is_terminal else None,
            replay=replay,
            session_id=link.session_id,
            message_id=link.message_id,
            attempt_id=requested.final_attempt_id,
        )

    async def wait_for_cancellation(
        self,
        *,
        context: TrustedPersistenceContext,
        run_id: UUID,
        poll_interval: float = 0.05,
        timeout: float = 5.0,
    ) -> AskCancellationStatus:
        """Wait briefly for a terminal result owned by Temporal."""

        if not 0 < poll_interval <= 5:
            raise ValueError("cancellation poll interval must be between zero and five seconds")
        if timeout <= 0:
            raise ValueError("cancellation wait timeout must be positive")

        async def wait_until_terminal() -> AskCancellationStatus:
            while True:
                status = await self.request_cancellation(context=context, run_id=run_id)
                if status.status is AskCancellationLifecycle.TERMINAL:
                    return status
                await self.sleeper(poll_interval)

        return await asyncio.wait_for(wait_until_terminal(), timeout=timeout)


def result_workspace(context: TrustedPersistenceContext) -> UUID:
    scope = context.scope
    if not isinstance(scope, EnvironmentScope):
        raise InvalidPersistenceContextError("Ask admission requires an environment scope")
    return scope.workspace_id


def result_environment(context: TrustedPersistenceContext) -> UUID:
    scope = context.scope
    if not isinstance(scope, EnvironmentScope):
        raise InvalidPersistenceContextError("Ask admission requires an environment scope")
    return scope.environment_id


def _validate_status_context(context: TrustedPersistenceContext) -> None:
    try:
        context.validate_shape()
        if not isinstance(context.scope, EnvironmentScope):
            raise InvalidPersistenceContextError(
                "Ask admission requires an environment scope"
            )
        if context.operation != QA_ASK_ADMISSION_OPERATION:
            raise InvalidPersistenceContextError("Ask admission context is invalid")
    except InvalidPersistenceContextError:
        raise
    except Exception:
        raise InvalidPersistenceContextError("Ask admission context is invalid") from None


def _validate_cancellation_context(context: TrustedPersistenceContext) -> None:
    try:
        context.validate_shape()
        if (
            not isinstance(context.scope, EnvironmentScope)
            or context.operation
            not in {QA_ASK_ADMISSION_OPERATION, QA_ASK_LEGACY_OPERATION}
        ):
            raise InvalidPersistenceContextError("Ask cancellation context is invalid")
    except InvalidPersistenceContextError:
        raise
    except Exception:
        raise InvalidPersistenceContextError("Ask cancellation context is invalid") from None


__all__ = ["PersistedAskAdmissionService"]
