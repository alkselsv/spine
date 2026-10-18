"""Production composition for the authorized ``answer_question`` capability.

This adapter owns request identity and stage ordering. Retrieval, candidate
execution, validation, persistence and disclosure remain behind their existing
application contracts so that the composition can be exercised with safe fakes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from spine.application.answer_question_context import (
    AnswerQuestionAuthorizedContext,
    AnswerQuestionContextRequest,
    AnswerQuestionContextResolution,
    AnswerQuestionContextService,
    AnswerQuestionDisclosureGate,
)
from spine.application.answer_question_execution import (
    AnswerQuestionExecutionBoundary,
    AnswerQuestionExecutionRequest,
)
from spine.application.answer_outcomes import AnswerOutcomeFacts, resolve_answer_outcome
from spine.application.authorized_ask import (
    AnswerQuestionCapabilityCommand,
    AnswerQuestionCapabilityResult,
)
from spine.application.persistence.context import EnvironmentScope
from spine.domain.capabilities import (
    AnswerAuthorizationOutcome,
    AnswerQuestionInput,
    AnswerQuestionOutcome,
    AnswerQuestionOutput,
    AnswerQuestionSafeReason,
    AnswerQuestionSafeReasonCode,
    AnswerQuestionScope,
    QuestionSubject,
    RetrievalOutcome,
    RunAttemptCorrelation,
)
from spine.domain.capabilities.answer_question_candidate import AnswerQuestionCandidateOutput
from spine.domain.common import ActorKind, ActorRef, EnvironmentKind


@dataclass(frozen=True, slots=True)
class AnswerQuestionContextReferences:
    """References minted by the trusted composition root for one command."""

    trusted_request_context_ref: UUID
    authorization_context_ref: UUID

    def __post_init__(self) -> None:
        if (
            self.trusted_request_context_ref.int == 0
            or self.authorization_context_ref.int == 0
        ):
            raise ValueError("trusted answer context references must be non-zero")


class AnswerQuestionContextReferenceProvider(Protocol):
    """Resolve opaque trusted-context references without deriving authority."""

    def for_command(
        self, command: AnswerQuestionCapabilityCommand
    ) -> AnswerQuestionContextReferences: ...


class AnswerQuestionCandidateRunner(Protocol):
    """Run the candidate through the admitted runtime/artifact contracts."""

    async def generate(
        self,
        *,
        question: AnswerQuestionInput,
        authorized_context: AnswerQuestionAuthorizedContext,
        execution: AnswerQuestionExecutionBoundary,
        execution_request: AnswerQuestionExecutionRequest,
        idempotency_key: str,
    ) -> AnswerQuestionCandidateOutput: ...

    async def cancel(
        self, command: AnswerQuestionCapabilityCommand
    ) -> AnswerQuestionCapabilityResult: ...


class AnswerQuestionCandidateValidator(Protocol):
    """Compose the existing citation, semantic and evaluation validators."""

    async def validate(
        self,
        *,
        question: AnswerQuestionInput,
        authorized_context: AnswerQuestionAuthorizedContext,
        candidate: AnswerQuestionCandidateOutput,
    ) -> AnswerOutcomeFacts: ...


@dataclass(frozen=True, slots=True)
class ProductionAnswerQuestionCapability:
    """Concrete application adapter for the existing capability Protocol."""

    context: AnswerQuestionContextService
    execution: AnswerQuestionExecutionBoundary
    context_references: AnswerQuestionContextReferenceProvider
    disclosure_gate: AnswerQuestionDisclosureGate
    candidate_runner: AnswerQuestionCandidateRunner
    validator: AnswerQuestionCandidateValidator
    disclosure_environment: EnvironmentKind = EnvironmentKind.PRODUCTION

    async def answer(
        self, command: AnswerQuestionCapabilityCommand
    ) -> AnswerQuestionCapabilityResult:
        request = self._request(command)
        try:
            resolved = self.execution.resolve(
                AnswerQuestionExecutionRequest(
                    workspace_id=request.question.scope.workspace_id,
                    environment_id=request.question.scope.environment_id,
                    environment=self.disclosure_environment,
                )
            )
            _, pin = resolved
            context_resolution = await self.context.prepare(
                AnswerQuestionContextRequest(
                    question=request.question,
                    execution_pin=pin,
                    request_id=request.request_id,
                    trusted_request_context_ref=request.references.trusted_request_context_ref,
                    authorization_context_ref=request.references.authorization_context_ref,
                    idempotency_key=request.idempotency_key,
                )
            )
            if context_resolution.output is not None:
                output = context_resolution.output
                self._validate_output(request.question, output)
                return self._result(command, request, output, None)
            authorized = context_resolution.context
            if authorized is None:
                return self._failed(command, request)

            candidate = await self.candidate_runner.generate(
                question=request.question,
                authorized_context=authorized,
                execution=self.execution,
                execution_request=AnswerQuestionExecutionRequest(
                    workspace_id=request.question.scope.workspace_id,
                    environment_id=request.question.scope.environment_id,
                    environment=self.disclosure_environment,
                ),
                idempotency_key=request.idempotency_key,
            )
            candidate = AnswerQuestionCandidateOutput.model_validate(
                candidate.model_dump(mode="python"), strict=True
            )
            if (
                candidate.question != request.question
                or candidate.context_bundle != authorized.bundle.lineage_reference
            ):
                return self._failed(command, request)
            facts = await self.validator.validate(
                question=request.question,
                authorized_context=authorized,
                candidate=candidate,
            )
            if type(facts) is not AnswerOutcomeFacts or facts.candidate != candidate:
                return self._failed(command, request)
            disclosed = await self.context.disclose(
                context=authorized,
                outcome_facts=facts,
                request_id=request.request_id,
                requester_id=command.request_context.subject_id,
                disclosure_gate=self.disclosure_gate,
            )
            output = disclosed.safe_output
            if output is None and disclosed.accepted_answer is not None:
                resolution = resolve_answer_outcome(
                    replace(facts, disclosure_eligible=True)
                )
                output = resolution.output
            if output is None:
                return self._failed(command, request)
            self._validate_output(request.question, output)
            return self._result(command, request, output, disclosed.accepted_answer, disclosed.replay)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Provider, broker, validator and storage diagnostics may contain
            # protected material; callers receive only this typed safe result.
            return self._failed(command, request)

    async def cancel(
        self, command: AnswerQuestionCapabilityCommand
    ) -> AnswerQuestionCapabilityResult:
        if command.run_id is None:
            raise ValueError("answer cancellation requires an admitted run identity")
        try:
            result = await self.candidate_runner.cancel(command)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise RuntimeError("answer cancellation is unavailable") from None
        if (
            type(result) is not AnswerQuestionCapabilityResult
            or result.output.scope.workspace_id != command.request_context.workspace_id
            or result.output.scope.environment_id != command.request_context.environment_id
            or result.output.correlation.run_id != command.run_id
            or result.output.correlation.trace_id != command.request_context.trace_id
        ):
            raise ValueError("answer cancellation returned mismatched identity")
        return result

    def _request(
        self,
        command: AnswerQuestionCapabilityCommand,
    ) -> "_CapabilityRequest":
        command.request_context  # reject malformed lookalikes before accessing data
        context = command.request_context
        persistence = command.persistence_context
        persistence.validate_shape()
        if (
            not isinstance(persistence.scope, EnvironmentScope)
            or persistence.scope.workspace_id != context.workspace_id
            or persistence.scope.environment_id != context.environment_id
            or persistence.acting_subject_id != context.subject_id
            or persistence.trace_id != context.trace_id
        ):
            raise ValueError("trusted persistence context does not match the request")
        if command.session_id is None or command.message_id is None:
            raise ValueError("answer request requires the admitted session and message")
        if command.question is None:
            raise ValueError("answer request requires question text")
        key = command.idempotency_key.value
        stable_id = uuid5(
            NAMESPACE_URL,
            f"spine:answer-question:command:v1:{context.workspace_id}:{context.environment_id}:"
            f"{command.session_id}:{command.message_id}:{key}",
        )
        run_id = command.run_id or uuid5(stable_id, "run")
        attempt_id = uuid5(run_id, "attempt:1")
        command_id = command.command_id or stable_id
        question = AnswerQuestionInput(
            subject=QuestionSubject(text=command.question),
            scope=AnswerQuestionScope(
                workspace_id=context.workspace_id,
                environment_id=context.environment_id,
            ),
            correlation=RunAttemptCorrelation(
                run_id=run_id,
                attempt_id=attempt_id,
                trace_id=context.trace_id,
                correlation_id=context.correlation_id,
                causation_id=context.causation_id,
            ),
        )
        refs = self.context_references.for_command(command)
        return _CapabilityRequest(
            question=question,
            request_id=uuid5(run_id, "context-request"),
            idempotency_key=key,
            command_id=command_id,
            references=refs,
        )

    def _failed(
        self, command: AnswerQuestionCapabilityCommand, request: "_CapabilityRequest"
    ) -> AnswerQuestionCapabilityResult:
        output = AnswerQuestionOutput(
            correlation=request.question.correlation,
            scope=request.question.scope,
            outcome=AnswerQuestionOutcome.FAILED,
            reason=AnswerQuestionSafeReason(
                code=AnswerQuestionSafeReasonCode.MANDATORY_PROCESSING_FAILED
            ),
            lineage=self._run_lineage(request.question.correlation.run_id),
        )
        return self._result(command, request, output, None)

    @staticmethod
    def _validate_output(question: AnswerQuestionInput, output: AnswerQuestionOutput) -> None:
        if (
            output.scope != question.scope
            or output.correlation != question.correlation
        ):
            raise ValueError("answer output identity does not match the admitted command")

    @staticmethod
    def _run_lineage(run_id: UUID):
        from spine.domain.capabilities import LineageReference

        return (
            LineageReference(
                kind="run", identifier=run_id, schema_version="answer-question-run:v1"
            ),
        )

    @staticmethod
    def _result(
        command: AnswerQuestionCapabilityCommand,
        request: "_CapabilityRequest",
        output: AnswerQuestionOutput,
        accepted_answer,
        replay: bool = False,
    ) -> AnswerQuestionCapabilityResult:
        return AnswerQuestionCapabilityResult(
            command_id=request.command_id,
            session_id=command.session_id,  # validated by _request
            message_id=command.message_id,
            output=output,
            accepted_answer=accepted_answer,
            replay=replay,
        )


@dataclass(frozen=True, slots=True)
class _CapabilityRequest:
    question: AnswerQuestionInput
    request_id: UUID
    idempotency_key: str
    command_id: UUID
    references: AnswerQuestionContextReferences


__all__ = [
    "AnswerQuestionCandidateRunner",
    "AnswerQuestionCandidateValidator",
    "AnswerQuestionContextReferenceProvider",
    "AnswerQuestionContextReferences",
    "ProductionAnswerQuestionCapability",
]
