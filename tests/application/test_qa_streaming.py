from __future__ import annotations

from uuid import UUID

import pytest

from spine.application.authorized_ask import AuthorizedAskResult
from spine.application.qa_events import InMemoryQAEventLog
from spine.application.qa_streaming import AuthorizedQAStreamService
from spine.domain.capabilities import AnswerQuestionOutcome
from spine.domain.qa_events import QAEvent
from tests.api.test_authorized_ask import (
    COMMAND_ID,
    MESSAGE_ID,
    SESSION_ID,
    safe_output,
)


RUN_ID = UUID("40000000-0000-0000-0000-000000000001")


class FailAfterFirstPersistLog(InMemoryQAEventLog):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next = True

    async def append(self, event: QAEvent) -> QAEvent:
        persisted = await super().append(event)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("synthetic lost response")
        return persisted


@pytest.mark.asyncio
async def test_publishing_safe_ask_result_creates_resumable_terminal_stream() -> None:
    log = InMemoryQAEventLog()
    service = AuthorizedQAStreamService(event_log=log)
    result = AuthorizedAskResult(
        command_id=COMMAND_ID,
        session_id=SESSION_ID,
        message_id=MESSAGE_ID,
        output=safe_output(AnswerQuestionOutcome.ABSTAINED),
        accepted_answer=None,
    )

    await service.publish_result(result)

    stream = service.open_stream(
        workspace_id=result.output.scope.workspace_id,
        environment_id=result.output.scope.environment_id,
        session_id=result.session_id,
        message_id=result.message_id,
        run_id=RUN_ID,
        trace_id=result.output.correlation.trace_id,
        disclosure_authorizer=lambda _receipt: False,
    )
    replay = await stream.replay()

    assert tuple(event.type.value for event in replay.events) == (
        "run_started",
        "stage_changed",
        "abstained",
    )
    assert replay.terminal is not None
    assert replay.terminal.type.value == "abstained"


@pytest.mark.asyncio
async def test_publishing_same_ask_result_is_idempotent_and_does_not_duplicate_terminal() -> None:
    log = InMemoryQAEventLog()
    service = AuthorizedQAStreamService(event_log=log)
    result = AuthorizedAskResult(
        command_id=COMMAND_ID,
        session_id=SESSION_ID,
        message_id=MESSAGE_ID,
        output=safe_output(AnswerQuestionOutcome.CANCELLED),
        accepted_answer=None,
    )

    await service.publish_result(result)
    await service.publish_result(result)

    events = await log.read(
        workspace_id=result.output.scope.workspace_id,
        environment_id=result.output.scope.environment_id,
        session_id=result.session_id,
        message_id=result.message_id,
        run_id=RUN_ID,
    )
    assert tuple(event.type.value for event in events) == (
        "run_started",
        "stage_changed",
        "cancelled",
    )


@pytest.mark.asyncio
async def test_retry_after_partial_event_persistence_has_one_logical_effect() -> None:
    log = FailAfterFirstPersistLog()
    service = AuthorizedQAStreamService(event_log=log)
    result = AuthorizedAskResult(
        command_id=COMMAND_ID,
        session_id=SESSION_ID,
        message_id=MESSAGE_ID,
        output=safe_output(AnswerQuestionOutcome.ABSTAINED),
        accepted_answer=None,
    )

    with pytest.raises(RuntimeError, match="lost response"):
        await service.publish_result(result)
    await service.publish_result(result)

    events = await log.read(
        workspace_id=result.output.scope.workspace_id,
        environment_id=result.output.scope.environment_id,
        session_id=result.session_id,
        message_id=result.message_id,
        run_id=RUN_ID,
    )
    assert tuple(event.type.value for event in events) == (
        "run_started",
        "stage_changed",
        "abstained",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_types"),
    (
        (AnswerQuestionOutcome.CLARIFICATION_REQUIRED, ("run_started", "stage_changed", "clarification_required", "completed")),
        (AnswerQuestionOutcome.DENIED, ("run_started", "stage_changed", "denied")),
        (AnswerQuestionOutcome.FAILED, ("run_started", "stage_changed", "failed")),
        (AnswerQuestionOutcome.CANCELLED, ("run_started", "stage_changed", "cancelled")),
    ),
)
async def test_publishing_each_safe_ask_outcome_emits_only_safe_event_types(
    outcome: AnswerQuestionOutcome, expected_types: tuple[str, ...]
) -> None:
    log = InMemoryQAEventLog()
    service = AuthorizedQAStreamService(event_log=log)
    result = AuthorizedAskResult(
        command_id=COMMAND_ID,
        session_id=SESSION_ID,
        message_id=MESSAGE_ID,
        output=safe_output(outcome),
        accepted_answer=None,
    )

    await service.publish_result(result)

    events = await log.read(
        workspace_id=result.output.scope.workspace_id,
        environment_id=result.output.scope.environment_id,
        session_id=result.session_id,
        message_id=result.message_id,
        run_id=RUN_ID,
    )
    assert tuple(event.type.value for event in events) == expected_types


@pytest.mark.asyncio
async def test_completion_wins_a_later_cancellation_without_a_second_terminal_event() -> None:
    log = InMemoryQAEventLog()
    service = AuthorizedQAStreamService(event_log=log)
    completed = AuthorizedAskResult(
        command_id=COMMAND_ID,
        session_id=SESSION_ID,
        message_id=MESSAGE_ID,
        output=safe_output(AnswerQuestionOutcome.ABSTAINED),
        accepted_answer=None,
    )
    cancelled = AuthorizedAskResult(
        command_id=COMMAND_ID,
        session_id=SESSION_ID,
        message_id=MESSAGE_ID,
        output=safe_output(AnswerQuestionOutcome.CANCELLED),
        accepted_answer=None,
    )

    await service.publish_result(completed)
    winner = await service.publish_result(cancelled)

    assert winner is not None and winner.type.value == "abstained"
