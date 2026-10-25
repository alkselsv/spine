"""Production composition for the authorized ``answer_question`` capability.

This adapter owns request identity and stage ordering. Retrieval, candidate
execution, validation, persistence and disclosure remain behind their existing
application contracts so that the composition can be exercised with safe fakes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import logging
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5


_LOGGER = logging.getLogger(__name__)

from spine.application.answer_question_context import (
    AnswerQuestionAuthorizedContext,
    AnswerQuestionContextRequest,
    AnswerQuestionContextService,
    AnswerQuestionDisclosureGate,
)
from spine.application.answer_question_execution import (
    AnswerQuestionExecutionBoundary,
    AnswerQuestionExecutionIntent,
    AnswerQuestionExecutionRequest,
)
from spine.application.answer_outcomes import AnswerOutcomeFacts, resolve_answer_outcome
from spine.application.citation_validation import (
    CitationValidationOutcome,
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
from spine.application.qa_observation.contracts import (
    AdmittedExecutionIdentity,
    SameExecutionCapture,
)
from spine.application.qa_observation.context_capture import (
    AuthorizedContextCaptureContext,
)
from spine.application.persistence.answer_question_runs import CanonicalQATerminalizationReceipt
from spine.agents.runtime import ArtifactPersistence
from spine.domain.capabilities import (
    AcceptedAnswerPresentation,
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
from spine.domain.capabilities.answer_disclosure import (
    AnswerDisclosureRecord,
    AnswerExecutionCheckpoint,
)
from spine.domain.capabilities.answer_question_candidate import (
    AnswerQuestionCandidateInput,
    AnswerQuestionCandidateOutput,
    parse_answer_question_candidate_output,
)
from spine.domain.capabilities.answer_question_execution import (
    AnswerQuestionExecutionPin,
)
from spine.domain.capabilities.answer_question_runs import AgentResultRef
from spine.domain.capabilities.answer_evaluation import (
    AnswerEvaluationDecision,
    GroundedConflict,
    GroundedClaimAssessment,
    ObservedCoverage,
    PartialFallbackAuthorization,
    TargetCompletenessScope,
)
from spine.domain.capabilities.semantic_support import (
    SemanticSupportOutcome,
    SemanticSupportRequest,
    SemanticSupportResult,
)
from spine.application.semantic_support import SemanticSupportValidationService
from spine.application.answer_question_candidate import (
    generate_answer_question_candidate,
)
from spine.domain.agents import AgentInvocation, AgentRetrySafety, AgentRunStatus, AgentResult
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

    async def generate_persisted(
        self,
        *,
        question: AnswerQuestionInput,
        requester_id: UUID,
        authorized_context: AnswerQuestionAuthorizedContext,
        execution: AnswerQuestionExecutionBoundary,
        execution_request: AnswerQuestionExecutionRequest,
        expected_execution_pin: AnswerQuestionExecutionPin,
        idempotency_key: str,
    ) -> "PersistedCandidateExecution": ...

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


@dataclass(frozen=True, slots=True)
class PersistedCandidateExecution:
    """Actual candidate and persisted runtime/artifact lineage from one run."""

    candidate: AnswerQuestionCandidateOutput
    candidate_input_artifact_id: UUID
    agent_result_ref: AgentResultRef
    agent_result: AgentResult
    output_artifacts: tuple[Artifact, ...]


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
        return (await self._generate_persisted(
            question=question,
            requester_id=requester_id,
            authorized_context=authorized_context,
            execution=execution,
            execution_request=execution_request,
            expected_execution_pin=expected_execution_pin,
            idempotency_key=idempotency_key,
            require_persisted_reference=False,
        )).candidate

    async def generate_persisted(
        self,
        *,
        question: AnswerQuestionInput,
        requester_id: UUID,
        authorized_context: AnswerQuestionAuthorizedContext,
        execution: AnswerQuestionExecutionBoundary,
        execution_request: AnswerQuestionExecutionRequest,
        expected_execution_pin: AnswerQuestionExecutionPin,
        idempotency_key: str,
    ) -> PersistedCandidateExecution:
        return await self._generate_persisted(
            question=question,
            requester_id=requester_id,
            authorized_context=authorized_context,
            execution=execution,
            execution_request=execution_request,
            expected_execution_pin=expected_execution_pin,
            idempotency_key=idempotency_key,
            require_persisted_reference=True,
        )

    async def _generate_persisted(
        self,
        *,
        question: AnswerQuestionInput,
        requester_id: UUID,
        authorized_context: AnswerQuestionAuthorizedContext,
        execution: AnswerQuestionExecutionBoundary,
        execution_request: AnswerQuestionExecutionRequest,
        expected_execution_pin: AnswerQuestionExecutionPin,
        idempotency_key: str,
        require_persisted_reference: bool,
    ) -> PersistedCandidateExecution:
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
        if executed.agent_result_ref is None and require_persisted_reference:
            raise RuntimeError("persisted AgentResult reference is unavailable")
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
        return PersistedCandidateExecution(
            candidate=candidate,
            candidate_input_artifact_id=input_id,
            agent_result_ref=executed.agent_result_ref,
            agent_result=result,
            output_artifacts=(artifact,),
        )

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


@dataclass(frozen=True, slots=True)
class AnswerQuestionEvaluationLineage:
    """Immutable evaluator inputs and decision for one exact attempt."""

    target_scope: TargetCompletenessScope
    observed_coverage: ObservedCoverage
    partial_fallback: PartialFallbackAuthorization | None
    claim_assessments: tuple[GroundedClaimAssessment, ...]
    conflicts: tuple[GroundedConflict, ...]
    decision: AnswerEvaluationDecision


class AnswerQuestionValidationPlanProvider(Protocol):
    """Resolve current authorization and semantic inputs without retrieval."""

    def create_plan(
        self,
        *,
        question: AnswerQuestionInput,
        authorized_context: AnswerQuestionAuthorizedContext,
        candidate: AnswerQuestionCandidateOutput,
    ) -> AnswerQuestionValidationPlan: ...

    async def evaluate(
        self,
        *,
        question: AnswerQuestionInput,
        authorized_context: AnswerQuestionAuthorizedContext,
        candidate: AnswerQuestionCandidateOutput,
        citation_results: tuple[CitationValidationOutcome, ...],
        semantic_results: tuple[SemanticSupportResult, ...],
    ) -> AnswerQuestionEvaluationLineage: ...


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
        evaluation_lineage = None
        evaluate = getattr(self.plans, "evaluate", None)
        if callable(evaluate):
            evaluation_lineage = await evaluate(
                question=question,
                authorized_context=authorized_context,
                candidate=candidate,
                citation_results=citation_results,
                semantic_results=semantic_results,
            )
            if type(evaluation_lineage) is not AnswerQuestionEvaluationLineage:
                raise ValueError("answer evaluation lineage is invalid")
        return AnswerOutcomeFacts(
            question=question,
            retrieval_outcome=RetrievalOutcome.EVIDENCE_AVAILABLE,
            authorization=AnswerAuthorizationOutcome.ALLOWED,
            candidate=candidate,
            citation_results=citation_results,
            semantic_results=semantic_results,
            evaluation=(
                plan.evaluation
                if evaluation_lineage is None
                else evaluation_lineage.decision
            ),
            target_scope=(
                None if evaluation_lineage is None else evaluation_lineage.target_scope
            ),
            observed_coverage=(
                None
                if evaluation_lineage is None
                else evaluation_lineage.observed_coverage
            ),
            partial_fallback=(
                None
                if evaluation_lineage is None
                else evaluation_lineage.partial_fallback
            ),
            claim_assessments=(
                ()
                if evaluation_lineage is None
                else evaluation_lineage.claim_assessments
            ),
            conflicts=(
                () if evaluation_lineage is None else evaluation_lineage.conflicts
            ),
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
        self, question: AnswerQuestionInput, allow_partial_answer: bool | None
    ) -> AnswerQuestionExecutionRequest:
        return AnswerQuestionExecutionRequest(
            workspace_id=question.scope.workspace_id,
            environment_id=question.scope.environment_id,
            environment=self.disclosure_environment,
            intent=AnswerQuestionExecutionIntent(
                allow_partial_answer=allow_partial_answer
            ),
        )

    async def answer(
        self, command: AnswerQuestionCapabilityCommand
    ) -> AnswerQuestionCapabilityResult:
        request = self._request(command)
        safe_stage = "configuration"
        try:
            request = replace(
                request, references=self.context_references.for_command(command)
            )
            capture_context = None
            if command.capture_subscription is not None:
                if (
                    command.command_id is None
                    or command.run_id is None
                    or command.attempt_id is None
                    or command.attempt_number is None
                    or command.session_id is None
                    or command.message_id is None
                ):
                    raise ValueError("observer capture lacks canonical attempt identity")
                capture_identity = AdmittedExecutionIdentity(
                    command_id=command.command_id,
                    run_id=command.run_id,
                    session_id=command.session_id,
                    message_id=command.message_id,
                    workspace_id=command.request_context.workspace_id,
                    environment_id=command.request_context.environment_id,
                    requester=ActorRef(
                        kind=ActorKind.HUMAN,
                        id=command.request_context.subject_id,
                    ),
                    attempt_id=command.attempt_id,
                    attempt_number=command.attempt_number,
                )
                capture_context = AuthorizedContextCaptureContext(
                    identity=capture_identity,
                    subscription=command.capture_subscription,
                    question=request.question,
                )
            execution_request = self._execution_request(
                request.question, request.allow_partial_answer
            )
            _, pin = self.execution.resolve(execution_request)
            if command.execution_pin is not None and command.execution_pin != pin:
                # The allocated pin is canonical. Do not retrieve under a newly
                # resolved configuration when the runtime cannot honor it.
                return self._failed(command, request)
            if command.execution_pin is not None:
                pin = command.execution_pin
            if request.references is None:
                return self._failed(command, request)
            safe_stage = "context"
            context_resolution = await self.context.prepare(
                AnswerQuestionContextRequest(
                    question=request.question,
                    execution_pin=pin,
                    request_id=request.request_id,
                    trusted_request_context_ref=request.references.trusted_request_context_ref,
                    authorization_context_ref=request.references.authorization_context_ref,
                    idempotency_key=request.idempotency_key,
                    allow_partial_answer=request.allow_partial_answer,
                    cancelled=command.cancelled,
                    capture_context=capture_context,
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
                if availability is AnswerQuestionCapabilityAvailability.UNAVAILABLE:
                    _LOGGER.warning(
                        "governed Q&A context unavailable: %s",
                        (
                            "unknown"
                            if context_resolution.failure is None
                            else context_resolution.failure.value
                        ),
                    )
                    return self._result(
                        command, request, output, None, availability=availability
                    )
                receipt = None
                if command.attempt_id is not None:
                    disclosed = await self.context.prepare_terminal_disclosure(
                        question=request.question,
                        retrieval_outcome=context_resolution.retrieval_outcome,
                        authorization=context_resolution.authorization,
                        request_id=request.request_id,
                        disclosure_gate=self.disclosure_gate,
                        cancelled=output.outcome is AnswerQuestionOutcome.CANCELLED,
                        execution_checkpoint=AnswerExecutionCheckpoint(
                            execution_pin_digest=pin.pin_digest,
                        ),
                    )
                    persisted_output = disclosed.record.output
                    if persisted_output is None:
                        return self._failed(command, request)
                    self._validate_output(request.question, persisted_output)
                    if persisted_output.outcome is not output.outcome:
                        return self._failed(command, request)
                    output = persisted_output
                    receipt = CanonicalQATerminalizationReceipt(
                        run_id=command.run_id,
                        attempt_id=command.attempt_id,
                        attempt_number=command.attempt_number,
                        execution_pin=pin,
                        output=output,
                        context_bundle_ref=None,
                        agent_result_ref=None,
                        agent_result=None,
                        output_artifact_ids=(),
                        candidate_input_artifact_id=None,
                        candidate_output_artifacts=(),
                        disclosure_record=disclosed.record,
                        authorization=context_resolution.authorization,
                    )
                return self._result(
                    command,
                    request,
                    output,
                    None,
                    availability=availability,
                    terminalization_receipt=receipt,
                )
            authorized = context_resolution.context
            if authorized is None:
                return self._failed(command, request)

            safe_stage = "candidate_generation"
            persisted_candidate = None
            generate_persisted = getattr(self.candidate_runner, "generate_persisted", None)
            if command.attempt_id is not None and callable(generate_persisted):
                persisted_candidate = await generate_persisted(
                    question=request.question,
                    requester_id=command.request_context.subject_id,
                    authorized_context=authorized,
                    execution=self.execution,
                    execution_request=execution_request,
                    expected_execution_pin=pin,
                    idempotency_key=request.idempotency_key,
                )
                if type(persisted_candidate) is not PersistedCandidateExecution:
                    raise ValueError("persisted candidate execution is invalid")
                candidate = persisted_candidate.candidate
            else:
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
                if command.attempt_id is not None:
                    return await self._persist_validation_failure(
                        command, request, authorized, pin, persisted_candidate
                    )
                return self._failed(
                    command,
                    request,
                    availability=AnswerQuestionCapabilityAvailability.AVAILABLE,
                )
            safe_stage = "candidate_validation"
            facts = await self.validator.validate(
                question=request.question,
                authorized_context=authorized,
                candidate=candidate,
            )
            if type(facts) is not AnswerOutcomeFacts or facts.candidate != candidate:
                if command.attempt_id is not None:
                    return await self._persist_validation_failure(
                        command, request, authorized, pin, persisted_candidate
                    )
                return self._failed(
                    command,
                    request,
                    availability=AnswerQuestionCapabilityAvailability.AVAILABLE,
                )
            if command.attempt_id is not None and persisted_candidate is None:
                # Durable completion requires the actual runtime-owned result
                # reference and artifact lineage; a transient candidate cannot
                # be converted into a canonical answered receipt.
                return self._failed(
                    command,
                    request,
                    availability=AnswerQuestionCapabilityAvailability.UNAVAILABLE,
                )
            safe_stage = "disclosure"
            disclosed = await self.context.disclose(
                context=authorized,
                outcome_facts=facts,
                request_id=request.request_id,
                requester_id=command.request_context.subject_id,
                disclosure_gate=self.disclosure_gate,
                defer_delivery=command.attempt_id is not None,
                execution_checkpoint=(
                    AnswerExecutionCheckpoint(
                        execution_pin_digest=pin.pin_digest,
                        context_bundle_ref=authorized.bundle.reference,
                        candidate_input_artifact_id=(
                            None
                            if persisted_candidate is None
                            else persisted_candidate.candidate_input_artifact_id
                        ),
                        agent_result_ref=(
                            None
                            if persisted_candidate is None
                            else persisted_candidate.agent_result_ref
                        ),
                        output_artifact_ids=(
                            ()
                            if persisted_candidate is None
                            else persisted_candidate.agent_result.output_artifact_ids
                        ),
                    )
                    if command.attempt_id is not None
                    else None
                ),
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
            same_execution_capture = None
            if context_resolution.broker_capture is not None:
                try:
                    captured = context_resolution.broker_capture
                    same_execution_capture = SameExecutionCapture(
                        question=request.question,
                        context_response=captured.response,
                        context_bundle=captured.context_bundle,
                        projection_retrieval=captured.projection_retrieval,
                        retrieval=captured.retrieval,
                        answer=output,
                        disclosure=disclosed.record,
                    )
                except Exception:
                    # Capture validation is an independent observer concern.
                    # The canonical answer remains governed by the disclosure
                    # and terminalization contracts.
                    same_execution_capture = None
            receipt = None
            if command.attempt_id is not None and persisted_candidate is not None:
                receipt = CanonicalQATerminalizationReceipt(
                    run_id=command.run_id,
                    attempt_id=command.attempt_id,
                    attempt_number=command.attempt_number,
                    execution_pin=pin,
                    output=output,
                    context_bundle_ref=authorized.bundle.reference,
                    agent_result_ref=persisted_candidate.agent_result_ref,
                    agent_result=persisted_candidate.agent_result,
                    output_artifact_ids=persisted_candidate.agent_result.output_artifact_ids,
                    candidate_input_artifact_id=persisted_candidate.candidate_input_artifact_id,
                    candidate_output_artifacts=persisted_candidate.output_artifacts,
                    disclosure_record=disclosed.record,
                    authorization=facts.authorization,
                    disclosure_dependencies=authorized.disclosure_dependencies,
                )
            return self._result(
                command,
                request,
                output,
                disclosed.accepted_answer,
                disclosed.replay,
                availability=(
                    AnswerQuestionCapabilityAvailability.UNAVAILABLE
                    if _validator_unavailable(facts)
                    else AnswerQuestionCapabilityAvailability.AVAILABLE
                ),
                terminalization_receipt=receipt,
                same_execution_capture=same_execution_capture,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Provider, broker, validator and storage diagnostics may contain
            # protected material; callers receive only this typed safe result.
            _LOGGER.warning(
                "governed Q&A capability unavailable at %s: %s",
                safe_stage,
                type(error).__name__,
            )
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

    async def _persist_validation_failure(
        self,
        command: AnswerQuestionCapabilityCommand,
        request: "_CapabilityRequest",
        authorized: AnswerQuestionAuthorizedContext,
        pin: AnswerQuestionExecutionPin,
        persisted_candidate: PersistedCandidateExecution | None,
    ) -> AnswerQuestionCapabilityResult:
        """Persist a safe terminal failure for deterministic output mismatches."""

        if command.attempt_id is None or command.run_id is None or command.attempt_number is None:
            raise ValueError("durable validation failure lacks canonical attempt identity")
        from spine.application.answer_question_context import (
            map_context_outcome_to_retrieval_outcome,
        )

        failed = self._failed(
            command,
            request,
            availability=AnswerQuestionCapabilityAvailability.AVAILABLE,
        )
        checkpoint = AnswerExecutionCheckpoint(
            execution_pin_digest=pin.pin_digest,
            context_bundle_ref=authorized.bundle.reference,
            candidate_input_artifact_id=(
                None
                if persisted_candidate is None
                else persisted_candidate.candidate_input_artifact_id
            ),
            agent_result_ref=(
                None
                if persisted_candidate is None
                else persisted_candidate.agent_result_ref
            ),
            output_artifact_ids=(
                ()
                if persisted_candidate is None
                else persisted_candidate.agent_result.output_artifact_ids
            ),
        )
        disclosed = await self.context.disclose(
            context=authorized,
            outcome_facts=AnswerOutcomeFacts(
                question=request.question,
                retrieval_outcome=map_context_outcome_to_retrieval_outcome(
                    authorized.response.outcome
                ),
                authorization=AnswerAuthorizationOutcome.ALLOWED,
            ),
            request_id=request.request_id,
            requester_id=command.request_context.subject_id,
            disclosure_gate=self.disclosure_gate,
            defer_delivery=True,
            execution_checkpoint=checkpoint,
            safe_terminal_output=failed.output,
        )
        output = disclosed.record.output
        if output != failed.output:
            raise ValueError("persisted safe validation failure changed identity")
        receipt = CanonicalQATerminalizationReceipt(
            run_id=command.run_id,
            attempt_id=command.attempt_id,
            attempt_number=command.attempt_number,
            execution_pin=pin,
            output=output,
            context_bundle_ref=authorized.bundle.reference,
            agent_result_ref=(
                None
                if persisted_candidate is None
                else persisted_candidate.agent_result_ref
            ),
            agent_result=(
                None if persisted_candidate is None else persisted_candidate.agent_result
            ),
            output_artifact_ids=(
                ()
                if persisted_candidate is None
                else persisted_candidate.agent_result.output_artifact_ids
            ),
            candidate_input_artifact_id=(
                None
                if persisted_candidate is None
                else persisted_candidate.candidate_input_artifact_id
            ),
            candidate_output_artifacts=(
                ()
                if persisted_candidate is None
                else persisted_candidate.output_artifacts
            ),
            disclosure_record=disclosed.record,
            authorization=AnswerAuthorizationOutcome.ALLOWED,
            disclosure_dependencies=authorized.disclosure_dependencies,
        )
        return self._result(
            command,
            request,
            output,
            None,
            disclosed.replay,
            availability=AnswerQuestionCapabilityAvailability.AVAILABLE,
            terminalization_receipt=receipt,
        )

    async def deliver_committed(
        self,
        record: AnswerDisclosureRecord,
        presentation: AcceptedAnswerPresentation,
    ) -> None:
        """Perform accepted-answer output only after canonical terminal commit."""

        delivery = getattr(self.disclosure_gate, "deliver_committed", None)
        if not callable(delivery):
            raise RuntimeError("committed disclosure delivery is unavailable")
        await delivery(record, presentation)

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
        if command.attempt_id is not None and (
            command.run_id is None or command.attempt_number is None
        ):
            raise ValueError("durable answer request lacks canonical attempt identity")
        if command.capture_subscription is not None and (
            command.attempt_id is None
            or command.run_id is None
            or command.attempt_number is None
            or command.command_id is None
        ):
            raise ValueError("observer capture requires canonical attempt identity")
        key = command.idempotency_key.value
        stable_id = uuid5(
            NAMESPACE_URL,
            f"spine:answer-question:command:v1:{context.workspace_id}:{context.environment_id}:"
            f"{command.session_id}:{command.message_id}:{key}",
        )
        run_id = command.run_id or uuid5(stable_id, "run")
        attempt_id = command.attempt_id or uuid5(
            stable_id,
            f"legacy-ephemeral-attempt:v1:{command.session_id}:{command.message_id}:{key}",
        )
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
            allow_partial_answer=command.allow_partial_answer,
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
        terminalization_receipt: CanonicalQATerminalizationReceipt | None = None,
        same_execution_capture: SameExecutionCapture | None = None,
    ) -> AnswerQuestionCapabilityResult:
        return AnswerQuestionCapabilityResult(
            command_id=request.command_id,
            session_id=command.session_id,  # validated by _request
            message_id=command.message_id,
            output=output,
            accepted_answer=accepted_answer,
            terminalization_receipt=terminalization_receipt,
            same_execution_capture=same_execution_capture,
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
    allow_partial_answer: bool | None


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
    "AnswerQuestionEvaluationLineage",
    "AnswerQuestionValidationPlanProvider",
    "CandidateInputArtifactWriter",
    "ExistingAnswerQuestionCandidateValidator",
    "PersistedAnswerQuestionCandidateRunner",
    "ProductionAnswerQuestionCapability",
]
