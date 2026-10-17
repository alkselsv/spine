"""Scoped persistence adapter for the durable Ask workflow activity."""

from __future__ import annotations

from collections.abc import Callable

from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.errors import (
    AnswerQuestionRunNotFoundError,
    PersistenceError,
)
from spine.application.persistence.unit_of_work import TenantUnitOfWorkFactory
from spine.application.workflows.ask_admission_dispatch import (
    AskAdmissionCommandData,
    AskAdmissionCommandSource,
    AskAdmissionWorkflowInput,
)
from spine.domain.capabilities.answer_question_runs import AnswerQuestionRunState


class PersistenceAskAdmissionCommandSource(AskAdmissionCommandSource):
    """Read one admitted command through the existing scoped UoW ports.

    The context factory is composition-root owned. It must issue a fresh,
    trusted worker context for the exact workflow scope and current policy
    generation; this adapter never accepts caller-provided authority.
    """

    def __init__(
        self,
        *,
        uow_factory: TenantUnitOfWorkFactory,
        context_factory: Callable[
            [AskAdmissionWorkflowInput], TrustedPersistenceContext
        ],
    ) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory

    async def load_exact(
        self, workflow_input: AskAdmissionWorkflowInput
    ) -> AskAdmissionCommandData | None:
        context = self._context_factory(workflow_input)
        try:
            async with self._uow_factory(context) as uow:
                run = await uow.answer_question_runs.resolve_run_in_scope(
                    workspace_id=workflow_input.workspace_id,
                    environment_id=workflow_input.environment_id,
                    run_id=workflow_input.run_id,
                )
                if run.state is AnswerQuestionRunState.COMPLETED:
                    return None
                link = await uow.answer_question_runs.resolve_message_link(
                    workflow_input.run_id
                )
                if link is None or (
                    link.session_id != workflow_input.session_id
                    or link.message_id != workflow_input.message_id
                ):
                    return None
                message = await uow.qa_sessions.resolve_message(
                    workspace_id=workflow_input.workspace_id,
                    environment_id=workflow_input.environment_id,
                    session_id=workflow_input.session_id,
                    message_id=workflow_input.message_id,
                )
                if message is None or (
                    message.run_id != workflow_input.run_id
                    or message.workspace_id != workflow_input.workspace_id
                    or message.environment_id != workflow_input.environment_id
                ):
                    return None
                return AskAdmissionCommandData(
                    command_id=workflow_input.command_id,
                    workspace_id=run.workspace_id,
                    environment_id=run.environment_id,
                    session_id=message.session_id,
                    message_id=message.message_id,
                    run_id=run.run_id,
                    question=message.question,
                )
        except (AnswerQuestionRunNotFoundError, PersistenceError):
            return None


__all__ = ["PersistenceAskAdmissionCommandSource"]
