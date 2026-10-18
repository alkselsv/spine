from __future__ import annotations

from uuid import UUID

import pytest

from spine.application.authorized_ask import (
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
from spine.application.production_qa_capability import (
    AnswerQuestionContextReferences,
    ProductionAnswerQuestionCapability,
)
from spine.domain.capabilities import AnswerQuestionOutcome, AnswerQuestionSafeReasonCode
from spine.domain.common import EnvironmentKind


WORKSPACE = UUID(int=1)
ENVIRONMENT = UUID(int=2)
SUBJECT = UUID(int=3)
TRACE = UUID(int=4)
SESSION = UUID(int=5)
MESSAGE = UUID(int=6)
RUN = UUID(int=7)
COMMAND_ID = UUID(int=8)


def command(*, run_id: UUID | None = RUN) -> AnswerQuestionCapabilityCommand:
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
            origin=ContextOrigin.INTERACTIVE,
            purpose=PersistencePurpose("knowledge_control"),
            operation=PersistenceOperation("answer_question"),
            trace_id=TRACE,
            acting_subject_id=SUBJECT,
            service_principal_id=None,
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
    assert result.output.reason.code is AnswerQuestionSafeReasonCode.MANDATORY_PROCESSING_FAILED
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
