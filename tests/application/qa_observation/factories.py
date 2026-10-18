from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from spine.application.qa_observation.contracts import (
    AdmittedExecutionIdentity,
    CaptureEnvelope,
    SameExecutionCapture,
)
from spine.domain.capabilities import (
    AnswerQuestionInput,
    AnswerQuestionOutcome,
    AnswerQuestionOutput,
    AnswerQuestionSafeReason,
    AnswerQuestionSafeReasonCode,
    AnswerQuestionScope,
    QuestionSubject,
    RunAttemptCorrelation,
)
from spine.domain.capabilities.answer_disclosure import AnswerDisclosureRecord
from spine.domain.common import ActorKind, ActorRef
from spine.memory.contracts import (
    ContextOutcome,
    ContextReasonCode,
    ContextResponse,
    ProjectionRetrievalResult,
    RetrievalResult,
)


def execution_identity() -> AdmittedExecutionIdentity:
    return AdmittedExecutionIdentity(
        command_id=UUID("10000000-0000-0000-0000-000000000471"),
        run_id=UUID("20000000-0000-0000-0000-000000000471"),
        session_id=UUID("30000000-0000-0000-0000-000000000471"),
        message_id=UUID("40000000-0000-0000-0000-000000000471"),
        workspace_id=UUID("50000000-0000-0000-0000-000000000471"),
        environment_id=UUID("60000000-0000-0000-0000-000000000471"),
        requester=ActorRef(
            kind=ActorKind.HUMAN,
            id=UUID("70000000-0000-0000-0000-000000000471"),
        ),
        attempt_id=UUID("80000000-0000-0000-0000-000000000471"),
        attempt_number=1,
    )


def same_execution_capture() -> SameExecutionCapture:
    identity = execution_identity()
    question = AnswerQuestionInput(
        subject=QuestionSubject(text="Synthetic question"),
        scope=AnswerQuestionScope(
            workspace_id=identity.workspace_id,
            environment_id=identity.environment_id,
        ),
        correlation=RunAttemptCorrelation(
            run_id=identity.run_id,
            attempt_id=identity.attempt_id,
            trace_id=UUID("a0000000-0000-0000-0000-000000000471"),
        ),
    )
    answer = AnswerQuestionOutput(
        correlation=question.correlation,
        scope=question.scope,
        outcome=AnswerQuestionOutcome.DENIED,
        reason=AnswerQuestionSafeReason(
            code=AnswerQuestionSafeReasonCode.AUTHORIZATION_DENIED
        ),
    )
    return SameExecutionCapture(
        question=question,
        context_response=ContextResponse(
            outcome=ContextOutcome.DENIED,
            reason_code=ContextReasonCode.AUTHORIZATION_DENIED,
        ),
        context_bundle=None,
        projection_retrieval=ProjectionRetrievalResult(
            outcome="missing",
            command_identity="synthetic-capture",
            command_digest="b" * 64,
        ),
        retrieval=RetrievalResult(retrieval_strategy="synthetic"),
        answer=answer,
        disclosure=AnswerDisclosureRecord(
            request_id=UUID("b0000000-0000-0000-0000-000000000471"),
            request_digest="c" * 64,
            workspace_id=identity.workspace_id,
            environment_id=identity.environment_id,
            run_id=identity.run_id,
            attempt_id=identity.attempt_id,
            outcome=answer.outcome,
            safe_output=answer,
            created_at=datetime(2026, 10, 18, tzinfo=timezone.utc),
        ),
    )


def capture_envelope(**overrides: object) -> CaptureEnvelope:
    identity = execution_identity()
    values = {
        "capture_id": UUID("c0000000-0000-0000-0000-000000000471"),
        "subscription_id": UUID("d0000000-0000-0000-0000-000000000471"),
        "identity": identity,
        "capture": same_execution_capture(),
    }
    return CaptureEnvelope(**(values | overrides))
