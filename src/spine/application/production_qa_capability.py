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
    AnswerQuestionContextService,
    AnswerQuestionDisclosureGate,
)
from spine.application.answer_question_execution import (
    AnswerQuestionExecutionBoundary,
    AnswerQuestionExecutionRequest,
)
from spine.application.answer_outcomes import AnswerOutcomeFacts, resolve_answer_outcome
from spine.application.citation_validation import (
    CitationValidationReasonCode,
    CitationValidationService,
    RejectedCitation,
)
from spine.application.content_authorization import TrustedAuthorizationContext
from spine.application.authorized_ask import (
    AnswerQuestionCapabilityAvailability,
    AnswerQuestionCapabilityCommand,
    AnswerQuestionCapabilityResult,
    AuthorizedAskServiceUnavailable,
)
from spine.application.persistence.context import EnvironmentScope
from spine.agents.runtime import ArtifactPersistence
from spine.domain.capabilities import (
    AcceptedGroundedAnswer,
    AnswerAuthorizationOutcome,
    AnswerQuestionInput,
    AnswerQuestionOutcome,
    AnswerQuestionOutput,
    AnswerQuestionSafeReason,
    AnswerQuestionSafeReasonCode,
    AnswerQuestionScope,
    LineageReference,
    QuestionSubject,
    RetrievalOutcome,
    RunAttemptCorrelation,
)
from spine.domain.capabilities.answer_question_candidate import (
    AnswerQuestionCandidateInput,
    AnswerQuestionCandidateOutput,
    parse_answer_question_candidate_output,
)
from spine.domain.capabilities.answer_question_execution import (
    AnswerQuestionExecutionPin,
)
from spine.domain.capabilities.answer_evaluation import AnswerEvaluationDecision
from spine.domain.capabilities.semantic_support import (
    SemanticSupportOutcome,
    SemanticSupportRequest,
)
from spine.application.semantic_support import SemanticSupportValidationService
from spine.application.answer_question_candidate import (
    generate_answer_question_candidate,
)
from spine.domain.agents import AgentInvocation, AgentRetrySafety, AgentRunStatus
from spine.domain.artifacts import Artifact, ArtifactState
from spine.domain.common import ActorKind, ActorRef, ContextOrigin, EnvironmentKind


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
        requester_id: UUID,
        authorized_context: AnswerQuestionAuthorizedContext,
        execution: AnswerQuestionExecutionBoundary,
        execution_request: AnswerQuestionExecutionRequest,
        expected_execution_pin: AnswerQuestionExecutionPin,
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


class CandidateInputArtifactWriter(Protocol):
    """Persist an authorized candidate input as an immutable artifact."""

    async def persist_input(
        self,
        *,
        input_value: AnswerQuestionCandidateInput,
        workspace_id: UUID,
        environment_id: UUID,
        work_item_id: UUID,
        idempotency_key: str,
    ) -> UUID: ...


class AnswerQuestionRunCanceller(Protocol):
    """Delegate cancellation to the admitted durable execution owner."""

    async def cancel(
        self, command: AnswerQuestionCapabilityCommand
    ) -> AnswerQuestionCapabilityResult: ...


@dataclass(frozen=True, slots=True)
class PersistedAnswerQuestionCandidateRunner:
    """Run generation through the governed runtime and persisted artifacts."""

    input_artifacts: CandidateInputArtifactWriter
    artifacts: ArtifactPersistence
    canceller: AnswerQuestionRunCanceller

    async def generate(
        self,
        *,
        question: AnswerQuestionInput,
        requester_id: UUID,
        authorized_context: AnswerQuestionAuthorizedContext,
        execution: AnswerQuestionExecutionBoundary,
        execution_request: AnswerQuestionExecutionRequest,
        expected_execution_pin: AnswerQuestionExecutionPin,
        idempotency_key: str,
    ) -> AnswerQuestionCandidateOutput:
        input_id = await self.input_artifacts.persist_input(
            input_value=authorized_context.candidate_input,
            workspace_id=question.scope.workspace_id,
            environment_id=question.scope.environment_id,
            work_item_id=question.correlation.run_id,
            idempotency_key=f"{idempotency_key}:candidate-input",
        )
        if not isinstance(input_id, UUID) or input_id.int == 0:
            raise ValueError("candidate input artifact identity is invalid")
        invocation = AgentInvocation(
            work_item_id=question.correlation.run_id,
            step_run_id=question.correlation.attempt_id,
            capability="answer_question",
            acting_on_behalf_of=ActorRef(kind=ActorKind.HUMAN, id=requester_id),
            input_artifact_ids=(input_id,),
            idempotency_key=f"{idempotency_key}:candidate-generation",
            retry_safety=AgentRetrySafety.SAFE_TO_REPEAT,
        )
        executed = await generate_answer_question_candidate(
            execution, execution_request, invocation
        )
        if executed.pin != expected_execution_pin:
            raise RuntimeError("answer candidate execution pin changed")
        result = executed.result
        if (
            result.status is not AgentRunStatus.SUCCEEDED
            or len(result.output_artifact_ids) != 1
        ):
            raise RuntimeError("answer candidate execution did not complete")
        artifact = await self.artifacts.load(
            result.output_artifact_ids[0],
            workspace_id=question.scope.workspace_id,
            environment_id=question.scope.environment_id,
            work_item_id=question.correlation.run_id,
        )
        if type(artifact) is not Artifact or artifact.state is not ArtifactState.ACTIVE:
            raise RuntimeError("answer candidate artifact is unavailable")
        await self.artifacts.verify_lineage(
            artifact,
            workspace_id=question.scope.workspace_id,
            environment_id=question.scope.environment_id,
            work_item_id=question.correlation.run_id,
        )
        candidate = parse_answer_question_candidate_output(artifact.content)
        if (
            candidate.question != question
            or candidate.context_bundle != authorized_context.bundle.lineage_reference
        ):
            raise ValueError("persisted answer candidate has mismatched identity")
        return candidate

    async def cancel(
        self, command: AnswerQuestionCapabilityCommand
    ) -> AnswerQuestionCapabilityResult:
        return await self.canceller.cancel(command)


@dataclass(frozen=True, slots=True)
class AnswerQuestionValidationPlan:
    """Trusted validator inputs derived from the exact authorized attempt."""

    citation_authorization_context: TrustedAuthorizationContext
    semantic_requests: tuple[SemanticSupportRequest, ...]
    evaluation: AnswerEvaluationDecision | None


class AnswerQuestionValidationPlanProvider(Protocol):
    """Resolve current authorization and semantic inputs without retrieval."""

    def create_plan(
        self,
        *,
        question: AnswerQuestionInput,
        authorized_context: AnswerQuestionAuthorizedContext,
        candidate: AnswerQuestionCandidateOutput,
    ) -> AnswerQuestionValidationPlan: ...


@dataclass(frozen=True, slots=True)
class ExistingAnswerQuestionCandidateValidator:
    """Compose the existing citation and semantic validation services."""

    citations: CitationValidationService
    semantic_support: SemanticSupportValidationService
    plans: AnswerQuestionValidationPlanProvider

    async def validate(
        self,
        *,
        question: AnswerQuestionInput,
        authorized_context: AnswerQuestionAuthorizedContext,
        candidate: AnswerQuestionCandidateOutput,
    ) -> AnswerOutcomeFacts:
        plan = self.plans.create_plan(
            question=question,
            authorized_context=authorized_context,
            candidate=candidate,
        )
        if type(plan) is not AnswerQuestionValidationPlan:
            raise ValueError("answer validation plan is invalid")
        citation_results = tuple(
            [
                await self.citations.validate(
                    citation=citation,
                    expected_context_bundle=candidate.context_bundle,
                    authorization_context=plan.citation_authorization_context,
                )
                for citation in candidate.answer.citations
            ]
        )
        expected_claim_ids = tuple(claim.claim_id for claim in candidate.answer.claims)
        planned_claim_ids = tuple(
            request.claim.claim_id for request in plan.semantic_requests
        )
        if planned_claim_ids != expected_claim_ids:
            raise ValueError("semantic validation plan does not cover every claim")
        semantic_results = tuple(
            [
                await self.semantic_support.validate(request)
                for request in plan.semantic_requests
            ]
        )
        return AnswerOutcomeFacts(
            question=question,
            retrieval_outcome=RetrievalOutcome.EVIDENCE_AVAILABLE,
            authorization=AnswerAuthorizationOutcome.ALLOWED,
            candidate=candidate,
            citation_results=citation_results,
            semantic_results=semantic_results,
            evaluation=plan.evaluation,
        )


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

    def _execution_request(
        self, question: AnswerQuestionInput
    ) -> AnswerQuestionExecutionRequest:
        return AnswerQuestionExecutionRequest(
            workspace_id=question.scope.workspace_id,
            environment_id=question.scope.environment_id,
            environment=self.disclosure_environment,
        )

    async def answer(
        self, command: AnswerQuestionCapabilityCommand
    ) -> AnswerQuestionCapabilityResult:
        request = self._request(command)
        try:
            request = replace(
                request, references=self.context_references.for_command(command)
            )
            execution_request = self._execution_request(request.question)
            _, pin = self.execution.resolve(execution_request)
            if request.references is None:
                return self._failed(command, request)
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
                availability = (
                    AnswerQuestionCapabilityAvailability.UNAVAILABLE
                    if context_resolution.failure is not None
                    else AnswerQuestionCapabilityAvailability.AVAILABLE
                )
                return self._result(
                    command, request, output, None, availability=availability
                )
            authorized = context_resolution.context
            if authorized is None:
                return self._failed(command, request)

            candidate = await self.candidate_runner.generate(
                question=request.question,
                requester_id=command.request_context.subject_id,
                authorized_context=authorized,
                execution=self.execution,
                execution_request=execution_request,
                expected_execution_pin=pin,
                idempotency_key=request.idempotency_key,
            )
            candidate = AnswerQuestionCandidateOutput.model_validate(
                candidate.model_dump(mode="python"), strict=True
            )
            if (
                candidate.question != request.question
                or candidate.context_bundle != authorized.bundle.lineage_reference
            ):
                return self._failed(
                    command,
                    request,
                    availability=AnswerQuestionCapabilityAvailability.AVAILABLE,
                )
            facts = await self.validator.validate(
                question=request.question,
                authorized_context=authorized,
                candidate=candidate,
            )
            if type(facts) is not AnswerOutcomeFacts or facts.candidate != candidate:
                return self._failed(
                    command,
                    request,
                    availability=AnswerQuestionCapabilityAvailability.AVAILABLE,
                )
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
            return self._result(
                command,
                request,
                output,
                disclosed.accepted_answer,
                disclosed.replay,
                availability=(
                    AnswerQuestionCapabilityAvailability.UNAVAILABLE
                    if _validator_unavailable(facts)
                    or output.outcome is AnswerQuestionOutcome.FAILED
                    else AnswerQuestionCapabilityAvailability.AVAILABLE
                ),
            )
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
            raise AuthorizedAskServiceUnavailable() from None
        if (
            type(result) is not AnswerQuestionCapabilityResult
            or result.output.scope.workspace_id != command.request_context.workspace_id
            or result.output.scope.environment_id
            != command.request_context.environment_id
            or result.output.correlation.run_id != command.run_id
            or result.output.correlation.trace_id != command.request_context.trace_id
        ):
            raise ValueError("answer cancellation returned mismatched identity")
        return result

    def _request(
        self,
        command: AnswerQuestionCapabilityCommand,
    ) -> "_CapabilityRequest":
        if type(command) is not AnswerQuestionCapabilityCommand:
            raise TypeError("answer capability command is invalid")
        context = command.request_context
        persistence = command.persistence_context
        persistence.validate_shape()
        if (
            not isinstance(persistence.scope, EnvironmentScope)
            or persistence.scope.workspace_id != context.workspace_id
            or persistence.scope.environment_id != context.environment_id
            or persistence.trace_id != context.trace_id
        ):
            raise ValueError("trusted persistence context does not match the request")
        if persistence.origin is ContextOrigin.INTERACTIVE:
            if persistence.acting_subject_id != context.subject_id:
                raise ValueError(
                    "interactive persistence principal does not match request"
                )
        elif persistence.origin is ContextOrigin.WORKER:
            if (
                persistence.acting_subject_id is not None
                or persistence.service_principal_id is None
            ):
                raise ValueError("worker persistence principal is invalid")
        else:
            raise ValueError("persistence context origin is invalid")
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
        attempt_id = command.attempt_id or uuid5(run_id, "attempt:1")
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
        return _CapabilityRequest(
            question=question,
            request_id=uuid5(run_id, "context-request"),
            idempotency_key=key,
            command_id=command_id,
            references=None,
        )

    def _failed(
        self,
        command: AnswerQuestionCapabilityCommand,
        request: "_CapabilityRequest",
        *,
        availability: AnswerQuestionCapabilityAvailability = (
            AnswerQuestionCapabilityAvailability.UNAVAILABLE
        ),
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
        return self._result(command, request, output, None, availability=availability)

    @staticmethod
    def _validate_output(
        question: AnswerQuestionInput, output: AnswerQuestionOutput
    ) -> None:
        if output.scope != question.scope or output.correlation != question.correlation:
            raise ValueError(
                "answer output identity does not match the admitted command"
            )

    @staticmethod
    def _run_lineage(run_id: UUID) -> tuple[LineageReference, ...]:
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
        accepted_answer: AcceptedGroundedAnswer | None,
        replay: bool = False,
        *,
        availability: AnswerQuestionCapabilityAvailability = (
            AnswerQuestionCapabilityAvailability.AVAILABLE
        ),
    ) -> AnswerQuestionCapabilityResult:
        return AnswerQuestionCapabilityResult(
            command_id=request.command_id,
            session_id=command.session_id,  # validated by _request
            message_id=command.message_id,
            output=output,
            accepted_answer=accepted_answer,
            replay=replay,
            availability=availability,
        )


@dataclass(frozen=True, slots=True)
class _CapabilityRequest:
    question: AnswerQuestionInput
    request_id: UUID
    idempotency_key: str
    command_id: UUID
    references: AnswerQuestionContextReferences | None


def _validator_unavailable(facts: AnswerOutcomeFacts) -> bool:
    return any(
        isinstance(result, RejectedCitation)
        and result.reason_code is CitationValidationReasonCode.VALIDATION_UNAVAILABLE
        for result in facts.citation_results
    ) or any(
        result.outcome is SemanticSupportOutcome.VALIDATOR_UNAVAILABLE
        for result in facts.semantic_results
    )


__all__ = [
    "AnswerQuestionCandidateRunner",
    "AnswerQuestionCandidateValidator",
    "AnswerQuestionContextReferenceProvider",
    "AnswerQuestionContextReferences",
    "AnswerQuestionRunCanceller",
    "AnswerQuestionValidationPlan",
    "AnswerQuestionValidationPlanProvider",
    "CandidateInputArtifactWriter",
    "ExistingAnswerQuestionCandidateValidator",
    "PersistedAnswerQuestionCandidateRunner",
    "ProductionAnswerQuestionCapability",
]
