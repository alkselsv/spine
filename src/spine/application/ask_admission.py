"""Application use case for durable, execution-free Ask admission."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from spine.application.persistence.ask_admission import (
    AskAdmissionCommand,
    AskAdmissionLifecycle,
    AskAdmissionResult,
    AskAdmissionStatus,
    QA_ASK_ADMISSION_OPERATION,
    validate_ask_admission_context,
)
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import InvalidPersistenceContextError
from spine.domain.capabilities.answer_question_runs import AnswerQuestionRunState


@dataclass(frozen=True, slots=True)
class PersistedAskAdmissionService:
    """Commit one admission and dispatch intent as one database transaction."""

    uow_factory: UnitOfWorkFactory

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
            await uow.commit()
        if run.state is AnswerQuestionRunState.COMPLETED:
            return AskAdmissionStatus(
                command_id=result.command_id,
                session_id=result.session_id,
                message_id=result.message_id,
                run_id=result.run_id,
                status=AskAdmissionLifecycle.TERMINAL,
                outcome=run.outcome,
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
        )


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


__all__ = ["PersistedAskAdmissionService"]
