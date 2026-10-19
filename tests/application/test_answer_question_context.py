from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.qa_observation.context_capture import (
    AuthorizedContextCaptureContext,
    AuthorizedContextCaptureResult,
    CaptureDisposition,
)
from spine.application.qa_observation.contracts import (
    AdmittedExecutionIdentity,
    CaptureSubscription,
)

from spine.application.answer_question_context import (
    AnswerQuestionContextRequest,
    AnswerQuestionContextService,
    map_context_outcome_to_retrieval_outcome,
)
from spine.application.answer_disclosure import AnswerDisclosureFacts, AnswerDisclosureResult
from spine.application.answer_outcomes import AnswerOutcomeFacts
from spine.application.authorized_context_broker import (
    AuthorizedContextRequest,
    InMemoryContextBundleStore,
    SavedContextBundleAccess,
)
from spine.domain.capabilities import (
    AnswerAuthorizationOutcome,
    AnswerQuestionInput,
    AnswerQuestionOutcome,
    AnswerQuestionOutput,
    AnswerQuestionSafeReason,
    AnswerQuestionSafeReasonCode,
    AnswerQuestionScope,
    RetrievalOutcome,
    RunAttemptCorrelation,
)
from spine.domain.capabilities.answer_question_execution import (
    AnswerQuestionExecutionPin,
    PinnedContextProfile,
    PinnedProjection,
)
from spine.domain.context_bundles import (
    ContextBundle,
    ContextBundleRef,
    RevalidationCheckpoint,
    RevalidationCheckpointResult,
    RevalidationDisposition,
    RevalidationRequest,
)
from spine.domain.disclosure import (
    DisclosureRevalidationCheckpoint,
    DisclosureRevalidationDisposition,
    DisclosureRevalidationRequest,
    DisclosureRevalidationResult,
    DisclosureValidationFact,
)
from spine.memory.contracts import ContextOutcome, ContextReasonCode, ContextResponse
from spine.domain.common import EnvironmentKind
from spine.domain.common import ActorKind, ActorRef

from tests.domain.context_bundle.test_context_bundle_models import context_bundle
from tests.application.test_answer_question_execution import configuration


WORKSPACE = UUID(int=1)
ENVIRONMENT = UUID(int=2)
RUN = UUID(int=8)
ATTEMPT = UUID(int=9)


def question() -> AnswerQuestionInput:
    return AnswerQuestionInput(
        subject={"text": "Where is the contract?"},
        scope=AnswerQuestionScope(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
        ),
        correlation=RunAttemptCorrelation(
            run_id=RUN,
            attempt_id=ATTEMPT,
            trace_id=UUID(int=30),
        ),
    )


def pin_for(bundle: ContextBundle) -> AnswerQuestionExecutionPin:
    selected = configuration().model_copy(
        update={
            "context_profile": PinnedContextProfile(
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                configuration_generation=9,
                binding_key="qa.context:3",
                profile=bundle.profile_ref,
            ),
            "projection": PinnedProjection(
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                configuration_generation=9,
                projection_snapshot_id=bundle.projection_snapshot.projection_snapshot_id,
                snapshot_manifest_digest=bundle.projection_snapshot.manifest_digest,
                projection_activation_id=bundle.projection_activation.projection_activation_id,
                activation_generation=bundle.activation_generation,
                projection_config_version_id=bundle.projection_config_version.projection_config_version_id,
                projection_config_digest=bundle.projection_config_version.config_digest,
                canonical_boundary_id=bundle.canonical_boundary.boundary_id,
                canonical_boundary_digest=bundle.canonical_boundary.manifest_digest,
            ),
        }
    )
    return AnswerQuestionExecutionPin.create(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        environment=EnvironmentKind.DEVELOPMENT,
        configuration=selected,
    )


class Broker:
    def __init__(self, response: ContextResponse) -> None:
        self.response = response
        self.requests: list[AuthorizedContextRequest] = []

    async def retrieve(self, request: AuthorizedContextRequest) -> ContextResponse:
        self.requests.append(request)
        return self.response


class Revalidator:
    def __init__(
        self,
        *,
        disposition: RevalidationDisposition = RevalidationDisposition.SAFE_REPLAY,
        failures_remaining: int = 0,
    ) -> None:
        self.disposition = disposition
        self.failures_remaining = failures_remaining
        self.requests: list[RevalidationRequest] = []

    async def revalidate(self, request: RevalidationRequest) -> RevalidationCheckpointResult:
        self.requests.append(request)
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("temporary revalidation failure")
        return RevalidationCheckpointResult(
            disposition=self.disposition,
            checkpoint=request.checkpoint,
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            bundle_reference=(
                request.bundle.reference
                if self.disposition is RevalidationDisposition.SAFE_REPLAY
                else None
            ),
            idempotency_key=request.idempotency_key,
            command_digest=request.command_digest,
            failure_codes=(),
            retry_attempt=request.retry_attempt,
        )


class RecordingDisclosureGate:
    def __init__(self) -> None:
        self.facts: AnswerDisclosureFacts | None = None
        self.result: AnswerDisclosureResult | None = None

    async def disclose_facts(self, facts: AnswerDisclosureFacts) -> AnswerDisclosureResult:
        self.facts = facts
        request = facts.to_request()
        question_value = facts.outcome_facts.question
        safe_output = AnswerQuestionOutput(
            correlation=question_value.correlation,
            scope=question_value.scope,
            outcome=AnswerQuestionOutcome.ABSTAINED,
            reason=AnswerQuestionSafeReason(code=AnswerQuestionSafeReasonCode.REJECTED_CANDIDATE),
        )
        from spine.domain.capabilities.answer_disclosure import AnswerDisclosureRecord

        self.result = AnswerDisclosureResult(
            record=AnswerDisclosureRecord(
                request_id=request.request_id,
                request_digest=request.command_digest,
                workspace_id=request.decision.question.scope.workspace_id,
                environment_id=request.decision.question.scope.environment_id,
                run_id=request.decision.question.correlation.run_id,
                attempt_id=request.decision.question.correlation.attempt_id,
                outcome=AnswerQuestionOutcome.ABSTAINED,
                safe_output=safe_output,
                created_at=datetime(2026, 10, 16, tzinfo=timezone.utc),
            ),
            replay=False,
        )
        return self.result


class DisclosureRevalidator:
    def __init__(self, result: DisclosureRevalidationResult) -> None:
        self.result = result
        self.requests: list[DisclosureRevalidationRequest] = []

    async def revalidate(self, request: DisclosureRevalidationRequest) -> DisclosureRevalidationResult:
        self.requests.append(request)
        return self.result


def eligible_disclosure_result() -> DisclosureRevalidationResult:
    return DisclosureRevalidationResult(
        request_id=UUID(int=50),
        request_digest="a" * 64,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        checkpoint=DisclosureRevalidationCheckpoint.BEFORE_STORED_ANSWER_READ,
        disposition=DisclosureRevalidationDisposition.SAFE_REUSE,
        safe_reason="current_authority_confirmed",
        facts=(
            DisclosureValidationFact.create(
                validation_id=UUID(int=51),
                request_id=UUID(int=50),
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                run_id=RUN,
                attempt_id=ATTEMPT,
                message_id=None,
                checkpoint=DisclosureRevalidationCheckpoint.BEFORE_STORED_ANSWER_READ,
                dependency_id=UUID(int=52),
                disposition=DisclosureRevalidationDisposition.SAFE_REUSE,
                failure_codes=(),
                evaluated_at=datetime(2026, 10, 16, tzinfo=timezone.utc),
            ),
        ),
    )


def request(*, cancelled: bool = False, capture_context=None) -> AnswerQuestionContextRequest:
    bundle = context_bundle()
    return AnswerQuestionContextRequest(
        question=question(),
        execution_pin=pin_for(bundle),
        request_id=bundle.request_id,
        trusted_request_context_ref=bundle.trusted_request_context_ref,
        authorization_context_ref=bundle.authorization_context_ref,
        idempotency_key=bundle.idempotency_key,
        cancelled=cancelled,
        capture_context=capture_context,
    )


@pytest.mark.asyncio
async def test_allowed_context_consumes_exact_bundle_and_pin_without_caller_selectors() -> None:
    bundle = context_bundle()
    broker = Broker(
        ContextResponse(
            outcome=ContextOutcome.ALLOWED,
            context_bundle_ref=bundle.reference,
            reason_code=ContextReasonCode.ALLOWED,
            publication_pin_id=bundle.publication_pin_id,
        )
    )
    revalidator = Revalidator()
    store = InMemoryContextBundleStore()
    await store.create_or_replay(bundle)
    service = AnswerQuestionContextService(
        broker=broker,
        bundles=SavedContextBundleAccess(store=store, revalidator=revalidator),
        revalidator=revalidator,
    )

    result = await service.prepare(request())

    assert result.output is None
    assert result.context is not None
    assert result.context.bundle is bundle
    assert result.context.candidate_input.context_bundle is bundle
    assert result.context.disclosure_dependencies.dependencies[0].source_object_id == UUID(int=20)
    sent = broker.requests[0]
    assert sent.profile_ref == bundle.profile_ref
    assert sent.run_id == RUN
    assert sent.attempt_id == ATTEMPT
    assert "projection_snapshot_id" not in sent.model_dump()
    assert "physical_namespace" not in sent.model_dump()
    assert revalidator.requests[0].checkpoint is RevalidationCheckpoint.BEFORE_EVIDENCE_REUSE


@pytest.mark.asyncio
async def test_denied_context_returns_safe_denial_without_loading_bundle() -> None:
    broker = Broker(
        ContextResponse(
            outcome=ContextOutcome.DENIED,
            reason_code=ContextReasonCode.AUTHORIZATION_DENIED,
        )
    )
    service = AnswerQuestionContextService(
        broker=broker,
        bundles=ExplodingBundles(),
        revalidator=Revalidator(),
    )

    result = await service.prepare(request())

    assert result.context is None
    assert result.output is not None
    assert result.output.outcome is AnswerQuestionOutcome.DENIED
    assert result.output.reason.code.value == "authorization_denied"
    assert "protected" not in result.output.model_dump_json()


@pytest.mark.asyncio
async def test_capture_enabled_context_uses_one_broker_call_without_fabricating_evidence() -> None:
    denied = ContextResponse(
        outcome=ContextOutcome.DENIED,
        reason_code=ContextReasonCode.AUTHORIZATION_DENIED,
    )

    class CaptureBroker:
        retrieve_calls = 0
        capture_calls = 0

        async def retrieve(self, request):
            self.retrieve_calls += 1
            raise AssertionError("capture path must not issue a second retrieval")

        async def retrieve_with_capture(
            self, request, *, capture_context, capture_sink
        ):
            del request, capture_context, capture_sink
            self.capture_calls += 1
            return AuthorizedContextCaptureResult(
                response=denied,
                disposition=CaptureDisposition.NOT_ELIGIBLE,
            )

    capture_context = AuthorizedContextCaptureContext(
        identity=AdmittedExecutionIdentity(
            command_id=UUID(int=70),
            run_id=RUN,
            session_id=UUID(int=71),
            message_id=UUID(int=72),
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            requester=ActorRef(kind=ActorKind.HUMAN, id=UUID(int=73)),
            attempt_id=ATTEMPT,
            attempt_number=1,
        ),
        subscription=CaptureSubscription(
            capture_id=UUID(int=74),
            subscription_id=UUID(int=75),
            expires_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
        ),
        question=question(),
    )
    broker = CaptureBroker()
    service = AnswerQuestionContextService(
        broker=broker,
        bundles=SavedContextBundleAccess(
            store=InMemoryContextBundleStore(), revalidator=Revalidator()
        ),
        revalidator=Revalidator(),
    )

    result = await service.prepare(
        request(capture_context=capture_context)
    )

    assert broker.capture_calls == 1
    assert broker.retrieve_calls == 0
    assert result.output is not None
    assert result.output.outcome is AnswerQuestionOutcome.DENIED
    assert result.broker_capture is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "reason", "expected"),
    [
        (ContextOutcome.INSUFFICIENT, ContextReasonCode.INSUFFICIENT_EVIDENCE, AnswerQuestionOutcome.ABSTAINED),
        (ContextOutcome.STALE, ContextReasonCode.STALE_CONTEXT, AnswerQuestionOutcome.ABSTAINED),
        (ContextOutcome.UNAVAILABLE, ContextReasonCode.RETRIEVAL_UNAVAILABLE, AnswerQuestionOutcome.FAILED),
        (ContextOutcome.DEGRADED, ContextReasonCode.DEGRADED_CONTEXT, AnswerQuestionOutcome.FAILED),
        (ContextOutcome.CANCELLED, ContextReasonCode.CANCELLED, AnswerQuestionOutcome.CANCELLED),
    ],
)
async def test_broker_terminal_outcomes_are_not_treated_as_answers(
    outcome: ContextOutcome,
    reason: ContextReasonCode,
    expected: AnswerQuestionOutcome,
) -> None:
    service = AnswerQuestionContextService(
        broker=Broker(ContextResponse(outcome=outcome, reason_code=reason)),
        bundles=ExplodingBundles(),
        revalidator=Revalidator(),
    )

    result = await service.prepare(request())

    assert result.context is None
    assert result.output is not None
    assert result.output.outcome is expected


@pytest.mark.asyncio
async def test_idempotent_prepare_replay_preserves_the_same_bundle_identity() -> None:
    bundle = context_bundle()
    store = InMemoryContextBundleStore()
    await store.create_or_replay(bundle)
    broker = Broker(
        ContextResponse(
            outcome=ContextOutcome.ALLOWED,
            context_bundle_ref=bundle.reference,
            reason_code=ContextReasonCode.ALLOWED,
            publication_pin_id=bundle.publication_pin_id,
        )
    )
    revalidator = Revalidator()
    service = AnswerQuestionContextService(
        broker=broker,
        bundles=SavedContextBundleAccess(store=store, revalidator=revalidator),
        revalidator=revalidator,
    )

    first = await service.prepare(request())
    replay = await service.prepare(request())

    assert first.context is not None and replay.context is not None
    assert replay.context.bundle is first.context.bundle
    assert [item.idempotency_key for item in broker.requests] == [bundle.idempotency_key] * 2


@pytest.mark.asyncio
async def test_retry_after_partial_revalidation_failure_reuses_the_same_bundle() -> None:
    bundle = context_bundle()
    store = InMemoryContextBundleStore()
    await store.create_or_replay(bundle)
    revalidator = Revalidator(failures_remaining=1)
    service = AnswerQuestionContextService(
        broker=Broker(
            ContextResponse(
                outcome=ContextOutcome.ALLOWED,
                context_bundle_ref=bundle.reference,
                reason_code=ContextReasonCode.ALLOWED,
                publication_pin_id=bundle.publication_pin_id,
            )
        ),
        bundles=SavedContextBundleAccess(store=store, revalidator=revalidator),
        revalidator=revalidator,
    )

    failed = await service.prepare(request())
    retried = await service.prepare(request())

    assert failed.output is not None
    assert failed.output.outcome is AnswerQuestionOutcome.FAILED
    assert retried.context is not None
    assert retried.context.bundle.reference == bundle.reference


@pytest.mark.asyncio
async def test_retryable_revalidation_disposition_is_not_a_terminal_failure() -> None:
    bundle = context_bundle()
    store = InMemoryContextBundleStore()
    await store.create_or_replay(bundle)
    revalidator = Revalidator(disposition=RevalidationDisposition.RETRYABLE_FAILURE)
    service = AnswerQuestionContextService(
        broker=Broker(
            ContextResponse(
                outcome=ContextOutcome.ALLOWED,
                context_bundle_ref=bundle.reference,
                reason_code=ContextReasonCode.ALLOWED,
                publication_pin_id=bundle.publication_pin_id,
            )
        ),
        bundles=SavedContextBundleAccess(store=store, revalidator=revalidator),
        revalidator=revalidator,
    )

    result = await service.prepare(request())

    assert result.output is not None
    assert result.output.outcome is AnswerQuestionOutcome.FAILED
    assert result.failure is not None


@pytest.mark.asyncio
async def test_disclose_delegates_to_aq11_gate_only_after_context_checkpoint() -> None:
    bundle = context_bundle()
    store = InMemoryContextBundleStore()
    await store.create_or_replay(bundle)
    revalidator = Revalidator()
    service = AnswerQuestionContextService(
        broker=Broker(
            ContextResponse(
                outcome=ContextOutcome.ALLOWED,
                context_bundle_ref=bundle.reference,
                reason_code=ContextReasonCode.ALLOWED,
                publication_pin_id=bundle.publication_pin_id,
            )
        ),
        bundles=SavedContextBundleAccess(store=store, revalidator=revalidator),
        revalidator=revalidator,
    )
    prepared = await service.prepare(request())
    assert prepared.context is not None
    gate = RecordingDisclosureGate()

    result = await service.disclose(
        context=prepared.context,
        outcome_facts=AnswerOutcomeFacts(
            question=prepared.context.question,
            retrieval_outcome=RetrievalOutcome.EVIDENCE_AVAILABLE,
            authorization=AnswerAuthorizationOutcome.ALLOWED,
        ),
        request_id=UUID(int=41),
        requester_id=UUID(int=40),
        disclosure_gate=gate,
    )

    assert result is gate.result
    assert gate.facts is not None
    assert gate.facts.current_eligibility is not None
    assert not gate.facts.current_eligibility.can_disclose
    assert gate.facts.outcome_facts.disclosure_eligible is False
    assert revalidator.requests[-1].checkpoint is RevalidationCheckpoint.BEFORE_FINAL_DISCLOSURE


@pytest.mark.asyncio
async def test_disclose_passes_eligible_current_authority_to_aq11_gate() -> None:
    bundle = context_bundle()
    store = InMemoryContextBundleStore()
    await store.create_or_replay(bundle)
    revalidator = Revalidator()
    disclosure_revalidator = DisclosureRevalidator(eligible_disclosure_result())
    service = AnswerQuestionContextService(
        broker=Broker(
            ContextResponse(
                outcome=ContextOutcome.ALLOWED,
                context_bundle_ref=bundle.reference,
                reason_code=ContextReasonCode.ALLOWED,
                publication_pin_id=bundle.publication_pin_id,
            )
        ),
        bundles=SavedContextBundleAccess(store=store, revalidator=revalidator),
        revalidator=revalidator,
        disclosure_revalidator=disclosure_revalidator,
    )
    prepared = await service.prepare(request())
    assert prepared.context is not None
    gate = RecordingDisclosureGate()

    await service.disclose(
        context=prepared.context,
        outcome_facts=AnswerOutcomeFacts(
            question=prepared.context.question,
            retrieval_outcome=RetrievalOutcome.EVIDENCE_AVAILABLE,
            authorization=AnswerAuthorizationOutcome.ALLOWED,
        ),
        request_id=UUID(int=53),
        requester_id=UUID(int=40),
        disclosure_gate=gate,
    )

    assert gate.facts is not None
    assert gate.facts.current_eligibility is not None
    assert gate.facts.current_eligibility.can_disclose
    assert gate.facts.outcome_facts.disclosure_eligible
    assert disclosure_revalidator.requests[0].dependencies == prepared.context.disclosure_dependencies


@pytest.mark.asyncio
async def test_foreign_workspace_bundle_is_rejected_without_context_leakage() -> None:
    bundle = context_bundle()
    foreign_bundle = bundle.model_copy(update={"workspace_id": UUID(int=99)})

    class ForeignBundlePort:
        async def load(
            self,
            *,
            workspace_id: UUID,
            environment_id: UUID,
            reference: ContextBundleRef,
        ) -> ContextBundle:
            return foreign_bundle

    service = AnswerQuestionContextService(
        broker=Broker(
            ContextResponse(
                outcome=ContextOutcome.ALLOWED,
                context_bundle_ref=bundle.reference,
                reason_code=ContextReasonCode.ALLOWED,
                publication_pin_id=bundle.publication_pin_id,
            )
        ),
        bundles=ForeignBundlePort(),
        revalidator=Revalidator(),
    )

    result = await service.prepare(request())

    assert result.context is None
    assert result.failure.value == "bundle_lineage_invalid"
    assert result.output is not None
    assert result.output.answer is None
    assert "Where is the contract?" not in result.output.model_dump_json()


@pytest.mark.asyncio
async def test_revalidation_failure_blocks_pre_disclosure_and_never_returns_answered() -> None:
    bundle = context_bundle()
    store = InMemoryContextBundleStore()
    await store.create_or_replay(bundle)
    revalidator = Revalidator(disposition=RevalidationDisposition.REJECTED)
    service = AnswerQuestionContextService(
        broker=Broker(
            ContextResponse(
                outcome=ContextOutcome.ALLOWED,
                context_bundle_ref=bundle.reference,
                reason_code=ContextReasonCode.ALLOWED,
                publication_pin_id=bundle.publication_pin_id,
            )
        ),
        bundles=SavedContextBundleAccess(store=store, revalidator=revalidator),
        revalidator=revalidator,
    )

    result = await service.prepare(request())

    assert result.output is not None
    assert result.output.outcome is AnswerQuestionOutcome.ABSTAINED


@pytest.mark.asyncio
async def test_final_disclosure_revalidation_reuses_same_bundle_and_dependencies() -> None:
    bundle = context_bundle()
    store = InMemoryContextBundleStore()
    await store.create_or_replay(bundle)
    revalidator = Revalidator()
    service = AnswerQuestionContextService(
        broker=Broker(
            ContextResponse(
                outcome=ContextOutcome.ALLOWED,
                context_bundle_ref=bundle.reference,
                reason_code=ContextReasonCode.ALLOWED,
                publication_pin_id=bundle.publication_pin_id,
            )
        ),
        bundles=SavedContextBundleAccess(store=store, revalidator=revalidator),
        revalidator=revalidator,
    )

    prepared = await service.prepare(request())
    assert prepared.context is not None

    checkpoint = await service.before_final_disclosure(
        prepared.context,
        requester_id=UUID(int=40),
    )

    assert not checkpoint.eligibility.can_disclose
    assert checkpoint.revalidation.checkpoint is RevalidationCheckpoint.BEFORE_FINAL_DISCLOSURE
    assert checkpoint.disclosure_dependencies == prepared.context.disclosure_dependencies
    assert [item.checkpoint for item in revalidator.requests] == [
        RevalidationCheckpoint.BEFORE_EVIDENCE_REUSE,
        RevalidationCheckpoint.BEFORE_FINAL_DISCLOSURE,
    ]


def test_context_outcome_mapping_keeps_broker_and_answer_outcomes_distinct() -> None:
    assert map_context_outcome_to_retrieval_outcome(ContextOutcome.ALLOWED) is RetrievalOutcome.EVIDENCE_AVAILABLE
    assert map_context_outcome_to_retrieval_outcome(ContextOutcome.PARTIAL) is RetrievalOutcome.EVIDENCE_AVAILABLE
    assert map_context_outcome_to_retrieval_outcome(ContextOutcome.INSUFFICIENT) is RetrievalOutcome.INSUFFICIENT_EVIDENCE
    assert map_context_outcome_to_retrieval_outcome(ContextOutcome.UNAVAILABLE) is RetrievalOutcome.UNAVAILABLE


class ExplodingBundles:
    async def load(self, **kwargs: object) -> ContextBundle:
        raise AssertionError("terminal broker outcome must not load a bundle")
