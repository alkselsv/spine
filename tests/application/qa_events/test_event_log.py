from __future__ import annotations

from uuid import UUID

import pytest

from spine.application.qa_events import (
    CandidateOutputStreamingError,
    CursorScopeError,
    InMemoryQAEventLog,
    ResumeCursorError,
)
from spine.domain.qa_events import (
    AnswerDeltaPayload,
    CompletedPayload,
    DeniedPayload,
    DisclosureReceipt,
    QAEvent,
    QAEventType,
    RunStartedPayload,
    StageChangedPayload,
)


class FailAfterFirstPersistLog(InMemoryQAEventLog):
    """Simulate a lost response after the emission is durably recorded."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next = True

    async def append(self, event: QAEvent) -> QAEvent:
        persisted = await super().append(event)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("synthetic lost append response")
        return persisted


def uid(value: int) -> UUID:
    return UUID(int=value)


def event(
    event_type: QAEventType,
    sequence: int,
    *,
    event_id: int | None = None,
    workspace_id: int = 2,
    payload: object | None = None,
) -> QAEvent:
    payload = payload or {
        QAEventType.RUN_STARTED: RunStartedPayload(stage="accepted"),
        QAEventType.STAGE_CHANGED: StageChangedPayload(stage="grounding"),
    }[event_type]
    return QAEvent(
        event_id=uid(event_id or 100 + sequence),
        sequence=sequence,
        session_id=uid(1),
        workspace_id=uid(workspace_id),
        environment_id=uid(3),
        message_id=uid(4),
        run_id=uid(5),
        trace_id=uid(6),
        type=event_type,
        payload=payload,
    )


@pytest.mark.asyncio
async def test_event_log_appends_events_in_sequence() -> None:
    log = InMemoryQAEventLog()
    started = event(QAEventType.RUN_STARTED, 1)
    completed = event(
        QAEventType.COMPLETED,
        2,
        payload=CompletedPayload(outcome="answered"),
    )

    assert await log.append(started) == started
    assert await log.append(completed) == completed
    assert await log.read(
        workspace_id=uid(2), environment_id=uid(3), session_id=uid(1), run_id=uid(5)
    ) == (
        started,
        completed,
    )


@pytest.mark.asyncio
async def test_event_log_replays_an_identical_emission_without_a_second_effect() -> None:
    log = InMemoryQAEventLog()
    started = event(QAEventType.RUN_STARTED, 1)

    first = await log.append(started)
    replay = await log.append(started)

    assert replay == first
    assert await log.read(
        workspace_id=uid(2), environment_id=uid(3), session_id=uid(1), run_id=uid(5)
    ) == (started,)


@pytest.mark.asyncio
async def test_event_log_retry_after_a_lost_response_replays_one_emission() -> None:
    log = FailAfterFirstPersistLog()
    started = event(QAEventType.RUN_STARTED, 1)

    with pytest.raises(RuntimeError, match="lost append response"):
        await log.append(started)

    assert await log.append(started) == started
    assert await log.read(
        workspace_id=uid(2), environment_id=uid(3), session_id=uid(1), run_id=uid(5)
    ) == (started,)


@pytest.mark.asyncio
async def test_event_log_rejects_sequence_gaps() -> None:
    log = InMemoryQAEventLog()
    await log.append(event(QAEventType.RUN_STARTED, 1))

    with pytest.raises(ResumeCursorError, match="sequence"):
        await log.append(event(QAEventType.STAGE_CHANGED, 3))


@pytest.mark.asyncio
async def test_event_log_rejects_event_identity_reuse() -> None:
    log = InMemoryQAEventLog()
    await log.append(event(QAEventType.RUN_STARTED, 1))
    await log.append(event(QAEventType.STAGE_CHANGED, 2))

    conflicting = event(QAEventType.STAGE_CHANGED, 2, event_id=101)
    with pytest.raises(ResumeCursorError, match="identity"):
        await log.append(conflicting)


@pytest.mark.asyncio
async def test_resume_returns_ordered_events_after_last_event_id() -> None:
    log = InMemoryQAEventLog(max_replay=2)
    events = [event(QAEventType.RUN_STARTED, 1), event(QAEventType.STAGE_CHANGED, 2)]
    for item in events:
        await log.append(item)

    replay = await log.read_after(
        workspace_id=uid(2),
        environment_id=uid(3),
        session_id=uid(1),
        run_id=uid(5),
        last_event_id=events[0].event_id,
        last_sequence=events[0].sequence,
    )

    assert replay == (events[1],)


@pytest.mark.asyncio
async def test_resume_does_not_duplicate_an_identical_cursor() -> None:
    log = InMemoryQAEventLog()
    events = [event(QAEventType.RUN_STARTED, 1), event(QAEventType.STAGE_CHANGED, 2)]
    for item in events:
        await log.append(item)

    first = await log.read_after(
        workspace_id=uid(2),
        environment_id=uid(3),
        session_id=uid(1),
        run_id=uid(5),
        last_event_id=events[0].event_id,
    )
    retry = await log.read_after(
        workspace_id=uid(2),
        environment_id=uid(3),
        session_id=uid(1),
        run_id=uid(5),
        last_event_id=events[0].event_id,
    )

    assert first == retry == (events[1],)


@pytest.mark.asyncio
async def test_resume_rejects_an_unbounded_limit() -> None:
    log = InMemoryQAEventLog(max_replay=2)
    await log.append(event(QAEventType.RUN_STARTED, 1))

    with pytest.raises(ResumeCursorError, match="bounded"):
        await log.read_after(
            workspace_id=uid(2),
            environment_id=uid(3),
            session_id=uid(1),
            run_id=uid(5),
            limit=3,
        )


@pytest.mark.asyncio
async def test_resume_rejects_unknown_and_cross_scope_cursors_safely() -> None:
    log = InMemoryQAEventLog()
    started = event(QAEventType.RUN_STARTED, 1)
    await log.append(started)

    with pytest.raises(ResumeCursorError, match="cursor"):
        await log.read_after(
            workspace_id=uid(2),
            environment_id=uid(3),
            session_id=uid(1),
            run_id=uid(5),
            last_event_id=uid(999),
        )


@pytest.mark.asyncio
async def test_resume_rejects_a_cursor_from_another_workspace() -> None:
    log = InMemoryQAEventLog()
    started = event(QAEventType.RUN_STARTED, 1)
    await log.append(started)

    with pytest.raises(CursorScopeError, match="cursor"):
        await log.read_after(
            workspace_id=uid(99),
            environment_id=uid(3),
            session_id=uid(1),
            run_id=uid(5),
            last_event_id=started.event_id,
        )


@pytest.mark.asyncio
async def test_stream_rehydrates_terminal_event_after_disconnect() -> None:
    log = InMemoryQAEventLog()
    started = event(QAEventType.RUN_STARTED, 1)
    completed = event(
        QAEventType.COMPLETED,
        2,
        payload=CompletedPayload(outcome="answered"),
    )
    await log.append(started)
    await log.append(completed)
    stream = log.open_stream(
        workspace_id=uid(2),
        environment_id=uid(3),
        session_id=uid(1),
        run_id=uid(5),
        trace_id=uid(6),
    )

    replay = await stream.replay(last_event_id=started.event_id)
    terminal = await stream.rehydrate_terminal()

    assert replay.events == (completed,)
    assert replay.terminal == completed
    assert terminal == completed


@pytest.mark.asyncio
async def test_heartbeat_retry_is_idempotent() -> None:
    log = InMemoryQAEventLog()
    await log.append(event(QAEventType.RUN_STARTED, 1))
    stream = log.open_stream(
        workspace_id=uid(2),
        environment_id=uid(3),
        session_id=uid(1),
        run_id=uid(5),
        trace_id=uid(6),
        event_id_factory=iter((uid(200), uid(201), uid(202))).__next__,
    )

    heartbeat = await stream.heartbeat()
    replayed_heartbeat = await stream.heartbeat()

    assert heartbeat.type is QAEventType.HEARTBEAT
    assert replayed_heartbeat == heartbeat
    assert tuple(item.type for item in await log.read(
        workspace_id=uid(2), environment_id=uid(3), session_id=uid(1), run_id=uid(5)
    )) == (QAEventType.RUN_STARTED, QAEventType.HEARTBEAT)


@pytest.mark.asyncio
async def test_cancellation_retry_is_idempotent() -> None:
    log = InMemoryQAEventLog()
    await log.append(event(QAEventType.RUN_STARTED, 1))
    stream = log.open_stream(
        workspace_id=uid(2),
        environment_id=uid(3),
        session_id=uid(1),
        run_id=uid(5),
        trace_id=uid(6),
        event_id_factory=iter((uid(201), uid(202))).__next__,
    )

    cancelled = await stream.cancel()
    replayed_cancel = await stream.cancel()

    assert cancelled.type is QAEventType.CANCELLED
    assert replayed_cancel == cancelled


@pytest.mark.asyncio
async def test_authorization_change_replaces_protected_emission_with_safe_denial() -> None:
    log = InMemoryQAEventLog()
    started = event(QAEventType.RUN_STARTED, 1)
    await log.append(started)
    protected = event(
        QAEventType.ANSWER_DELTA,
        2,
        payload=AnswerDeltaPayload(
            delta="secret accepted fragment",
            disclosure=DisclosureReceipt(
                receipt_id=uid(70),
                accepted_output_id=uid(71),
                validation_reference=uid(72),
            ),
        ),
    )
    await log.append(protected)
    completed = event(
        QAEventType.COMPLETED, 3, payload=CompletedPayload(outcome="answered")
    )
    await log.append(completed)
    authorization = {"allowed": True}
    stream = log.open_stream(
        workspace_id=uid(2),
        environment_id=uid(3),
        session_id=uid(1),
        run_id=uid(5),
        trace_id=uid(6),
        disclosure_authorizer=lambda _receipt: authorization["allowed"],
    )

    initially_authorized = await stream.replay()
    authorization["allowed"] = False
    replay = await stream.replay(last_event_id=started.event_id)
    rendered = replay.model_dump_json()

    assert initially_authorized.events == (started, protected, completed)
    assert tuple(item.type for item in replay.events) == (QAEventType.DENIED,)
    assert replay.terminal is not None and replay.terminal.type is QAEventType.DENIED
    assert "secret accepted fragment" not in rendered
    assert "candidate" not in rendered


@pytest.mark.asyncio
async def test_virtual_denial_cursor_replays_the_same_terminal_safely() -> None:
    log = InMemoryQAEventLog()
    await log.append(event(QAEventType.RUN_STARTED, 1))
    protected = event(
        QAEventType.ANSWER_DELTA,
        2,
        payload=AnswerDeltaPayload(
            delta="secret accepted fragment",
            disclosure=DisclosureReceipt(
                receipt_id=uid(70),
                accepted_output_id=uid(71),
                validation_reference=uid(72),
            ),
        ),
    )
    await log.append(protected)
    stream = log.open_stream(
        workspace_id=uid(2),
        environment_id=uid(3),
        session_id=uid(1),
        run_id=uid(5),
        trace_id=uid(6),
        disclosure_authorizer=lambda _receipt: False,
    )

    first = await stream.replay()
    reconnected = log.open_stream(
        workspace_id=uid(2),
        environment_id=uid(3),
        session_id=uid(1),
        run_id=uid(5),
        trace_id=uid(6),
        disclosure_authorizer=lambda _receipt: False,
    )
    second = await reconnected.replay(last_event_id=first.events[-1].event_id)

    assert first.terminal is not None
    assert second.events == ()
    assert second.terminal == first.terminal


@pytest.mark.asyncio
async def test_heartbeat_retry_is_idempotent_after_reconnect() -> None:
    log = InMemoryQAEventLog()
    await log.append(event(QAEventType.RUN_STARTED, 1))
    first_stream = log.open_stream(
        workspace_id=uid(2),
        environment_id=uid(3),
        session_id=uid(1),
        run_id=uid(5),
        trace_id=uid(6),
        event_id_factory=iter((uid(200), uid(201))).__next__,
    )
    second_stream = log.open_stream(
        workspace_id=uid(2),
        environment_id=uid(3),
        session_id=uid(1),
        run_id=uid(5),
        trace_id=uid(6),
        event_id_factory=iter((uid(202), uid(203))).__next__,
    )

    first = await first_stream.heartbeat(emission_id=uid(800))
    retry = await second_stream.heartbeat(emission_id=uid(800))

    assert retry == first


@pytest.mark.asyncio
async def test_stream_rejects_candidate_output_before_the_disclosure_gate() -> None:
    log = InMemoryQAEventLog()
    stream = log.open_stream(
        workspace_id=uid(2),
        environment_id=uid(3),
        session_id=uid(1),
        run_id=uid(5),
        trace_id=uid(6),
    )

    with pytest.raises(CandidateOutputStreamingError):
        await stream.append_candidate("candidate answer")
