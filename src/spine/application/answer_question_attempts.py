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
    AnswerQuestionAttemptTerminalResult,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import InvalidPersistenceContextError
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.application.persistence.errors import AnswerQuestionRunConflictError
from spine.domain.capabilities import AcceptedGroundedAnswer, AnswerQuestionOutput
from spine.domain.capabilities.answer_question import (
    AnswerQuestionOutcome,
    AnswerQuestionScope,
    LineageReference,
    RunAttemptCorrelation,
)
from spine.domain.capabilities.answer_question_runs import AnswerQuestionAttemptState


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

    async def resolve_terminal_result(
        self,
        *,
        context: TrustedPersistenceContext,
        run_id: UUID,
        attempt_id: UUID,
    ) -> "PersistedAnswerQuestionAttemptTerminalResult":
        """Read the canonical terminal disclosure without repeating retrieval."""

        context.validate_shape()
        scope = context.scope
        if not isinstance(scope, EnvironmentScope):
            raise InvalidPersistenceContextError(
                "Q&A attempt result resolution requires an environment scope"
            )
        async with self.uow_factory(context) as uow:
            run = await uow.answer_question_runs.resolve_run_in_scope(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                run_id=run_id,
            )
            attempt = await uow.answer_question_runs.resolve_attempt(attempt_id)
            if (
                attempt.run_id != run.run_id
                or attempt.workspace_id != scope.workspace_id
                or attempt.environment_id != scope.environment_id
                or attempt.state is AnswerQuestionAttemptState.RUNNING
            ):
                raise AnswerQuestionRunConflictError(
                    "Q&A attempt has no terminal result."
                )
            record = await uow.answer_disclosures.resolve_for_attempt(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                run_id=run_id,
                attempt_id=attempt_id,
            )
            if record is None or record.outcome is not attempt.outcome:
                raise AnswerQuestionRunConflictError(
                    "Q&A attempt terminal result is unavailable."
                )
            output = record.output
            if output is None:
                output = _legacy_answered_output(
                    run_id=run.run_id,
                    attempt_id=attempt.attempt_id,
                    workspace_id=scope.workspace_id,
                    environment_id=scope.environment_id,
                    trace_id=run.diagnostic_context.trace_id,
                    correlation_id=run.diagnostic_context.correlation_id,
                    causation_id=run.diagnostic_context.causation_id,
                    accepted_answer=record.accepted_answer,
                )
            await uow.commit()
        return AnswerQuestionAttemptTerminalResult(
            output=output,
            accepted_answer=record.accepted_answer,
        )


def _legacy_answered_output(
    *,
    run_id: UUID,
    attempt_id: UUID,
    workspace_id: UUID,
    environment_id: UUID,
    trace_id: UUID,
    correlation_id: UUID | None,
    causation_id: UUID | None,
    accepted_answer: AcceptedGroundedAnswer | None,
) -> AnswerQuestionOutput:
    """Rehydrate records written before ``terminal_output`` was persisted."""

    if accepted_answer is None:
        raise AnswerQuestionRunConflictError("Q&A attempt terminal result is unavailable.")
    answer = accepted_answer.answer
    bundle_ids = tuple(
        sorted(
            {citation.context_bundle_id for citation in answer.citations},
            key=lambda value: value.int,
        )
    )
    return AnswerQuestionOutput(
        correlation=RunAttemptCorrelation(
            run_id=run_id,
            attempt_id=attempt_id,
            trace_id=trace_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
        ),
        scope=AnswerQuestionScope(
            workspace_id=workspace_id,
            environment_id=environment_id,
        ),
        outcome=AnswerQuestionOutcome.ANSWERED,
        answer=answer,
        lineage=(
            LineageReference(
                kind="run", identifier=run_id, schema_version="answer-question-run:v1"
            ),
            LineageReference(
                kind="attempt",
                identifier=attempt_id,
                schema_version="answer-question-attempt:v1",
            ),
            *(
                LineageReference(
                    kind="context_bundle",
                    identifier=bundle_id,
                    schema_version="context-bundle:v1",
                )
                for bundle_id in bundle_ids
            ),
        ),
    )


__all__ = [
    "PersistedAnswerQuestionAttemptService",
]
