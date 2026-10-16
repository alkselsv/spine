from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest

from spine.api.authorized_ask import install_authorized_ask_routes
from spine.api.qa_stream import install_qa_stream_routes
from spine.application.qa_events import InMemoryQAEventLog
from spine.application.qa_streaming import AuthorizedQAStreamService
from spine.application.telemetry import (
    OperationalComponent,
    OperationalObservationEmitter,
    OperationalObservationPolicy,
    OperationalObservationRegistry,
    RecordingOperationalObservationSink,
)
from tests.api.test_authorized_ask import (
    FakeAskService,
    SESSION_ID,
    answered_result,
    headers,
)
from tests.api.test_authorization import authorized_app


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


def _app(*, allowed: dict[str, bool], log: InMemoryQAEventLog) -> httpx.ASGITransport:
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
    )
    return httpx.ASGITransport(app=application)


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
async def test_sse_persistence_emits_safe_terminal_observation_with_run_trace() -> None:
    registry = OperationalObservationRegistry()
    sink = RecordingOperationalObservationSink(registry)
    observer = OperationalObservationEmitter(
        registry=registry,
        sink=sink,
        policy=OperationalObservationPolicy(),
        clock=lambda: datetime(2026, 10, 16, tzinfo=timezone.utc),
        observation_id_source=lambda: UUID(int=361),
    )
    original = answered_result()
    correlation = original.output.correlation.model_copy(
        update={"trace_id": UUID(int=1000)}
    )
    result = replace(
        original,
        output=original.output.model_copy(update={"correlation": correlation}),
    )
    service = AuthorizedQAStreamService(
        event_log=InMemoryQAEventLog(),
        operational_observer=observer,
    )

    await service.publish_result(result)

    assert len(sink.observations) == 1
    assert sink.observations[0].component is OperationalComponent.SSE
    assert sink.observations[0].trace_id == UUID(int=1000)
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
