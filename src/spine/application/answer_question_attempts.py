"""Canonical allocation of retry-stable durable Q&A attempts."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from spine.application.answer_question_execution import (
    AnswerQuestionExecutionBoundary,
    AnswerQuestionExecutionRequest,
)
from spine.application.persistence.answer_question_runs import (
    AnswerQuestionAttemptAllocation,
    AnswerQuestionAttemptAllocationResult,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import InvalidPersistenceContextError
from spine.application.persistence.unit_of_work import UnitOfWorkFactory


@dataclass(frozen=True, slots=True)
class PersistedAnswerQuestionAttemptService:
    """Resolve execution configuration and allocate before authorized retrieval."""

    uow_factory: UnitOfWorkFactory
    execution: AnswerQuestionExecutionBoundary

    async def allocate_or_resume(
        self,
        *,
        context: TrustedPersistenceContext,
        run_id: UUID,
        logical_execution_key: str,
    ) -> AnswerQuestionAttemptAllocationResult:
        context.validate_shape()
        scope = context.scope
        if not isinstance(scope, EnvironmentScope):
            raise InvalidPersistenceContextError(
                "Q&A attempt allocation requires an environment scope"
            )
        async with self.uow_factory(context) as uow:
            run = await uow.answer_question_runs.resolve_run_in_scope(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                run_id=run_id,
            )
            _, pin = self.execution.resolve(
                AnswerQuestionExecutionRequest(
                    workspace_id=scope.workspace_id,
                    environment_id=scope.environment_id,
                    environment=run.environment,
                )
            )
            result = await uow.answer_question_runs.allocate_or_resume_attempt(
                AnswerQuestionAttemptAllocation(
                    run_id=run_id,
                    logical_execution_key=logical_execution_key,
                    execution_pin=pin,
                    input_artifact_ids=run.input_artifact_ids,
                )
            )
            await uow.commit()
        return result


__all__ = [
    "PersistedAnswerQuestionAttemptService",
]
