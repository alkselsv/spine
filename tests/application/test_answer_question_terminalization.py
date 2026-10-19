from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid5

import pytest

from spine.application.answer_question_attempts import PersistedAnswerQuestionAttemptService
from spine.application.persistence.answer_question_runs import (
    AnswerQuestionAttemptAllocation,
    CanonicalQATerminalizationReceipt,
)
from spine.application.persistence.ask_admission import QA_ASK_ADMISSION_OPERATION
from spine.application.persistence.errors import AnswerQuestionRunConflictError
from spine.application.persistence.context import PersistenceOperation
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.qa_sessions import PersistedQASessionService
from spine.domain.capabilities import (
    AnswerAuthorizationOutcome,
    AnswerQuestionOutcome,
    AnswerQuestionOutput,
    AnswerQuestionSafeReason,
    AnswerQuestionSafeReasonCode,
    AnswerQuestionScope,
    RunAttemptCorrelation,
)
from spine.domain.capabilities.answer_disclosure import AnswerDisclosureRecord
from tests.application.persistence.test_ask_admission import (
    NOW,
    RUN_ID,
    TRACE_ID,
    WORKSPACE_ID,
    ENVIRONMENT_ID,
    _command,
    _context,
    _persistence,
)
from tests.domain.capabilities.test_answer_question_runs import _pin


@pytest.mark.parametrize(
    ("outcome", "safe_reason", "authorization", "attempt_state"),
    [
        (
            AnswerQuestionOutcome.DENIED,
            AnswerQuestionSafeReasonCode.AUTHORIZATION_DENIED,
            AnswerAuthorizationOutcome.DENIED,
            "succeeded",
        ),
        (
            AnswerQuestionOutcome.ABSTAINED,
            AnswerQuestionSafeReasonCode.NO_USABLE_EVIDENCE,
            AnswerAuthorizationOutcome.ALLOWED,
            "succeeded",
        ),
        (
            AnswerQuestionOutcome.CANCELLED,
            AnswerQuestionSafeReasonCode.CANCELLED,
            AnswerAuthorizationOutcome.ALLOWED,
            "cancelled",
        ),
        (
            AnswerQuestionOutcome.FAILED,
            AnswerQuestionSafeReasonCode.MANDATORY_PROCESSING_FAILED,
            AnswerAuthorizationOutcome.ALLOWED,
            "failed",
        ),
    ],
)
@pytest.mark.parametrize("conflict", (False, True))
@pytest.mark.asyncio
async def test_terminalization_commits_safe_attempt_run_and_message_together(
    outcome: AnswerQuestionOutcome,
    safe_reason: AnswerQuestionSafeReasonCode,
    authorization: AnswerAuthorizationOutcome,
    attempt_state: str,
    conflict: bool,
) -> None:
    persistence, boundary = await _persistence()
    admission_context = _context(boundary, operation=QA_ASK_ADMISSION_OPERATION)
    async with persistence.uow_factory(admission_context) as uow:
        admitted = await uow.ask_admissions.admit(_command())
        run = await uow.answer_question_runs.resolve_run(admitted.run_id)
        await uow.answer_question_runs.start_run(run.begin_execution())
        pin = _pin(WORKSPACE_ID, ENVIRONMENT_ID)
        allocation = await uow.answer_question_runs.allocate_or_resume_attempt(
            AnswerQuestionAttemptAllocation(
                run_id=admitted.run_id,
                logical_execution_key="durable-ask:terminalize-denied",
                execution_pin=pin,
                input_artifact_ids=run.input_artifact_ids,
            )
        )
        attempt = allocation.attempt
        output = AnswerQuestionOutput(
            correlation=RunAttemptCorrelation(
                run_id=admitted.run_id,
                attempt_id=attempt.attempt_id,
                trace_id=TRACE_ID,
            ),
            scope=AnswerQuestionScope(
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
            ),
            outcome=outcome,
            reason=AnswerQuestionSafeReason(
                code=safe_reason
            ),
        )
        record = AnswerDisclosureRecord(
            request_id=uuid5(admitted.run_id, "context-request"),
            request_digest="a" * 64,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            run_id=admitted.run_id,
            attempt_id=attempt.attempt_id,
            outcome=outcome,
            safe_output=output,
            terminal_output=output,
            created_at=NOW,
        )
        await uow.answer_disclosures.save(record)
        await uow.commit()

    terminalizer = PersistedAnswerQuestionAttemptService(
        uow_factory=persistence.uow_factory,
        execution=object(),  # not used during finalization
        clock=lambda: datetime(2027, 1, 1, tzinfo=timezone.utc),
        event_id_factory=lambda: UUID(int=901),
    )
    session_service = PersistedQASessionService(
        repository=object(),
        disclosure_eligibility=object(),
        clock=lambda: datetime(2027, 1, 1, tzinfo=timezone.utc),
        command_id_factory=lambda: UUID(int=902),
    )
    receipt = CanonicalQATerminalizationReceipt(
        run_id=admitted.run_id,
        attempt_id=attempt.attempt_id,
        attempt_number=attempt.attempt_number,
        execution_pin=pin,
        output=output,
        context_bundle_ref=None,
        agent_result_ref=None,
        agent_result=None,
        output_artifact_ids=(),
        candidate_input_artifact_id=None,
        candidate_output_artifacts=(),
        disclosure_record=record,
        authorization=authorization,
    )

    if conflict:
        conflicting_outcome = (
            AnswerQuestionOutcome.ABSTAINED
            if outcome is AnswerQuestionOutcome.DENIED
            else AnswerQuestionOutcome.DENIED
        )
        async with persistence.uow_factory(
            _context(boundary, operation=PersistenceOperation("qa_sessions"))
        ) as uow:
            await session_service.complete_message_in_transaction(
                repository=uow.qa_sessions,
                context=_context(boundary, operation=PersistenceOperation("qa_sessions")),
                session_id=admitted.session_id,
                message_id=admitted.message_id,
                answer=None,
                outcome=conflicting_outcome,
                cancelled=False,
                idempotency_key=IdempotencyKey("preexisting-terminal-message"),
            )
            await uow.commit()

    if conflict:
        with pytest.raises(AnswerQuestionRunConflictError):
            await terminalizer.terminalize(
                context=_context(boundary, operation=PersistenceOperation("qa_sessions")),
                session_id=admitted.session_id,
                message_id=admitted.message_id,
                idempotency_key="durable-ask:terminalize-denied",
                sessions=session_service,
                receipt=receipt,
            )
    else:
        result = await terminalizer.terminalize(
            context=_context(boundary, operation=PersistenceOperation("qa_sessions")),
            session_id=admitted.session_id,
            message_id=admitted.message_id,
            idempotency_key="durable-ask:terminalize-denied",
            sessions=session_service,
            receipt=receipt,
        )

    async with persistence.uow_factory(
        _context(boundary, operation=PersistenceOperation("qa_sessions"))
    ) as uow:
        run = await uow.answer_question_runs.resolve_run(RUN_ID)
        stored_attempt = await uow.answer_question_runs.resolve_attempt(attempt.attempt_id)
        message = await uow.qa_sessions.resolve_message(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            session_id=admitted.session_id,
            message_id=admitted.message_id,
        )
        await uow.commit()

    assert message is not None
    if conflict:
        assert run.state.value == "running"
        assert run.final_attempt_id is None
        assert stored_attempt.state.value == "running"
        assert message.state.value == "terminal"
        assert message.terminal_outcome is not outcome
    else:
        assert result.output == output
        assert run.state.value == "completed"
        assert run.final_attempt_id == attempt.attempt_id
        assert run.outcome is outcome
        assert stored_attempt.state.value == attempt_state
        assert stored_attempt.outcome is outcome
        assert message.state.value == "terminal"
        assert message.terminal_outcome is outcome

        replay = await terminalizer.terminalize(
            context=_context(boundary, operation=PersistenceOperation("qa_sessions")),
            session_id=admitted.session_id,
            message_id=admitted.message_id,
            idempotency_key="durable-ask:terminalize-denied",
            sessions=session_service,
            receipt=None,
        )
        assert replay.output == output
