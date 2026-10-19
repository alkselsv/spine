from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.authorized_ask import (
    AnswerQuestionCapabilityCommand,
    AskExecutionCommand,
    AskRequestContext,
    PersistedAuthorizedAskService,
)
from spine.application.persistence.answer_question_runs import (
    AnswerQuestionAttemptTerminalResult,
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
from spine.domain.capabilities import (
    AnswerQuestionOutput,
    AnswerQuestionOutcome,
    AnswerQuestionSafeReason,
    AnswerQuestionSafeReasonCode,
    AnswerQuestionScope,
    RunAttemptCorrelation,
)
from spine.domain.capabilities.answer_question_runs import AnswerQuestionAttemptState
from spine.domain.qa_sessions import QAMessageState


WORKSPACE_ID = UUID(int=1)
ENVIRONMENT_ID = UUID(int=2)
SUBJECT_ID = UUID(int=3)
TRACE_ID = UUID(int=4)
COMMAND_ID = UUID(int=5)
SESSION_ID = UUID(int=6)
MESSAGE_ID = UUID(int=7)
RUN_ID = UUID(int=8)
ATTEMPT_ID = UUID(int=9)


def _context() -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
        ),
        origin=ContextOrigin.WORKER,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("answer_question"),
        trace_id=TRACE_ID,
        acting_subject_id=None,
        service_principal_id=UUID(int=10),
        provenance=TrustedContextProvenance(
            issuer_id=UUID(int=11), signature=b"synthetic-trusted-context"
        ),
    )


def _terminal_output() -> AnswerQuestionOutput:
    return AnswerQuestionOutput(
        correlation=RunAttemptCorrelation(
            run_id=RUN_ID,
            attempt_id=ATTEMPT_ID,
            trace_id=TRACE_ID,
        ),
        scope=AnswerQuestionScope(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
        ),
        outcome=AnswerQuestionOutcome.FAILED,
        reason=AnswerQuestionSafeReason(
            code=AnswerQuestionSafeReasonCode.MANDATORY_PROCESSING_FAILED
        ),
    )


@pytest.mark.asyncio
async def test_terminal_activity_replay_returns_persisted_result_without_capability() -> None:
    class Attempts:
        def __init__(self) -> None:
            self.resolved: list[tuple[UUID, UUID]] = []

        async def allocate_or_resume(self, **_: object):
            return SimpleNamespace(
                attempt=SimpleNamespace(
                    attempt_id=ATTEMPT_ID,
                    attempt_number=1,
                    state=AnswerQuestionAttemptState.FAILED,
                )
            )

        async def resolve_terminal_result(self, **values: object):
            self.resolved.append((values["run_id"], values["attempt_id"]))
            return AnswerQuestionAttemptTerminalResult(
                output=_terminal_output(), accepted_answer=None
            )

        async def terminalize(self, **values: object):
            assert values["receipt"] is None
            return await self.resolve_terminal_result(
                run_id=RUN_ID, attempt_id=ATTEMPT_ID
            )

    class Capability:
        async def answer(self, _: AnswerQuestionCapabilityCommand):
            pytest.fail("terminal replay must not repeat retrieval or generation")

        async def cancel(self, _: AnswerQuestionCapabilityCommand):
            pytest.fail("not used")

    class Sessions:
        async def get_message(self, **_: object):
            return SimpleNamespace(state=QAMessageState.TERMINAL)

    attempts = Attempts()
    service = PersistedAuthorizedAskService(
        capability=Capability(),
        sessions=Sessions(),  # type: ignore[arg-type]
        attempts=attempts,  # type: ignore[arg-type]
    )

    result = await service.execute_admitted(
        AskExecutionCommand(
            request_context=AskRequestContext(
                subject_id=SUBJECT_ID,
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
                trace_id=TRACE_ID,
                authorization_generation=1,
            ),
            persistence_context=_context(),
            command_id=COMMAND_ID,
            session_id=SESSION_ID,
            message_id=MESSAGE_ID,
            run_id=RUN_ID,
            question="Question is not stored in the retry key.",
            idempotency_key=IdempotencyKey("terminal-replay"),
            logical_execution_key="durable-ask:terminal-replay",
        )
    )

    assert attempts.resolved == [(RUN_ID, ATTEMPT_ID), (RUN_ID, ATTEMPT_ID)]
    assert result.replay is True
    assert result.output == _terminal_output()
