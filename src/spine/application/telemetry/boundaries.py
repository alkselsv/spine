"""Observation decorators for concrete persistence handoff boundaries."""

from __future__ import annotations

from uuid import UUID

from spine.application.diagnostics import DiagnosticContext
from spine.application.persistence.answer_question_runs import (
    AnswerQuestionRunCommand,
    AnswerQuestionRunMessageLink,
    AnswerQuestionRunMutationResult,
    AnswerQuestionRunPage,
    AnswerQuestionRunPosition,
    AnswerQuestionRunQuery,
    AnswerQuestionRunRepository,
)
from spine.application.persistence.outbox import OutboxIntent, OutboxWriter
from spine.domain.capabilities.answer_question_runs import (
    AnswerQuestionAttempt,
    AnswerQuestionRun,
)

from .observations import (
    OperationalComponent,
    OperationalObservationEmitter,
    OperationalObservationOutcome,
    OperationalOperation,
    OperationalStage,
    SafeOperationalFailureCode,
)


class ObservedOutboxWriter:
    """Emit only after the wrapped outbox has accepted the intent."""

    def __init__(self, writer: OutboxWriter, observer: OperationalObservationEmitter) -> None:
        self._writer = writer
        self._observer = observer

    async def append(self, intent: OutboxIntent) -> UUID:
        context = DiagnosticContext(
            trace_id=intent.trace_id,
            correlation_id=intent.correlation_id,
            causation_id=intent.causation_id,
        )
        try:
            event_id = await self._writer.append(intent)
        except Exception:
            if intent.environment_id is not None:
                await self._emit(
                    intent,
                    context,
                    OperationalObservationOutcome.FAILED,
                    SafeOperationalFailureCode.PERSISTENCE_UNAVAILABLE,
                )
            raise
        if intent.event_id is not None and event_id != intent.event_id:
            raise ValueError("outbox writer substituted the trusted event identity")
        if intent.environment_id is not None:
            await self._emit(
                intent,
                context,
                OperationalObservationOutcome.SUCCEEDED,
            )
        return event_id

    async def _emit(
        self,
        intent: OutboxIntent,
        context: DiagnosticContext,
        outcome: OperationalObservationOutcome,
        failure_code: SafeOperationalFailureCode | None = None,
    ) -> None:
        assert intent.environment_id is not None
        await self._observer.emit(
            workspace_id=intent.workspace_id,
            environment_id=intent.environment_id,
            diagnostic_context=context,
            component=OperationalComponent.OUTBOX,
            stage=OperationalStage.ENQUEUE,
            operation=OperationalOperation.OUTBOX_ENQUEUE,
            outcome=outcome,
            safe_failure_code=failure_code,
        )


class ObservedAnswerQuestionRunRepository:
    """Observe actual durable run writes while preserving the repository port."""

    def __init__(
        self,
        repository: AnswerQuestionRunRepository,
        observer: OperationalObservationEmitter,
    ) -> None:
        self._repository = repository
        self._observer = observer

    async def create_run(
        self, command: AnswerQuestionRunCommand
    ) -> AnswerQuestionRunMutationResult:
        try:
            result = await self._repository.create_run(command)
        except Exception:
            await self._observe_failed(command.run)
            raise
        if result.run != command.run:
            raise ValueError("run repository substituted the trusted run")
        await self._observe(result.run, OperationalObservationOutcome.SUCCEEDED)
        return result

    async def complete_run(self, run: AnswerQuestionRun) -> AnswerQuestionRun:
        try:
            persisted = await self._repository.complete_run(run)
        except Exception:
            await self._observe_failed(run)
            raise
        if (
            persisted.run_id != run.run_id
            or persisted.workspace_id != run.workspace_id
            or persisted.environment_id != run.environment_id
            or persisted.diagnostic_context != run.diagnostic_context
        ):
            raise ValueError("run repository substituted trusted run context")
        await self._observe(persisted, OperationalObservationOutcome.SUCCEEDED)
        return persisted

    async def resolve_run(self, run_id: UUID) -> AnswerQuestionRun:
        return await self._repository.resolve_run(run_id)

    async def resolve_run_by_key(self, idempotency_key: str) -> AnswerQuestionRun:
        return await self._repository.resolve_run_by_key(idempotency_key)

    async def start_attempt(self, attempt: AnswerQuestionAttempt) -> AnswerQuestionAttempt:
        return await self._repository.start_attempt(attempt)

    async def complete_attempt(self, attempt: AnswerQuestionAttempt) -> AnswerQuestionAttempt:
        return await self._repository.complete_attempt(attempt)

    async def resolve_attempt(self, attempt_id: UUID) -> AnswerQuestionAttempt:
        return await self._repository.resolve_attempt(attempt_id)

    async def list_attempts(self, run_id: UUID) -> tuple[AnswerQuestionAttempt, ...]:
        return await self._repository.list_attempts(run_id)

    async def list_runs(
        self,
        *,
        query: AnswerQuestionRunQuery,
        limit: int,
        after: AnswerQuestionRunPosition | None,
    ) -> AnswerQuestionRunPage:
        return await self._repository.list_runs(query=query, limit=limit, after=after)

    async def resolve_message_link(
        self, run_id: UUID
    ) -> AnswerQuestionRunMessageLink | None:
        return await self._repository.resolve_message_link(run_id)

    async def _observe_failed(self, run: AnswerQuestionRun) -> None:
        await self._observe(
            run,
            OperationalObservationOutcome.FAILED,
            SafeOperationalFailureCode.PERSISTENCE_UNAVAILABLE,
        )

    async def _observe(
        self,
        run: AnswerQuestionRun,
        outcome: OperationalObservationOutcome,
        failure_code: SafeOperationalFailureCode | None = None,
    ) -> None:
        await self._observer.emit(
            workspace_id=run.workspace_id,
            environment_id=run.environment_id,
            diagnostic_context=DiagnosticContext(
                trace_id=run.diagnostic_context.trace_id,
                correlation_id=run.diagnostic_context.correlation_id,
                causation_id=run.diagnostic_context.causation_id,
            ),
            component=OperationalComponent.RUN,
            stage=OperationalStage.PERSISTENCE,
            operation=OperationalOperation.RUN_PERSISTENCE,
            outcome=outcome,
            safe_failure_code=failure_code,
            run_id=run.run_id,
        )


__all__ = ["ObservedAnswerQuestionRunRepository", "ObservedOutboxWriter"]
