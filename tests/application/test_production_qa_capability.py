from __future__ import annotations

from types import SimpleNamespace
from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID, uuid5

import pytest

from spine.application.authorized_ask import (
    AnswerQuestionCapabilityAvailability,
    AnswerQuestionCapabilityCommand,
    AnswerQuestionCapabilityResult,
    AskRequestContext,
)
from spine.application.persistence.context import (
    ContextOrigin,
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.answer_question_context import AnswerQuestionContextResolution
from spine.application.answer_outcomes import resolve_answer_outcome
from spine.application.answer_disclosure import (
    AnswerDisclosureGate,
    InMemoryAcceptedAnswerOutputPort,
    InMemoryAnswerDisclosureRepository,
)
from spine.application.answer_question_context import AnswerQuestionContextService
from spine.application.authorized_context_broker import (
    InMemoryContextBundleStore,
    SavedContextBundleAccess,
)
from spine.application.citation_validation import CitationValidationFacts, ValidCitation
from spine.application.answer_question_execution import (
    AnswerQuestionExecutionPin,
    AnswerQuestionExecutionRequest,
)
from spine.application.production_qa_capability import (
    AnswerQuestionValidationPlan,
    AnswerQuestionContextReferences,
    ExistingAnswerQuestionCandidateValidator,
    PersistedAnswerQuestionCandidateRunner,
    ProductionAnswerQuestionCapability,
)
from spine.application.answer_question_execution import AnswerQuestionExecutionResult
from spine.domain.agents import AgentResult, AgentRunStatus
from spine.domain.artifacts import Artifact
from spine.domain.capabilities.answer_question_candidate import (
    AnswerQuestionCandidateInput,
    AnswerQuestionCandidateOutput,
)
from spine.domain.common import ActorKind, ActorRef, SchemaRef
from spine.domain.capabilities.answer_evaluation import (
    AnswerEvaluationDecision,
    AnswerEvaluationSafeReason,
    AttemptProjectionBoundary,
)
from spine.domain.capabilities import (
    AcceptedGroundedAnswer,
    AnswerAuthorizationOutcome,
    AnswerQuestionOutput,
    AnswerQuestionOutcome,
    AnswerQuestionSafeReason,
    AnswerQuestionSafeReasonCode,
    QuestionSubject,
    RetrievalOutcome,
)
from spine.domain.capabilities.answer_disclosure import AnswerDisclosureValidation
from spine.domain.common import EnvironmentKind
from spine.domain.context_bundles import ContextBundle
from spine.domain.disclosure import (
    DisclosureRevalidationDisposition,
    DisclosureRevalidationResult,
    DisclosureValidationFact,
)
from spine.memory.contracts import ContextOutcome, ContextReasonCode, ContextResponse
from spine.domain.capabilities.semantic_support import (
    SemanticSupportEvidence,
    SemanticSupportOutcome,
    SemanticSupportPolicy,
    SemanticSupportReasonCode,
    SemanticSupportRequest,
    SemanticSupportResult,
)
from spine.domain.context_bundles import ContextBundle
from tests.application.test_answer_question_execution import configuration
from tests.application.test_answer_question_context import (
    Broker as ContextBroker,
    Revalidator as ContextRevalidator,
    pin_for,
)
from tests.domain.context_bundle.test_context_bundle_models import (
    context_bundle as base_context_bundle,
)
from tests.domain.capabilities.test_answer_question_candidate import (
    candidate_input,
    candidate_output,
    question as candidate_question,
)

WORKSPACE = UUID(int=1)
ENVIRONMENT = UUID(int=2)
SUBJECT = UUID(int=3)
TRACE = UUID(int=4)
SESSION = UUID(int=5)
MESSAGE = UUID(int=6)
RUN = UUID(int=7)
COMMAND_ID = UUID(int=8)


def command(
    *,
    run_id: UUID | None = RUN,
    worker: bool = False,
    attempt_id: UUID | None = None,
    attempt_number: int | None = None,
) -> AnswerQuestionCapabilityCommand:
    return AnswerQuestionCapabilityCommand(
        request_context=AskRequestContext(
            subject_id=SUBJECT,
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            trace_id=TRACE,
            authorization_generation=1,
        ),
        persistence_context=TrustedPersistenceContext(
            scope=EnvironmentScope(
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
            ),
            origin=ContextOrigin.WORKER if worker else ContextOrigin.INTERACTIVE,
            purpose=PersistencePurpose("knowledge_control"),
            operation=PersistenceOperation("answer_question"),
            trace_id=TRACE,
            acting_subject_id=None if worker else SUBJECT,
            service_principal_id=UUID(int=12) if worker else None,
            provenance=TrustedContextProvenance(
                issuer_id=UUID(int=9), signature=b"synthetic-trusted-context"
            ),
        ),
        session_id=SESSION,
        message_id=MESSAGE,
        question="Where is the policy?",
        run_id=run_id,
        idempotency_key=IdempotencyKey("admitted-key"),
        command_id=COMMAND_ID,
        attempt_id=attempt_id,
        attempt_number=attempt_number,
    )


class References:
    def for_command(self, _: AnswerQuestionCapabilityCommand):
        return AnswerQuestionContextReferences(
            trusted_request_context_ref=UUID(int=10),
            authorization_context_ref=UUID(int=11),
        )


class UnavailableExecution:
    def resolve(self, _):
        raise RuntimeError("provider response contains protected text")


class BoundExecution:
    def resolve(self, request):
        assert request.workspace_id == WORKSPACE
        assert request.environment_id == ENVIRONMENT
        pin = AnswerQuestionExecutionPin.create(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            environment=EnvironmentKind.DEVELOPMENT,
            configuration=configuration(),
        )
        return None, pin


class TerminalContext:
    def __init__(self, outcome: AnswerQuestionOutcome) -> None:
        self.outcome = outcome
        self.prepared = 0

    async def prepare(self, request):
        self.prepared += 1
        reason = {
            AnswerQuestionOutcome.ABSTAINED: AnswerQuestionSafeReasonCode.NO_USABLE_EVIDENCE,
            AnswerQuestionOutcome.DENIED: AnswerQuestionSafeReasonCode.AUTHORIZATION_DENIED,
            AnswerQuestionOutcome.FAILED: AnswerQuestionSafeReasonCode.MANDATORY_PROCESSING_FAILED,
            AnswerQuestionOutcome.CANCELLED: AnswerQuestionSafeReasonCode.CANCELLED,
        }[self.outcome]
        output = AnswerQuestionOutput(
            correlation=request.question.correlation,
            scope=request.question.scope,
            outcome=self.outcome,
            reason=AnswerQuestionSafeReason(code=reason),
        )
        return AnswerQuestionContextResolution(
            retrieval_outcome=RetrievalOutcome.UNAVAILABLE,
            authorization=(
                AnswerAuthorizationOutcome.DENIED
                if self.outcome is AnswerQuestionOutcome.DENIED
                else AnswerAuthorizationOutcome.ALLOWED
            ),
            output=output,
        )


class UnexpectedCandidateStage:
    async def generate(self, **_):
        raise AssertionError("terminal Context Broker output must stop generation")

    async def cancel(self, _):
        raise AssertionError("cancellation is not part of this test")


class PersistedInput:
    async def persist_input(self, **kwargs):
        assert isinstance(kwargs["input_value"], AnswerQuestionCandidateInput)
        return UUID(int=20)


class PersistedOutput:
    def __init__(self, artifact: Artifact) -> None:
        self.artifact = artifact

    async def load(self, artifact_id, **_):
        assert artifact_id == self.artifact.id
        return self.artifact

    async def verify_lineage(self, *_args, **_kwargs):
        return None


class CandidateExecution:
    def __init__(self, result: AnswerQuestionExecutionResult) -> None:
        self.result = result
        self.invocations = []

    async def execute(self, _request, invocation):
        self.invocations.append(invocation)
        return self.result


class UnexpectedCanceller:
    async def cancel(self, _):
        raise AssertionError("cancellation is not part of this test")


def _candidate_for_question(question, lineage=None):
    original = candidate_output()
    correlation = question.correlation
    if lineage is None:
        bundle_values = candidate_input().context_bundle.model_dump(mode="python")
        bundle_values.update(
            {"run_id": correlation.run_id, "attempt_id": correlation.attempt_id}
        )
        bundle = ContextBundle.create(**bundle_values)
        lineage = bundle.lineage_reference
    values = original.model_dump(mode="python")
    values["question"] = question.model_dump(mode="python")
    values["context_bundle"] = lineage.model_dump(mode="python")
    answer = values["answer"]
    answer.update(
        {
            "workspace_id": question.scope.workspace_id,
            "environment_id": question.scope.environment_id,
            "run_id": correlation.run_id,
            "attempt_id": correlation.attempt_id,
        }
    )
    for claim in answer["claims"]:
        claim.update(
            {
                "workspace_id": question.scope.workspace_id,
                "environment_id": question.scope.environment_id,
                "run_id": correlation.run_id,
                "attempt_id": correlation.attempt_id,
            }
        )
    for relationship in answer["relationships"]:
        relationship.update(
            {
                "workspace_id": question.scope.workspace_id,
                "environment_id": question.scope.environment_id,
                "run_id": correlation.run_id,
                "attempt_id": correlation.attempt_id,
            }
        )
    for citation in answer["citations"]:
        citation["context_bundle"] = lineage.model_dump(mode="python")
    return AnswerQuestionCandidateOutput.model_validate(values)


class SuccessfulContext:
    def __init__(self) -> None:
        self.disclosures = {}
        self.disclosure_error = None
        self.requests = []

    async def prepare(self, request):
        self.requests.append(request)
        bundle = _candidate_for_question(request.question).context_bundle
        authorized_context = SimpleNamespace(
            question=request.question,
            bundle=SimpleNamespace(lineage_reference=bundle),
        )
        return AnswerQuestionContextResolution(
            retrieval_outcome=RetrievalOutcome.EVIDENCE_AVAILABLE,
            authorization=AnswerAuthorizationOutcome.ALLOWED,
            context=authorized_context,
        )

    async def disclose(
        self,
        *,
        context,
        outcome_facts,
        request_id,
        requester_id,
        disclosure_gate,
    ):
        del context, requester_id, disclosure_gate
        try:
            resolution = resolve_answer_outcome(
                replace(outcome_facts, disclosure_eligible=True)
            )
        except Exception as error:
            self.disclosure_error = error
            raise
        self.disclosures[request_id] = resolution
        answer = resolution.output.answer
        bundle = outcome_facts.candidate.context_bundle.bundle
        accepted_answer = AcceptedGroundedAnswer(
            answer=answer,
            validation=AnswerDisclosureValidation(
                citation_validation_reference=UUID(int=37),
                semantic_support_validation_reference=UUID(int=38),
                evaluation_lineage_reference=UUID(int=36),
                disclosure_eligibility_reference=UUID(int=39),
                context_bundle_id=bundle.bundle_id,
                context_bundle_version=bundle.bundle_version,
                context_bundle_digest=bundle.bundle_digest,
                claim_ids=tuple(claim.claim_id for claim in answer.claims),
                citation_ids=tuple(
                    citation.citation_id for citation in answer.citations
                ),
            ),
            accepted_at=datetime(2026, 10, 18, tzinfo=timezone.utc),
        )
        return SimpleNamespace(
            safe_output=resolution.output,
            accepted_answer=accepted_answer,
            replay=False,
        )


class SuccessfulCandidateRunner(UnexpectedCandidateStage):
    def __init__(self) -> None:
        self.generated = False
        self.requests = []

    async def generate(self, *, question, authorized_context, idempotency_key, **_):
        self.generated = True
        self.requests.append(
            (
                question.correlation.run_id,
                question.correlation.attempt_id,
                idempotency_key,
            )
        )
        return _candidate_for_question(
            question, authorized_context.bundle.lineage_reference
        )


class ValidationPlan:
    def create_plan(self, *, question, authorized_context, candidate):
        del authorized_context
        citation = candidate.answer.citations[0]
        request = SemanticSupportRequest(
            policy=SemanticSupportPolicy(
                policy_id=UUID(int=30),
                policy_version="semantic-support-policy:1",
                policy_digest="a" * 64,
            ),
            scope=question.scope,
            correlation=question.correlation,
            context_bundle=candidate.context_bundle,
            claim=candidate.answer.claims[0],
            relationships=candidate.answer.relationships,
            evidence=(
                SemanticSupportEvidence(
                    citation=citation,
                    content="synthetic authorized evidence",
                ),
            ),
        )
        evidence_reference = citation.evidence_reference
        evaluation = AnswerEvaluationDecision(
            boundary=AttemptProjectionBoundary(
                workspace_id=question.scope.workspace_id,
                environment_id=question.scope.environment_id,
                run_id=question.correlation.run_id,
                attempt_id=question.correlation.attempt_id,
                projection_snapshot_id=evidence_reference.projection_snapshot_id,
                activation_generation=evidence_reference.activation_generation,
                canonical_boundary_id=UUID(int=33),
            ),
            target_scope_id=UUID(int=34),
            observed_coverage_id=UUID(int=35),
            evaluation_lineage_reference=UUID(int=36),
            disclosure_status="accepted",
            coverage_status="complete",
            partiality_status="exhaustive",
            conflict_status="none",
            safe_reason=AnswerEvaluationSafeReason.COMPLETE,
            disclosable_claim_ids=tuple(
                claim.claim_id for claim in candidate.answer.claims
            ),
        )
        return AnswerQuestionValidationPlan(
            citation_authorization_context=object(),
            semantic_requests=(request,),
            evaluation=evaluation,
        )


class ExistingCitationValidation:
    async def validate(
        self, *, citation, expected_context_bundle, authorization_context
    ):
        del authorization_context
        return ValidCitation(
            validation_reference=UUID(int=31),
            facts=CitationValidationFacts(
                citation_id=citation.citation_id,
                context_bundle_id=expected_context_bundle.bundle.bundle_id,
                context_bundle_version=expected_context_bundle.bundle.bundle_version,
                context_bundle_digest=expected_context_bundle.bundle.bundle_digest,
                policy_decision_generations=candidate_input().context_bundle.policy_decision_generations,
            ),
        )


class ExistingSemanticValidation:
    async def validate(self, request):
        return SemanticSupportResult(
            validation_record_id=UUID(int=32),
            policy=request.policy,
            scope=request.scope,
            correlation=request.correlation,
            context_bundle=request.context_bundle,
            answer_id=request.claim.location.answer_id,
            claim_id=request.claim.claim_id,
            evidence_ids=tuple(item.citation.evidence_id for item in request.evidence),
            citation_ids=tuple(item.citation.citation_id for item in request.evidence),
            relationship_ids=tuple(
                item.relationship_id for item in request.relationships
            ),
            outcome=SemanticSupportOutcome.SUPPORTED,
            reason_code=SemanticSupportReasonCode.SUPPORTED_BY_EVIDENCE,
            disclosure_eligible=True,
            qna_failure=False,
        )


@pytest.mark.asyncio
async def test_execution_failure_returns_safe_identity_bound_result() -> None:
    capability = ProductionAnswerQuestionCapability(
        context=object(),  # not reached when execution binding is unavailable
        execution=UnavailableExecution(),
        context_references=References(),
        disclosure_gate=object(),
        candidate_runner=object(),
        validator=object(),
        disclosure_environment=EnvironmentKind.DEVELOPMENT,
    )
    result = await capability.answer(command())

    assert type(result) is AnswerQuestionCapabilityResult
    assert result.command_id == COMMAND_ID
    assert result.session_id == SESSION
    assert result.message_id == MESSAGE
    assert result.output.outcome is AnswerQuestionOutcome.FAILED
    assert result.availability is AnswerQuestionCapabilityAvailability.UNAVAILABLE
    assert (
        result.output.reason.code
        is AnswerQuestionSafeReasonCode.MANDATORY_PROCESSING_FAILED
    )
    assert result.output.correlation.run_id == RUN
    assert "protected text" not in repr(result)


@pytest.mark.asyncio
async def test_unadmitted_command_identity_is_stable_for_idempotent_retry() -> None:
    capability = ProductionAnswerQuestionCapability(
        context=object(),
        execution=UnavailableExecution(),
        context_references=References(),
        disclosure_gate=object(),
        candidate_runner=object(),
        validator=object(),
        disclosure_environment=EnvironmentKind.DEVELOPMENT,
    )
    request = command(run_id=None)

    first = await capability.answer(request)
    retry = await capability.answer(request)

    assert first == retry
    assert first.command_id == COMMAND_ID
    assert first.output.correlation.run_id.int != 0
    assert first.output.correlation.run_id == retry.output.correlation.run_id
    assert first.output.correlation.attempt_id == retry.output.correlation.attempt_id


@pytest.mark.asyncio
async def test_persisted_attempt_identity_is_used_before_context_retrieval() -> None:
    canonical_attempt_id = UUID(int=99)
    context = TerminalContext(AnswerQuestionOutcome.ABSTAINED)
    capability = ProductionAnswerQuestionCapability(
        context=context,
        execution=BoundExecution(),
        context_references=References(),
        disclosure_gate=object(),
        candidate_runner=UnexpectedCandidateStage(),
        validator=object(),
        disclosure_environment=EnvironmentKind.DEVELOPMENT,
    )

    result = await capability.answer(
        command(
            worker=True,
            attempt_id=canonical_attempt_id,
            attempt_number=3,
        )
    )

    assert result.output.correlation.attempt_id == canonical_attempt_id
    assert context.prepared == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        AnswerQuestionOutcome.ABSTAINED,
        AnswerQuestionOutcome.DENIED,
        AnswerQuestionOutcome.FAILED,
        AnswerQuestionOutcome.CANCELLED,
    ],
)
async def test_context_terminal_outcome_is_preserved_without_candidate_generation(
    outcome: AnswerQuestionOutcome,
) -> None:
    context = TerminalContext(outcome)
    capability = ProductionAnswerQuestionCapability(
        context=context,
        execution=BoundExecution(),
        context_references=References(),
        disclosure_gate=object(),
        candidate_runner=UnexpectedCandidateStage(),
        validator=object(),
        disclosure_environment=EnvironmentKind.DEVELOPMENT,
    )

    result = await capability.answer(command(worker=True))

    assert result.output.outcome is outcome
    assert result.command_id == COMMAND_ID
    assert result.session_id == SESSION
    assert result.message_id == MESSAGE
    assert result.output.correlation.run_id == RUN
    assert result.availability is AnswerQuestionCapabilityAvailability.AVAILABLE
    assert context.prepared == 1


@pytest.mark.asyncio
async def test_supported_answer_is_validated_and_disclosed_once() -> None:
    context = SuccessfulContext()
    runner = SuccessfulCandidateRunner()
    delegate_validator = ExistingAnswerQuestionCandidateValidator(
        citations=ExistingCitationValidation(),
        semantic_support=ExistingSemanticValidation(),
        plans=ValidationPlan(),
    )

    class RecordingValidator:
        error = None
        calls = 0

        async def validate(self, **kwargs):
            self.calls += 1
            try:
                return await delegate_validator.validate(**kwargs)
            except Exception as error:
                self.error = error
                raise

    validator = RecordingValidator()
    capability = ProductionAnswerQuestionCapability(
        context=context,
        execution=BoundExecution(),
        context_references=References(),
        disclosure_gate=object(),
        candidate_runner=runner,
        validator=validator,
        disclosure_environment=EnvironmentKind.DEVELOPMENT,
    )
    result = await capability.answer(command())

    assert runner.generated
    assert validator.calls == 1
    assert validator.error is None, repr(validator.error)
    assert len(context.disclosures) == 1


@pytest.mark.asyncio
async def test_candidate_generation_failure_returns_safe_unavailable_result() -> None:
    context = SuccessfulContext()

    class FailingCandidateRunner:
        async def generate(self, **_kwargs):
            raise RuntimeError("protected candidate and provider diagnostics")

    capability = ProductionAnswerQuestionCapability(
        context=context,
        execution=BoundExecution(),
        context_references=References(),
        disclosure_gate=object(),
        candidate_runner=FailingCandidateRunner(),
        validator=object(),
        disclosure_environment=EnvironmentKind.DEVELOPMENT,
    )

    result = await capability.answer(command())

    assert result.output.outcome is AnswerQuestionOutcome.FAILED
    assert result.availability is AnswerQuestionCapabilityAvailability.UNAVAILABLE
    assert result.accepted_answer is None
    assert not context.disclosures
    assert "protected candidate" not in repr(result)
    assert "provider diagnostics" not in repr(result)


@pytest.mark.asyncio
async def test_candidate_identity_mismatch_fails_closed_before_validation() -> None:
    context = SuccessfulContext()

    class MismatchedCandidateRunner:
        async def generate(self, *, question, authorized_context, **_kwargs):
            foreign_question = question.model_copy(
                update={"subject": QuestionSubject(text="different question")}
            )
            return _candidate_for_question(
                foreign_question, authorized_context.bundle.lineage_reference
            )

    class UnexpectedValidator:
        async def validate(self, **_kwargs):
            raise AssertionError("mismatched candidate must not be validated")

    capability = ProductionAnswerQuestionCapability(
        context=context,
        execution=BoundExecution(),
        context_references=References(),
        disclosure_gate=object(),
        candidate_runner=MismatchedCandidateRunner(),
        validator=UnexpectedValidator(),
        disclosure_environment=EnvironmentKind.DEVELOPMENT,
    )

    result = await capability.answer(command())

    assert result.output.outcome is AnswerQuestionOutcome.FAILED
    assert result.availability is AnswerQuestionCapabilityAvailability.AVAILABLE
    assert result.output.scope.workspace_id == WORKSPACE
    assert result.accepted_answer is None
    assert not context.disclosures


@pytest.mark.asyncio
async def test_supported_answer_revalidates_and_persists_through_disclosure_gate() -> (
    None
):
    request_id = uuid5(RUN, "context-request")
    attempt_id = uuid5(RUN, "attempt:1")
    idempotency_key = "admitted-key"
    bundle_values = base_context_bundle().model_dump(mode="python")
    bundle_values.update(
        {
            "run_id": RUN,
            "attempt_id": attempt_id,
            "request_id": request_id,
            "idempotency_key": idempotency_key,
            "trusted_request_context_ref": UUID(int=10),
            "authorization_context_ref": UUID(int=11),
        }
    )
    bundle = ContextBundle.create(**bundle_values)
    revalidator = ContextRevalidator()

    class CurrentAuthority:
        async def revalidate(self, authority_request):
            dependency = authority_request.dependencies.dependencies[0]
            return DisclosureRevalidationResult(
                request_id=authority_request.request_id,
                request_digest=authority_request.command_digest,
                workspace_id=authority_request.workspace_id,
                environment_id=authority_request.environment_id,
                checkpoint=authority_request.checkpoint,
                disposition=DisclosureRevalidationDisposition.SAFE_REUSE,
                safe_reason="current_authority_confirmed",
                facts=(
                    DisclosureValidationFact.create(
                        validation_id=UUID(int=53),
                        request_id=authority_request.request_id,
                        workspace_id=authority_request.workspace_id,
                        environment_id=authority_request.environment_id,
                        run_id=authority_request.run_id,
                        attempt_id=authority_request.attempt_id,
                        message_id=None,
                        checkpoint=authority_request.checkpoint,
                        dependency_id=dependency.dependency_id,
                        disposition=DisclosureRevalidationDisposition.SAFE_REUSE,
                        failure_codes=(),
                        evaluated_at=datetime(2026, 10, 18, tzinfo=timezone.utc),
                    ),
                ),
            )

    store = InMemoryContextBundleStore()
    await store.create_or_replay(bundle)
    context = AnswerQuestionContextService(
        broker=ContextBroker(
            ContextResponse(
                outcome=ContextOutcome.ALLOWED,
                context_bundle_ref=bundle.reference,
                reason_code=ContextReasonCode.ALLOWED,
                publication_pin_id=bundle.publication_pin_id,
            )
        ),
        bundles=SavedContextBundleAccess(store=store, revalidator=revalidator),
        revalidator=revalidator,
        disclosure_revalidator=CurrentAuthority(),
    )

    class PinnedExecution(BoundExecution):
        def resolve(self, execution_request):
            assert execution_request.workspace_id == bundle.workspace_id
            return None, pin_for(bundle)

    repository = InMemoryAnswerDisclosureRepository()
    sink = InMemoryAcceptedAnswerOutputPort(reference_factory=lambda: UUID(int=40))
    gate = AnswerDisclosureGate(
        repository=repository,
        accepted_output=sink,
        clock=lambda: datetime(2026, 10, 18, tzinfo=timezone.utc),
    )
    capability = ProductionAnswerQuestionCapability(
        context=context,
        execution=PinnedExecution(),
        context_references=References(),
        disclosure_gate=gate,
        candidate_runner=SuccessfulCandidateRunner(),
        validator=ExistingAnswerQuestionCandidateValidator(
            citations=ExistingCitationValidation(),
            semantic_support=ExistingSemanticValidation(),
            plans=ValidationPlan(),
        ),
        disclosure_environment=EnvironmentKind.DEVELOPMENT,
    )

    result = await capability.answer(command())
    stored = await repository.resolve(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        request_id=request_id,
    )

    assert result.output.outcome is AnswerQuestionOutcome.ANSWERED
    assert result.accepted_answer is not None
    assert stored is not None and stored.accepted_answer == result.accepted_answer
    assert len(sink.published) == 1
    assert [item.checkpoint.value for item in revalidator.requests] == [
        "before_evidence_reuse",
        "before_final_disclosure",
    ]


@pytest.mark.asyncio
async def test_retry_after_partial_validation_reuses_capability_identities() -> None:
    context = SuccessfulContext()
    runner = SuccessfulCandidateRunner()
    delegate = ExistingAnswerQuestionCandidateValidator(
        citations=ExistingCitationValidation(),
        semantic_support=ExistingSemanticValidation(),
        plans=ValidationPlan(),
    )

    class PartialFailureValidator:
        calls = 0

        async def validate(self, **kwargs):
            facts = await delegate.validate(**kwargs)
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("retryable validation infrastructure failure")
            return facts

    capability = ProductionAnswerQuestionCapability(
        context=context,
        execution=BoundExecution(),
        context_references=References(),
        disclosure_gate=object(),
        candidate_runner=runner,
        validator=PartialFailureValidator(),
        disclosure_environment=EnvironmentKind.DEVELOPMENT,
    )

    first = await capability.answer(command())
    retry = await capability.answer(command())

    assert first.output.outcome is AnswerQuestionOutcome.FAILED
    assert first.availability is AnswerQuestionCapabilityAvailability.UNAVAILABLE
    assert retry.output.outcome is AnswerQuestionOutcome.ANSWERED
    assert first.output.correlation == retry.output.correlation
    assert runner.requests[0] == runner.requests[1]
    assert [item.request_id for item in context.requests] == [
        context.requests[0].request_id,
        context.requests[0].request_id,
    ]
    assert [item.idempotency_key for item in context.requests] == [
        "admitted-key",
        "admitted-key",
    ]
    assert len(context.disclosures) == 1


@pytest.mark.asyncio
async def test_persisted_candidate_runner_uses_the_governed_runtime_and_lineage() -> (
    None
):
    question = candidate_question()
    input_value = candidate_input()
    output_value = candidate_output()
    output_artifact = Artifact(
        id=UUID(int=21),
        workspace_id=question.scope.workspace_id,
        environment_id=question.scope.environment_id,
        work_item_id=question.correlation.run_id,
        produced_by=ActorRef(kind=ActorKind.HUMAN, id=SUBJECT),
        artifact_schema=SchemaRef(
            name="answer-question.candidate-output", version="v1"
        ),
        content=output_value,
        created_at=datetime(2026, 10, 18, tzinfo=timezone.utc),
    )
    pin = AnswerQuestionExecutionPin.create(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        environment=EnvironmentKind.DEVELOPMENT,
        configuration=configuration(),
    )
    execution = CandidateExecution(
        AnswerQuestionExecutionResult(
            pin=pin,
            result=AgentResult(
                status=AgentRunStatus.SUCCEEDED,
                output_artifact_ids=(output_artifact.id,),
            ),
        )
    )
    authorized_context = SimpleNamespace(
        candidate_input=input_value,
        bundle=input_value.context_bundle,
    )
    runner = PersistedAnswerQuestionCandidateRunner(
        input_artifacts=PersistedInput(),
        artifacts=PersistedOutput(output_artifact),
        canceller=UnexpectedCanceller(),
    )

    result = await runner.generate(
        question=question,
        requester_id=SUBJECT,
        authorized_context=authorized_context,
        execution=execution,
        execution_request=AnswerQuestionExecutionRequest(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            environment=EnvironmentKind.DEVELOPMENT,
        ),
        expected_execution_pin=pin,
        idempotency_key="safe-idempotency-key",
    )

    assert result == output_value
    assert len(execution.invocations) == 1
    assert execution.invocations[0].input_artifact_ids == (UUID(int=20),)
    assert execution.invocations[0].work_item_id == question.correlation.run_id

    changed_pin = AnswerQuestionExecutionPin.create(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        environment=EnvironmentKind.PRODUCTION,
        configuration=configuration(),
    )
    with pytest.raises(RuntimeError, match="execution pin changed"):
        await runner.generate(
            question=question,
            requester_id=SUBJECT,
            authorized_context=authorized_context,
            execution=execution,
            execution_request=AnswerQuestionExecutionRequest(
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                environment=EnvironmentKind.DEVELOPMENT,
            ),
            expected_execution_pin=changed_pin,
            idempotency_key="safe-idempotency-key",
        )


@pytest.mark.asyncio
async def test_existing_candidate_validator_composes_citation_and_semantic_services() -> (
    None
):
    candidate = candidate_output()
    input_value = candidate_input()
    validator = ExistingAnswerQuestionCandidateValidator(
        citations=ExistingCitationValidation(),
        semantic_support=ExistingSemanticValidation(),
        plans=ValidationPlan(),
    )

    facts = await validator.validate(
        question=candidate.question,
        authorized_context=SimpleNamespace(bundle=input_value.context_bundle),
        candidate=candidate,
    )

    assert facts.question == candidate.question
    assert facts.candidate == candidate
    assert len(facts.citation_results) == len(candidate.answer.citations)
    assert tuple(item.claim_id for item in facts.semantic_results) == tuple(
        item.claim_id for item in candidate.answer.claims
    )
