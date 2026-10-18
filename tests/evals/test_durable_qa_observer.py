from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid5

import pytest

from spine.api.composition import DurableAskComposition
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.application.persistence.ask_admission import AskAdmissionResult
from spine.application.qa_observation.admission import ObserverAskAdmissionResult
from spine.application.qa_observation.channel import (
    CaptureDisposition,
    CaptureReceiveResult,
    CaptureReceiveState,
)
from spine.application.qa_observation.contracts import (
    CaptureEnvelope,
    ExecutionWaitState,
    SameExecutionCapture,
    TerminalWaitResult,
)
from spine.application.ask_terminal_waiter import TerminalAskWaitResult
from spine.application.qa_sessions import QAMessageRead
from spine.domain.capabilities import (
    AcceptedGroundedAnswer,
    AnswerDisclosureValidation,
    AnswerQuestionOutcome,
    LineageReference,
)
from spine.domain.capabilities.accepted_answer_presentation import (
    AcceptedAnswerClaimPresentation,
    AcceptedAnswerPresentation,
    AcceptedAnswerRelationshipPresentation,
)
from spine.domain.capabilities.answer_question import (
    AnswerClaimKind,
    ClaimEvidenceRelationshipKind,
)
from spine.domain.common import ContextOrigin, EnvironmentKind
from spine.domain.qa_sessions import QAMessageDisclosureEligibility, QAMessageState
from spine.evals.durable_qa_observer import (
    DurableProductionQAAFlow,
    ObserverAuthorization,
)
from spine.evals.production_contract import (
    CaseExecutionDisposition,
    CaseExecutionFailureCategory,
    ProductionContractCaseRequest,
    ProductionContractObserver,
)
from spine.evals.qa_r1 import load_r1_dataset
from tests.evals.test_production_observer import _request
from tests.evals.test_production_contract import _safe_observation
from tests.domain.capabilities.test_answer_question_candidate import candidate_output
from tests.evals.test_unauthorized_retrieval_safety import (
    UNAUTHORIZED_RETRIEVAL_SAFETY_SEAM,
    _allowed_observation,
    _request as _allowed_request,
)
from hashlib import sha256


_WORKSPACE = UUID("42000000-0000-0000-0000-000000000478")
_ENVIRONMENT = UUID("43000000-0000-0000-0000-000000000478")
_SUBJECT = UUID("44000000-0000-0000-0000-000000000478")


def _composition() -> DurableAskComposition:
    return DurableAskComposition(
        admission=object(),
        api_ask_service=object(),
        workflow_executor=object(),
        delivery_bindings=(),
        workflow_definition=object,
        activity_bindings=(object(), object()),
        observer_admission=object(),
        sessions=object(),
        capture_channel=object(),
    )  # type: ignore[arg-type]


def _context(
    workspace_id: UUID, environment_id: UUID, subject_id: UUID = _SUBJECT
) -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(workspace_id, environment_id),
        origin=ContextOrigin.INTERACTIVE,
        purpose=PersistencePurpose("qa.ask"),
        operation=PersistenceOperation("qa.ask-admission-cancellation"),
        trace_id=UUID("45000000-0000-0000-0000-000000000478"),
        acting_subject_id=subject_id,
        service_principal_id=None,
        provenance=TrustedContextProvenance(
            issuer_id=UUID("46000000-0000-0000-0000-000000000478"),
            signature=b"test-signature",
        ),
    )


class _Authorization:
    def __init__(self, context: TrustedPersistenceContext) -> None:
        self.context = context

    async def authorize(self, *, request: ProductionContractCaseRequest):
        del request
        return ObserverAuthorization(
            context=self.context,
            environment=EnvironmentKind.PRODUCTION,
            authorization_generation=3,
        )


class _RequestAuthorization:
    async def authorize(self, *, request: ProductionContractCaseRequest):
        return ObserverAuthorization(
            context=_context(
                request.workspace_id,
                request.environment_id,
                request.principal_id,
            ),
            environment=EnvironmentKind.PRODUCTION,
            authorization_generation=3,
        )


class _SessionService:
    async def create_session(self, **kwargs):
        del kwargs
        return SimpleNamespace(
            value=SimpleNamespace(
                session_id=UUID("47000000-0000-0000-0000-000000000478")
            )
        )


class _Admission:
    async def admit(self, *, context, command):
        del context
        return ObserverAskAdmissionResult(
            admission=AskAdmissionResult(
                command_id=UUID("48000000-0000-0000-0000-000000000478"),
                session_id=command.session_id,
                message_id=UUID("49000000-0000-0000-0000-000000000478"),
                run_id=UUID("4a000000-0000-0000-0000-000000000478"),
                dispatch_event_id=UUID("4b000000-0000-0000-0000-000000000478"),
                replay=False,
            ),
            capture_id=UUID("4c000000-0000-0000-0000-000000000478"),
            subscription_id=UUID("4d000000-0000-0000-0000-000000000478"),
        )


class _TerminalWaiter:
    async def wait(self, *, identity, **kwargs):
        del kwargs
        case = next(
            item
            for item in load_r1_dataset().cases
            if item.case_id == self.case_id
        )
        observed = (
            _answered_observation(
                run_id=identity.run_id,
                attempt_id=identity.attempt_id,
            )
            if self.answered
            else _safe_observation(case)
        )
        question = observed.question
        answer = observed.answer
        disclosure = observed.disclosure
        capture = SameExecutionCapture(
            question=question,
            context_response=observed.context_response,
            context_bundle=observed.context_bundle,
            projection_retrieval=observed.projection_retrieval,
            retrieval=observed.retrieval,
            answer=answer,
            disclosure=disclosure,
        )
        message = QAMessageRead(
            message_id=identity.message_id,
            session_id=identity.session_id,
            workspace_id=identity.workspace_id,
            environment_id=identity.environment_id,
            version=1,
            ordinal=1,
            trace_id=question.correlation.trace_id,
            run_id=identity.run_id,
            state=QAMessageState.TERMINAL,
            terminal_outcome=answer.outcome,
            cancelled=False,
            disclosure_eligibility=(
                QAMessageDisclosureEligibility.ELIGIBLE
                if answer.outcome is AnswerQuestionOutcome.ANSWERED
                else QAMessageDisclosureEligibility.UNKNOWN
            ),
            question=question.subject.text,
            answer=answer.answer.text if answer.answer is not None else None,
            accepted_presentation=(
                _presentation(identity, answer.answer)
                if answer.answer is not None
                else None
            ),
            created_at=datetime(2026, 10, 18, tzinfo=timezone.utc),
            updated_at=datetime(2026, 10, 18, tzinfo=timezone.utc),
        )
        self.envelope = CaptureEnvelope(
            capture_id=UUID("4c000000-0000-0000-0000-000000000478"),
            subscription_id=UUID("4d000000-0000-0000-0000-000000000478"),
            identity=identity,
            capture=capture,
            worker_execution_elapsed_ns=125_000_000,
        )
        self.channel.envelope = self.envelope
        return TerminalAskWaitResult(
            wait=TerminalWaitResult(
                identity=identity,
                state=ExecutionWaitState.TERMINAL,
                outcome=answer.outcome,
            ),
            message=message,
        )


class _Channel:
    async def receive(self, *, identity, **kwargs):
        del identity, kwargs
        return CaptureReceiveResult(
            state=CaptureReceiveState.CAPTURED,
            capture=self.envelope,
        )

    async def acknowledge(self, **kwargs):
        del kwargs
        return CaptureDisposition.ACKNOWLEDGED

    async def cleanup(self, **kwargs):
        del kwargs
        return CaptureDisposition.CLEANED


def _answered_observation(*, run_id: UUID, attempt_id: UUID):
    observation = _allowed_observation(run_id=run_id, attempt_id=attempt_id)
    assert observation.context_bundle is not None
    bundle = observation.context_bundle
    candidate = candidate_output().answer
    claim = candidate.claims[0].model_copy(
        update={
            "workspace_id": observation.question.scope.workspace_id,
            "environment_id": observation.question.scope.environment_id,
            "run_id": observation.question.correlation.run_id,
            "attempt_id": observation.question.correlation.attempt_id,
        }
    )
    citation = candidate.citations[0].model_copy(
        update={
            "context_bundle": bundle.lineage_reference,
            "evidence_reference": bundle.evidence_references[0],
        }
    )
    relationship = candidate.relationships[0].model_copy(
        update={
            "workspace_id": observation.question.scope.workspace_id,
            "environment_id": observation.question.scope.environment_id,
            "run_id": observation.question.correlation.run_id,
            "attempt_id": observation.question.correlation.attempt_id,
        }
    )
    answer = candidate.model_copy(
        update={
            "workspace_id": observation.question.scope.workspace_id,
            "environment_id": observation.question.scope.environment_id,
            "run_id": observation.question.correlation.run_id,
            "attempt_id": observation.question.correlation.attempt_id,
            "claims": (claim,),
            "citations": (citation,),
            "relationships": (relationship,),
        }
    )
    validation = AnswerDisclosureValidation(
        citation_validation_reference=UUID("51000000-0000-0000-0000-000000000478"),
        semantic_support_validation_reference=UUID("52000000-0000-0000-0000-000000000478"),
        evaluation_lineage_reference=UUID("53000000-0000-0000-0000-000000000478"),
        disclosure_eligibility_reference=UUID("54000000-0000-0000-0000-000000000478"),
        context_bundle_id=bundle.bundle_id,
        context_bundle_version=bundle.bundle_version,
        context_bundle_digest=bundle.bundle_digest,
        claim_ids=(claim.claim_id,),
        citation_ids=(citation.citation_id,),
        validation_fact_ids=(UUID("55000000-0000-0000-0000-000000000478"),),
    )
    accepted = AcceptedGroundedAnswer(
        answer=answer,
        validation=validation,
        accepted_at=observation.disclosure.created_at,
    )
    answered = observation.answer.model_copy(
        update={
            "outcome": AnswerQuestionOutcome.ANSWERED,
            "answer": answer,
            "reason": None,
            "lineage": (
                LineageReference(
                    kind="context_bundle",
                    identifier=bundle.bundle_id,
                    schema_version="context-bundle:v1",
                ),
            ),
        }
    )
    disclosure = observation.disclosure.model_copy(
        update={
            "outcome": AnswerQuestionOutcome.ANSWERED,
            "validation": validation,
            "accepted_answer": accepted,
            "safe_output": None,
        }
    )
    return observation.model_copy(update={"answer": answered, "disclosure": disclosure})


def _presentation(identity, answer):
    return AcceptedAnswerPresentation(
        message_id=identity.message_id,
        run_id=identity.run_id,
        attempt_id=identity.attempt_id,
        answer_id=answer.answer_id,
        answer_text_sha256=sha256(answer.text.encode("utf-8")).hexdigest(),
        claims=tuple(
            AcceptedAnswerClaimPresentation(
                claim_id=claim.claim_id,
                kind=claim.kind,
                start_offset=claim.location.start_offset,
                end_offset=claim.location.end_offset,
            )
            for claim in answer.claims
        ),
        relationships=tuple(
            AcceptedAnswerRelationshipPresentation(
                relationship_id=relationship.relationship_id,
                claim_id=relationship.claim_id,
                kind=relationship.kind,
                citation_ids=relationship.citation_ids,
                related_claim_id=relationship.related_claim_id,
            )
            for relationship in answer.relationships
        ),
        citation_ids=tuple(citation.citation_id for citation in answer.citations),
    )


@pytest.mark.asyncio
async def test_durable_observer_denies_changed_runtime_scope_without_admission() -> None:
    request = _request()
    assert request.workspace_id != _WORKSPACE
    flow = DurableProductionQAAFlow(
        composition=_composition(),
        authorization=_Authorization(_context(_WORKSPACE, _ENVIRONMENT)),
        waiter=object(),  # type: ignore[arg-type]
        wait_timeout=timedelta(seconds=1),
    )

    record = await ProductionContractObserver(flow=flow).observe(request=request)

    assert record.disposition is CaseExecutionDisposition.DENIED
    assert record.failure_category is CaseExecutionFailureCategory.AUTHORIZATION_DENIED
    assert record.observation is None
    assert record.correlation_id is not None
    assert request.question not in record.model_dump_json()


@pytest.mark.asyncio
async def test_durable_observer_without_capture_composition_fails_closed() -> None:
    composition = DurableAskComposition(
        admission=object(),
        api_ask_service=object(),
        workflow_executor=object(),
        delivery_bindings=(),
        workflow_definition=object,
        activity_bindings=(object(), object()),
    )  # type: ignore[arg-type]
    flow = DurableProductionQAAFlow(
        composition=composition,
        authorization=_Authorization(_context(_WORKSPACE, _ENVIRONMENT)),
        waiter=object(),  # type: ignore[arg-type]
    )

    record = await ProductionContractObserver(flow=flow).observe(request=_request())

    assert record.disposition is CaseExecutionDisposition.UNAVAILABLE
    assert record.observation is None
    assert "protected" not in record.model_dump_json()


@pytest.mark.asyncio
async def test_durable_observer_freezes_same_execution_terminal_capture() -> None:
    channel = _Channel()
    waiter = _TerminalWaiter()
    waiter.case_id = "allowed-acl"
    waiter.answered = True
    waiter.channel = channel
    # The test fake represents the already configured application composition;
    # the observer still validates canonical identity and terminal correlation.
    composition = DurableAskComposition(
        admission=object(),
        api_ask_service=object(),
        workflow_executor=object(),
        delivery_bindings=(),
        workflow_definition=object,
        activity_bindings=(object(), object()),
        observer_admission=_Admission(),  # type: ignore[arg-type]
        sessions=_SessionService(),  # type: ignore[arg-type]
        capture_channel=channel,  # type: ignore[arg-type]
    )
    timing_samples: list[tuple[UUID, int]] = []
    flow = DurableProductionQAAFlow(
        composition=composition,
        authorization=_RequestAuthorization(),
        waiter=waiter,  # type: ignore[arg-type]
        invocation_id_source=lambda: UUID("4e000000-0000-0000-0000-000000000478"),
        worker_timing_sink=lambda attempt_id, elapsed: timing_samples.append(
            (attempt_id, elapsed)
        ),
    )

    request = _allowed_request()
    record = await ProductionContractObserver(
        flow=flow,
        index_version=UNAUTHORIZED_RETRIEVAL_SAFETY_SEAM,
    ).observe(request=request)

    assert record.disposition is CaseExecutionDisposition.OBSERVED
    assert record.observation is not None
    assert record.observation.answer.outcome is AnswerQuestionOutcome.ANSWERED
    assert record.observation.answer.answer is not None
    assert record.observation.answer.answer.citations
    assert record.observation.context_bundle is not None
    assert timing_samples == [
        (uuid5(UUID("4a000000-0000-0000-0000-000000000478"), "attempt:1"), 125_000_000)
    ]
