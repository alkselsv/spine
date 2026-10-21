from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from spine.api.authorized_ask import install_authorized_ask_routes
from spine.api.qa_stream import _canonical_terminal_event, install_qa_stream_routes
from spine.application.qa_events import InMemoryQAEventLog
from spine.application.qa_streaming import AuthorizedQAStreamService
from spine.application.telemetry import (
    OperationalComponent,
    OperationalObservationEmitter,
    OperationalObservationRegistry,
    RecordingOperationalObservationSink,
)
from tests.api.test_authorized_ask import (
    AlwaysRetainPolicy,
    FakeAskService,
    SESSION_ID,
    TRACE_ID,
    answered_result,
    headers,
)
from tests.api.test_authorization import authorized_app
from spine.domain.capabilities import AnswerQuestionOutcome
from spine.domain.qa_sessions import QAMessageState


def _event_frames(value: str) -> list[dict[str, object]]:
    frames: list[dict[str, object]] = []
    for frame in value.strip().split("\n\n"):
        fields = dict(
            line.split(": ", 1)
            for line in frame.splitlines()
            if ": " in line
        )
        if "data" in fields:
            payload = json.loads(fields["data"])
            frames.append({"id": fields.get("id"), "event": fields.get("event"), "data": payload})
    return frames


def _app(
    *,
    allowed: dict[str, bool],
    log: InMemoryQAEventLog,
    operational_observer: OperationalObservationEmitter | None = None,
) -> httpx.ASGITransport:
    ask_service = FakeAskService(answered_result(), [])
    stream_service = AuthorizedQAStreamService(event_log=log)
    application = authorized_app([])
    install_authorized_ask_routes(
        application,
        service=ask_service,
        stream_service=stream_service,
    )
    install_qa_stream_routes(
        application,
        service=stream_service,
        disclosure_authorizer=lambda _receipt, _context: allowed["content"],
        operational_observer=operational_observer,
    )
    return httpx.ASGITransport(app=application)


@pytest.mark.asyncio
async def test_empty_running_stream_emits_resumable_heartbeat() -> None:
    log = InMemoryQAEventLog()
    transport = _app(allowed={"content": False}, log=log)
    message_id = UUID(int=701)
    run_id = UUID(int=702)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/qa/sessions/{SESSION_ID}/messages/{message_id}/events",
            params={"run_id": str(run_id)},
            headers=headers(),
        )

    frames = _event_frames(response.text)
    assert response.status_code == 200
    assert [frame["event"] for frame in frames] == ["heartbeat"]
    assert frames[0]["id"]


@pytest.mark.asyncio
async def test_worker_committed_message_rehydrates_content_free_terminal_event() -> None:
    message_id = UUID(int=701)
    run_id = UUID(int=702)

    class Sessions:
        async def get_message(self, **_: object) -> object:
            return SimpleNamespace(
                workspace_id=UUID(int=1),
                environment_id=UUID(int=2),
                session_id=SESSION_ID,
                message_id=message_id,
                run_id=run_id,
                trace_id=TRACE_ID,
                version=2,
                state=QAMessageState.TERMINAL,
                terminal_outcome=AnswerQuestionOutcome.ANSWERED,
            )

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(qa_session_service=Sessions()))
    )
    request_context = SimpleNamespace(
        acting_subject_id=UUID(int=3), authorization_generation=1
    )

    terminal = await _canonical_terminal_event(
        request=request,  # type: ignore[arg-type]
        persistence_context=object(),  # type: ignore[arg-type]
        request_context=request_context,  # type: ignore[arg-type]
        session_id=SESSION_ID,
        message_id=message_id,
        run_id=run_id,
    )

    assert terminal is not None
    assert terminal.sequence == 2
    assert terminal.type.value == "completed"
    assert terminal.payload.model_dump() == {"outcome": "answered"}


@pytest.mark.asyncio
async def test_sse_streams_accepted_ask_events_and_resumes_after_last_event_id() -> None:
    log = InMemoryQAEventLog()
    transport = _app(allowed={"content": True}, log=log)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ask = await client.post(
            "/api/v1/ask",
            json={"schema_version": 1, "session_id": str(SESSION_ID), "question": "Which policy applies?"},
            headers={**headers(), "Idempotency-Key": "stream-ask"},
        )
        run_id = ask.json()["result"]["run_id"]
        response = await client.get(
            f"/api/v1/qa/sessions/{SESSION_ID}/messages/{ask.json()['result']['message_id']}/events",
            params={"run_id": run_id},
            headers=headers(),
        )

        frames = _event_frames(response.text)
        resumed = await client.get(
            f"/api/v1/qa/sessions/{SESSION_ID}/messages/{ask.json()['result']['message_id']}/events",
            params={"run_id": run_id},
            headers={**headers(), "Last-Event-ID": str(frames[0]["id"])},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert [frame["event"] for frame in frames] == [
        "run_started",
        "stage_changed",
        "answer_delta",
        "citation_added",
        "completed",
    ]
    assert frames[2]["data"]["payload"]["delta"] == "The policy applies to this customer."
    assert [frame["event"] for frame in _event_frames(resumed.text)] == [
        "stage_changed",
        "answer_delta",
        "citation_added",
        "completed",
    ]


@pytest.mark.asyncio
async def test_sse_delivery_emits_safe_terminal_observation_after_http_stream() -> None:
    registry = OperationalObservationRegistry()
    sink = RecordingOperationalObservationSink(registry)
    observer = OperationalObservationEmitter(
        registry=registry,
        sink=sink,
        policy=AlwaysRetainPolicy(),
        clock=lambda: datetime(2026, 10, 16, tzinfo=timezone.utc),
        observation_id_source=lambda: UUID(int=361),
    )
    log = InMemoryQAEventLog()
    transport = _app(
        allowed={"content": True},
        log=log,
        operational_observer=observer,
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ask = await client.post(
            "/api/v1/ask",
            json={"schema_version": 1, "session_id": str(SESSION_ID), "question": "Which policy applies?"},
            headers={**headers(), "Idempotency-Key": "sse-observation"},
        )
        await client.get(
            f"/api/v1/qa/sessions/{SESSION_ID}/messages/{ask.json()['result']['message_id']}/events",
            params={"run_id": ask.json()["result"]["run_id"]},
            headers=headers(),
        )

    assert len(sink.observations) == 1
    assert sink.observations[0].component is OperationalComponent.SSE
    assert sink.observations[0].trace_id == TRACE_ID
    assert "The policy applies" not in sink.observations[0].model_dump_json()


@pytest.mark.asyncio
async def test_sse_revalidates_protected_events_and_emits_only_safe_denial() -> None:
    log = InMemoryQAEventLog()
    allowed = {"content": True}
    transport = _app(allowed=allowed, log=log)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ask = await client.post(
            "/api/v1/ask",
            json={"schema_version": 1, "session_id": str(SESSION_ID), "question": "Which policy applies?"},
            headers={**headers(), "Idempotency-Key": "revocation-ask"},
        )
        message_id = ask.json()["result"]["message_id"]
        run_id = ask.json()["result"]["run_id"]
        first = await client.get(
            f"/api/v1/qa/sessions/{SESSION_ID}/messages/{message_id}/events",
            params={"run_id": run_id},
            headers=headers(),
        )
        allowed["content"] = False
        revoked = await client.get(
            f"/api/v1/qa/sessions/{SESSION_ID}/messages/{message_id}/events",
            params={"run_id": run_id},
            headers={
                **headers(),
                "Last-Event-ID": _event_frames(first.text)[0]["id"],
            },
        )

    assert "The policy applies to this customer." not in revoked.text
    assert [frame["event"] for frame in _event_frames(revoked.text)] == [
        "stage_changed",
        "denied",
    ]


@pytest.mark.asyncio
async def test_sse_rejects_a_cursor_from_another_message_without_event_leakage() -> None:
    log = InMemoryQAEventLog()
    transport = _app(allowed={"content": True}, log=log)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ask = await client.post(
            "/api/v1/ask",
            json={"schema_version": 1, "session_id": str(SESSION_ID), "question": "Which policy applies?"},
            headers={**headers(), "Idempotency-Key": "cursor-scope-ask"},
        )
        first = await client.get(
            f"/api/v1/qa/sessions/{SESSION_ID}/messages/{ask.json()['result']['message_id']}/events",
            params={"run_id": ask.json()["result"]["run_id"]},
            headers=headers(),
        )
        foreign_message = UUID(int=999)
        response = await client.get(
            f"/api/v1/qa/sessions/{SESSION_ID}/messages/{foreign_message}/events",
            headers={**headers(), "Last-Event-ID": _event_frames(first.text)[0]["id"]},
        )

    assert response.status_code == 400
    assert "run_started" not in response.text
    assert "Which policy" not in response.text
