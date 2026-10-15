from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import logging
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from fastapi import Depends, FastAPI
from pydantic import BaseModel, ConfigDict, Field

from spine.api.main import create_app, get_diagnostic_context
from spine.api import main as api_main
from spine.api.schemas import InvalidCursor
from spine.application.diagnostics import DiagnosticContext, DiagnosticEventRegistry
from spine.application.persistence import PersistenceUnavailableError
from spine.infrastructure.diagnostics import (
    JsonLoggingDiagnosticSink,
    RecordingDiagnosticSink,
)


TRACE_ID = UUID("50000000-0000-0000-0000-000000000067")
FALLBACK_TRACE_ID = UUID("50000000-0000-0000-0000-000000000068")
NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


class SyntheticCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int = Field(ge=1)


def diagnostic_app(
    *,
    trace_id_source: Callable[[], UUID] = lambda: FALLBACK_TRACE_ID,
) -> tuple[FastAPI, RecordingDiagnosticSink]:
    registry = DiagnosticEventRegistry.with_default_families()
    sink = RecordingDiagnosticSink(registry)
    application = create_app(
        trace_id_source=trace_id_source,
        diagnostic_registry=registry,
        diagnostic_sink=sink,
        clock=lambda: NOW,
    )

    @application.get("/api/v1/diagnostic-context")
    async def diagnostic_context_endpoint(
        context: DiagnosticContext = Depends(get_diagnostic_context),
    ) -> dict[str, str]:
        return {"trace_id": str(context.trace_id)}

    @application.post("/api/v1/synthetic-command")
    async def synthetic_command(body: SyntheticCommand) -> dict[str, int]:
        return {"count": body.count}

    @application.get("/api/v1/known-failure")
    async def known_failure() -> None:
        raise PersistenceUnavailableError("Bearer production-token")

    @application.get("/api/v1/unexpected-failure")
    async def unexpected_failure() -> None:
        raise RuntimeError("SELECT password FROM credentials")

    @application.get("/api/v1/invalid-cursor")
    async def invalid_cursor() -> None:
        raise InvalidCursor("protected cursor payload")

    return application, sink


async def test_valid_traceparent_continues_one_trusted_request_trace() -> None:
    application, _ = diagnostic_app()
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/diagnostic-context",
            headers={
                "traceparent": f"00-{TRACE_ID.hex}-0123456789abcdef-01",
                "x-trace-id": str(FALLBACK_TRACE_ID),
            },
        )

    assert response.status_code == 200
    assert response.headers["x-trace-id"] == str(TRACE_ID)
    assert response.json() == {"trace_id": str(TRACE_ID)}


async def test_untrusted_inputs_cannot_override_fresh_isolated_request_traces() -> None:
    generated = iter(
        (
            FALLBACK_TRACE_ID,
            UUID("50000000-0000-0000-0000-000000000069"),
        )
    )
    application, sink = diagnostic_app(trace_id_source=generated.__next__)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        malformed = await client.get(
            "/api/v1/diagnostic-context",
            params={"trace_id": str(TRACE_ID)},
            headers={
                "traceparent": "00-protected customer document excerpt",
                "x-trace-id": str(TRACE_ID),
            },
        )
        absent = await client.get(
            "/api/v1/diagnostic-context",
            params={"trace_id": str(TRACE_ID)},
            headers={"x-trace-id": str(TRACE_ID)},
        )

    assert malformed.headers["x-trace-id"] == str(FALLBACK_TRACE_ID)
    assert malformed.json() == {"trace_id": str(FALLBACK_TRACE_ID)}
    assert absent.headers["x-trace-id"] == "50000000-0000-0000-0000-000000000069"
    assert absent.json() == {
        "trace_id": "50000000-0000-0000-0000-000000000069"
    }
    invalid_context_events = [
        event
        for event in sink.events
        if event.event_type == "failure.observed"
    ]
    assert len(invalid_context_events) == 1
    assert invalid_context_events[0].trace_id == FALLBACK_TRACE_ID
    assert invalid_context_events[0].payload_json() == {
        "failure_code": "trace_context.invalid"
    }


async def test_api_validation_failure_returns_safe_structured_error() -> None:
    application, _ = diagnostic_app()
    transport = httpx.ASGITransport(app=application)
    protected_value = "protected customer document excerpt"

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/synthetic-command",
            json={"count": protected_value},
        )

    assert response.status_code == 422
    assert response.headers["x-trace-id"] == str(FALLBACK_TRACE_ID)
    assert response.json() == {
        "schema_version": 1,
        "code": "spine.request.validation",
        "safe_message": "Request validation failed.",
        "retryability": "never",
        "trace_id": str(FALLBACK_TRACE_ID),
    }
    assert protected_value not in response.text


async def test_api_http_failure_uses_structured_error_envelope() -> None:
    application, _ = diagnostic_app()
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/missing")

    assert response.status_code == 404
    assert response.headers["x-trace-id"] == str(FALLBACK_TRACE_ID)
    assert response.json() == {
        "schema_version": 1,
        "code": "spine.request.rejected",
        "safe_message": "The request was rejected.",
        "retryability": "never",
        "trace_id": str(FALLBACK_TRACE_ID),
    }


async def test_registered_api_failure_uses_safe_mapping_and_same_trace() -> None:
    application, _ = diagnostic_app()
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/known-failure")

    assert response.status_code == 503
    assert response.headers["x-trace-id"] == str(FALLBACK_TRACE_ID)
    assert response.json() == {
        "schema_version": 1,
        "code": "spine.persistence.unavailable",
        "safe_message": "Persistence is temporarily unavailable.",
        "retryability": "after_delay",
        "trace_id": str(FALLBACK_TRACE_ID),
    }
    assert "production-token" not in response.text


async def test_unexpected_api_failure_is_generic_and_keeps_request_trace() -> None:
    application, _ = diagnostic_app()
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/unexpected-failure")

    assert response.status_code == 500
    assert response.headers["x-trace-id"] == str(FALLBACK_TRACE_ID)
    assert response.json() == {
        "schema_version": 1,
        "code": "spine.internal.unexpected",
        "safe_message": "An unexpected internal error occurred.",
        "retryability": "never",
        "trace_id": str(FALLBACK_TRACE_ID),
    }
    assert "password" not in response.text


async def test_invalid_cursor_uses_safe_structured_400_without_cursor_details() -> None:
    application, _ = diagnostic_app()
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/invalid-cursor")

    assert response.status_code == 400
    assert response.json() == {
        "schema_version": 1,
        "code": "spine.request.invalid_cursor",
        "safe_message": "The cursor is invalid.",
        "retryability": "never",
        "trace_id": str(FALLBACK_TRACE_ID),
    }
    assert "protected cursor payload" not in response.text


async def test_request_diagnostic_contains_only_safe_status_metadata() -> None:
    application, sink = diagnostic_app()
    transport = httpx.ASGITransport(app=application)
    protected_value = "system prompt containing customer instructions"

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/synthetic-command?question=protected",
            json={"count": protected_value},
        )

    assert response.status_code == 422
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.event_type == "http.request"
    assert event.trace_id == FALLBACK_TRACE_ID
    assert event.payload_json() == {
        "request_state": "failed",
        "response_status_code": 422,
    }
    assert protected_value not in event.model_dump_json()
    assert "question=protected" not in event.model_dump_json()


async def test_request_json_log_excludes_body_query_and_exception_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = DiagnosticEventRegistry.with_default_families()
    logger = logging.getLogger("spine.tests.api.request-diagnostics")
    application = create_app(
        trace_id_source=lambda: FALLBACK_TRACE_ID,
        diagnostic_registry=registry,
        diagnostic_sink=JsonLoggingDiagnosticSink(registry, logger=logger),
        clock=lambda: NOW,
    )

    @application.post("/api/v1/logging-command")
    async def logging_command(body: SyntheticCommand) -> dict[str, int]:
        return {"count": body.count}

    protected_value = "provider raw error https://provider.invalid/v1/responses"
    transport = httpx.ASGITransport(app=application)
    with caplog.at_level(logging.INFO, logger=logger.name):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/logging-command?question=protected",
                json={"count": protected_value},
            )

    assert response.status_code == 422
    assert len(caplog.records) == 1
    assert caplog.records[0].exc_info is None
    assert protected_value not in caplog.text
    assert "question=protected" not in caplog.text
    assert '"response_status_code":422' in caplog.text


async def test_legacy_ask_bodies_remain_compatible_with_trace_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def successful_ask(
        question: str,
        *,
        dataset: str | None,
        session_id: str | None,
    ) -> list[str]:
        del question, dataset, session_id
        return ["opaque-result"]

    async def failing_ask(
        question: str,
        *,
        dataset: str | None,
        session_id: str | None,
    ) -> list[str]:
        del question, dataset, session_id
        raise RuntimeError("legacy provider failure")

    monkeypatch.setattr(api_main, "ask", successful_ask)
    monkeypatch.setattr(api_main, "format_answers", lambda _: "legacy answer")
    application, _ = diagnostic_app()
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        success = await client.post("/ask", json={"question": "legacy question"})
        validation = await client.post("/ask", json={})
        monkeypatch.setattr(api_main, "ask", failing_ask)
        failure = await client.post("/ask", json={"question": "legacy question"})

    assert success.json() == {
        "question": "legacy question",
        "answer": "legacy answer",
        "raw_count": 1,
    }
    assert set(validation.json()) == {"detail"}
    assert failure.json() == {"detail": "recall failed: legacy provider failure"}
    for response in (success, validation, failure):
        assert response.headers["x-trace-id"] == str(FALLBACK_TRACE_ID)


async def test_legacy_ingest_body_remains_compatible_with_trace_header(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy.txt"
    source.write_text("synthetic legacy content", encoding="utf-8")

    async def remember(
        payloads: object,
        *,
        dataset: str | None,
        node_set: list[str] | None,
    ) -> None:
        del payloads, dataset, node_set

    monkeypatch.setattr(api_main, "load_path", lambda _: (object(),))
    monkeypatch.setattr(
        api_main,
        "documents_to_remember_payloads",
        lambda _: ("synthetic-payload",),
    )
    monkeypatch.setattr(api_main, "remember_texts", remember)
    application, _ = diagnostic_app()
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/ingest", json={"path": str(source)})

    assert response.status_code == 200
    assert response.headers["x-trace-id"] == str(FALLBACK_TRACE_ID)
    assert response.json() == {
        "path": str(source),
        "documents": 1,
        "status": "ok",
    }
