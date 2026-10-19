"""Canonical allocation of retry-stable durable Q&A attempts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from uuid import UUID, uuid4

from spine.application.answer_question_execution import (
    AnswerQuestionExecutionBoundary,
    AnswerQuestionExecutionRequest,
)
from spine.application.persistence.answer_question_runs import (
    AnswerQuestionAttemptAllocation,
    AnswerQuestionAttemptAllocationResult,
    AnswerQuestionAttemptTerminalResult,
    CanonicalQATerminalizationReceipt,
    validate_answer_question_context_bundle,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedPersistenceContext,
)
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.errors import InvalidPersistenceContextError
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.application.persistence.errors import AnswerQuestionRunConflictError
from spine.application.qa_sessions import QAMessageState, QASessionService
from spine.domain.agents import AgentRunStatus
from spine.domain.artifacts import ArtifactState
from spine.domain.capabilities import AcceptedGroundedAnswer, AnswerQuestionOutput
from spine.domain.capabilities.answer_question import (
    AnswerQuestionOutcome,
    AnswerQuestionScope,
    LineageReference,
    RunAttemptCorrelation,
)
from spine.domain.capabilities.answer_question_runs import (
    AnswerQuestionAttemptEvent,
    AnswerQuestionAttemptEventKind,
    AnswerQuestionAttemptState,
    AnswerQuestionRunState,
    AnswerQuestionOutcome,
    SafeAttemptReason,
)
from spine.domain.disclosure import DisclosureDependencySet


@dataclass(frozen=True, slots=True)
class PersistedAnswerQuestionAttemptService:
    """Resolve execution configuration and allocate before authorized retrieval."""

    uow_factory: UnitOfWorkFactory
    execution: AnswerQuestionExecutionBoundary
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    event_id_factory: Callable[[], UUID] = uuid4

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
    ) -> AnswerQuestionAttemptTerminalResult:
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
            disclosure_record=record,
        )

    async def terminalize(
        self,
        *,
        context: TrustedPersistenceContext,
        session_id: UUID,
        message_id: UUID,
        idempotency_key: str,
        sessions: QASessionService,
        receipt: CanonicalQATerminalizationReceipt | None,
    ) -> AnswerQuestionAttemptTerminalResult:
        """Commit attempt, run and message terminal state in one tenant UoW."""

        context.validate_shape()
        scope = context.scope
        if not isinstance(scope, EnvironmentScope):
            raise InvalidPersistenceContextError(
                "Q&A terminalization requires an environment scope"
            )
        async with self.uow_factory(context) as uow:
            # This row lock serializes completion, cancellation intent, and
            # overlapping Temporal activities through the same canonical run.
            run = await uow.answer_question_runs.resolve_run_for_update(
                receipt.run_id if receipt is not None else await self._linked_run_id(
                    uow, scope.workspace_id, scope.environment_id, session_id, message_id
                )
            )
            link = await uow.answer_question_runs.resolve_message_link(run.run_id)
            if link is None or (link.session_id, link.message_id) != (session_id, message_id):
                raise AnswerQuestionRunConflictError(
                    "Q&A terminal message link is inconsistent."
                )
            message = await uow.qa_sessions.resolve_message(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                session_id=session_id,
                message_id=message_id,
            )
            if (
                message is None
                or message.run_id != run.run_id
                or message.workspace_id != scope.workspace_id
                or message.environment_id != scope.environment_id
            ):
                raise AnswerQuestionRunConflictError(
                    "Q&A terminal message is unavailable."
                )

            attempt_id = (
                receipt.attempt_id
                if receipt is not None
                else run.final_attempt_id
            )
            if attempt_id is None:
                raise AnswerQuestionRunConflictError(
                    "Q&A terminal attempt is unavailable."
                )
            attempt = await uow.answer_question_runs.resolve_attempt(attempt_id)
            if (
                attempt.run_id != run.run_id
                or attempt.workspace_id != scope.workspace_id
                or attempt.environment_id != scope.environment_id
            ):
                raise AnswerQuestionRunConflictError(
                    "Q&A terminal attempt identity is inconsistent."
                )

            if receipt is not None:
                self._validate_receipt(receipt, run, attempt, scope.workspace_id, scope.environment_id)
            if attempt.state is AnswerQuestionAttemptState.RUNNING:
                if receipt is None:
                    raise AnswerQuestionRunConflictError(
                        "Q&A terminal receipt is unavailable."
                    )
                record = await uow.answer_disclosures.resolve_for_attempt(
                    workspace_id=scope.workspace_id,
                    environment_id=scope.environment_id,
                    run_id=run.run_id,
                    attempt_id=attempt.attempt_id,
                )
                if record is None or record != receipt.disclosure_record:
                    raise AnswerQuestionRunConflictError(
                        "Persisted Q&A disclosure does not match the execution receipt."
                    )
                bundle = await self._resolve_and_validate_bundle(
                    uow, attempt, receipt
                )
                if run.state is not AnswerQuestionRunState.RUNNING:
                    raise AnswerQuestionRunConflictError(
                        "Q&A run is not eligible for terminalization."
                    )
                completed_at = self.clock()
                state, reason = _attempt_terminal_state(receipt.output)
                result_ref, result = _persistable_agent_result(receipt, state)
                output_ids = (
                    receipt.output_artifact_ids
                    if result is not None
                    else ()
                )
                terminal_attempt = attempt.complete(
                    state=state,
                    outcome=receipt.output.outcome,
                    usage=receipt.usage or attempt.usage,
                    terminal_event=AnswerQuestionAttemptEvent(
                        event_id=self.event_id_factory(),
                        workspace_id=attempt.workspace_id,
                        environment_id=attempt.environment_id,
                        environment=attempt.environment,
                        kind=_terminal_event_kind(state),
                        occurred_at=completed_at,
                        reason=reason,
                    ),
                    reason=reason,
                    context_bundle_ref=(
                        None if bundle is None else bundle.reference
                    ),
                    output_artifact_ids=output_ids,
                    agent_result_ref=result_ref,
                    agent_result=result,
                )
                attempt = await uow.answer_question_runs.complete_attempt(
                    terminal_attempt
                )
            else:
                record = await uow.answer_disclosures.resolve_for_attempt(
                    workspace_id=scope.workspace_id,
                    environment_id=scope.environment_id,
                    run_id=run.run_id,
                    attempt_id=attempt.attempt_id,
                )
                if record is None or record.outcome is not attempt.outcome:
                    raise AnswerQuestionRunConflictError(
                        "Persisted Q&A disclosure does not match the terminal attempt."
                    )
                if receipt is not None and record != receipt.disclosure_record:
                    raise AnswerQuestionRunConflictError(
                        "Terminal replay conflicts with the persisted disclosure."
                    )
                bundle = await self._resolve_attempt_bundle(uow, attempt)

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
            if (
                output.correlation.run_id != run.run_id
                or output.correlation.attempt_id != attempt.attempt_id
                or output.scope.workspace_id != scope.workspace_id
                or output.scope.environment_id != scope.environment_id
                or output.outcome is not attempt.outcome
                or (run.state is AnswerQuestionRunState.COMPLETED and (
                    run.final_attempt_id != attempt.attempt_id
                    or run.outcome is not attempt.outcome
                    or run.output_artifact_ids != attempt.output_artifact_ids
                ))
            ):
                raise AnswerQuestionRunConflictError(
                    "Canonical Q&A terminal records conflict."
                )

            if run.state is AnswerQuestionRunState.RUNNING:
                assert attempt.completed_at is not None and attempt.outcome is not None
                run = await uow.answer_question_runs.complete_run(
                    run.complete(
                        outcome=attempt.outcome,
                        final_attempt_id=attempt.attempt_id,
                        output_artifact_ids=attempt.output_artifact_ids,
                        completed_at=attempt.completed_at,
                    )
                )
            if message.state is QAMessageState.TERMINAL:
                if (
                    message.terminal_outcome is not run.outcome
                    or message.answer != (
                        record.accepted_answer.answer.text
                        if record.accepted_answer is not None
                        else None
                    )
                    or message.cancelled
                    != (run.outcome is AnswerQuestionOutcome.CANCELLED)
                ):
                    raise AnswerQuestionRunConflictError(
                        "Canonical Q&A message conflicts with the terminal run."
                    )
            else:
                dependencies = (
                    DisclosureDependencySet.from_context_bundle(bundle)
                    if bundle is not None
                    else DisclosureDependencySet()
                )
                await sessions.complete_message_in_transaction(
                    repository=uow.qa_sessions,
                    context=context,
                    session_id=session_id,
                    message_id=message_id,
                    answer=(
                        record.accepted_answer.answer.text
                        if record.accepted_answer is not None
                        else None
                    ),
                    outcome=run.outcome,
                    cancelled=run.outcome is AnswerQuestionOutcome.CANCELLED,
                    idempotency_key=_terminal_message_key(idempotency_key),
                    disclosure_dependencies=dependencies,
                )
            await uow.commit()
        return AnswerQuestionAttemptTerminalResult(
            output=output,
            accepted_answer=record.accepted_answer,
            disclosure_record=record,
        )

    async def _linked_run_id(self, uow, workspace_id, environment_id, session_id, message_id):
        message = await uow.qa_sessions.resolve_message(
            workspace_id=workspace_id,
            environment_id=environment_id,
            session_id=session_id,
            message_id=message_id,
        )
        if message is None or message.run_id is None:
            raise AnswerQuestionRunConflictError("Q&A terminal run is unavailable.")
        return message.run_id

    async def _resolve_and_validate_bundle(self, uow, attempt, receipt):
        if receipt.context_bundle_ref is None:
            if receipt.disclosure_dependencies.dependencies:
                raise AnswerQuestionRunConflictError(
                    "Q&A receipt has disclosure lineage without a ContextBundle."
                )
            return None
        bundle = await uow.context_bundles.resolve(
            bundle_id=receipt.context_bundle_ref.bundle_id,
            bundle_version=receipt.context_bundle_ref.bundle_version,
            bundle_digest=receipt.context_bundle_ref.bundle_digest,
        )
        try:
            validate_answer_question_context_bundle(
                attempt.model_copy(update={"context_bundle_ref": receipt.context_bundle_ref}),
                bundle,
            )
        except ValueError as exc:
            raise AnswerQuestionRunConflictError(
                "Q&A receipt ContextBundle lineage is invalid."
            ) from exc
        dependencies = DisclosureDependencySet.from_context_bundle(bundle)
        if dependencies != receipt.disclosure_dependencies:
            raise AnswerQuestionRunConflictError(
                "Q&A receipt disclosure dependencies do not match its ContextBundle."
            )
        return bundle

    async def _resolve_attempt_bundle(self, uow, attempt):
        if attempt.context_bundle_ref is None:
            return None
        bundle = await uow.context_bundles.resolve(
            bundle_id=attempt.context_bundle_ref.bundle_id,
            bundle_version=attempt.context_bundle_ref.bundle_version,
            bundle_digest=attempt.context_bundle_ref.bundle_digest,
        )
        try:
            validate_answer_question_context_bundle(attempt, bundle)
        except ValueError as exc:
            raise AnswerQuestionRunConflictError(
                "Persisted Q&A ContextBundle lineage is invalid."
            ) from exc
        return bundle

    @staticmethod
    def _validate_receipt(receipt, run, attempt, workspace_id, environment_id) -> None:
        if (
            run.run_id != receipt.run_id
            or run.workspace_id != workspace_id
            or run.environment_id != environment_id
            or attempt.attempt_id != receipt.attempt_id
            or attempt.attempt_number != receipt.attempt_number
            or attempt.execution_pin != receipt.execution_pin
            or receipt.output.scope.workspace_id != workspace_id
            or receipt.output.scope.environment_id != environment_id
            or receipt.output.correlation.run_id != run.run_id
            or receipt.output.correlation.attempt_id != attempt.attempt_id
            or (
                receipt.output.outcome is AnswerQuestionOutcome.ANSWERED
                and (
                    receipt.context_bundle_ref is None
                    or receipt.agent_result_ref is None
                    or receipt.agent_result is None
                    or not receipt.output_artifact_ids
                )
            )
            or any(
                artifact.state is not ArtifactState.ACTIVE
                for artifact in receipt.candidate_output_artifacts
            )
        ):
            raise AnswerQuestionRunConflictError(
                "Q&A execution receipt does not match the persisted attempt."
            )


def _attempt_terminal_state(output: AnswerQuestionOutput):
    if output.outcome is AnswerQuestionOutcome.FAILED:
        reason = SafeAttemptReason.INTERNAL_FAILURE
        if output.reason is not None and output.reason.code.value == "rejected_candidate":
            reason = SafeAttemptReason.VALIDATION_FAILED
        return AnswerQuestionAttemptState.FAILED, reason
    if output.outcome is AnswerQuestionOutcome.CANCELLED:
        return AnswerQuestionAttemptState.CANCELLED, SafeAttemptReason.CANCELLED_BY_REQUEST
    return AnswerQuestionAttemptState.SUCCEEDED, None


def _terminal_event_kind(state: AnswerQuestionAttemptState) -> AnswerQuestionAttemptEventKind:
    return {
        AnswerQuestionAttemptState.SUCCEEDED: AnswerQuestionAttemptEventKind.COMPLETED,
        AnswerQuestionAttemptState.FAILED: AnswerQuestionAttemptEventKind.FAILED,
        AnswerQuestionAttemptState.CANCELLED: AnswerQuestionAttemptEventKind.CANCELLED,
    }[state]


def _persistable_agent_result(receipt, state):
    if receipt.agent_result is None:
        return None, None
    status = receipt.agent_result.status
    if state is AnswerQuestionAttemptState.SUCCEEDED and status is AgentRunStatus.SUCCEEDED:
        return receipt.agent_result_ref, receipt.agent_result
    if state is AnswerQuestionAttemptState.FAILED and status is AgentRunStatus.FAILED:
        return receipt.agent_result_ref, receipt.agent_result
    if state is AnswerQuestionAttemptState.CANCELLED and status is AgentRunStatus.CANCELLED:
        return receipt.agent_result_ref, receipt.agent_result
    # A successful candidate-generation AgentResult is not the terminal
    # AgentResult for a later Q&A validation failure/cancellation.
    return None, None


def _terminal_message_key(idempotency_key: str) -> IdempotencyKey:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    return IdempotencyKey(f"qa-terminal-message-{digest}")


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
