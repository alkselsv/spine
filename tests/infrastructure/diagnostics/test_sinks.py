from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.diagnostics import (
    DiagnosticContext,
    DiagnosticEventRegistry,
    DiagnosticSeverity,
    FailureDiagnosticPayload,
)
from spine.infrastructure.diagnostics import JsonLoggingDiagnosticSink
from spine.infrastructure.diagnostics import RecordingDiagnosticSink
from tests.contracts.diagnostics.fixtures import DIAGNOSTIC_LEAK_CORPUS


def synthetic_uuid(value: int) -> UUID:
    return UUID(int=value)


@pytest.mark.asyncio
async def test_logging_sink_emits_only_the_validated_structured_snapshot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = DiagnosticEventRegistry.with_default_families()
    logger = logging.getLogger("spine.tests.diagnostics")
    sink = JsonLoggingDiagnosticSink(registry, logger=logger)
    event = registry.build_event(
        event_type="failure.observed",
        schema_version=1,
        severity=DiagnosticSeverity.ERROR,
        occurred_at=datetime(2026, 10, 10, 12, 30, tzinfo=timezone.utc),
        diagnostic_context=DiagnosticContext(
            trace_id=synthetic_uuid(1),
            correlation_id=synthetic_uuid(2),
            causation_id=synthetic_uuid(3),
        ),
        workspace_id=synthetic_uuid(4),
        environment_id=synthetic_uuid(5),
        payload=FailureDiagnosticPayload(
            failure_code="authorization.unavailable",
        ),
    )
    caplog.set_level(logging.DEBUG, logger=logger.name)

    await sink.emit(event)

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.ERROR
    assert record.exc_info is None
    assert record.args == ()
    assert json.loads(record.message) == {
        "causation_id": str(synthetic_uuid(3)),
        "correlation_id": str(synthetic_uuid(2)),
        "environment_id": str(synthetic_uuid(5)),
        "event_type": "failure.observed",
        "occurred_at": "2026-10-10T12:30:00+00:00",
        "payload": {"failure_code": "authorization.unavailable"},
        "schema_version": 1,
        "severity": "error",
        "trace_id": str(synthetic_uuid(1)),
        "workspace_id": str(synthetic_uuid(4)),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("protected_value", DIAGNOSTIC_LEAK_CORPUS)
async def test_protected_corpus_cannot_reach_structured_logs(
    protected_value: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = DiagnosticEventRegistry.with_default_families()
    logger = logging.getLogger("spine.tests.diagnostics.leak-corpus")
    sink = JsonLoggingDiagnosticSink(registry, logger=logger)
    recording_sink = RecordingDiagnosticSink(registry)
    caplog.set_level(logging.DEBUG, logger=logger.name)

    with pytest.raises(ValidationError):
        payload = FailureDiagnosticPayload(failure_code=protected_value)
        event = registry.build_event(
            event_type="failure.observed",
            schema_version=1,
            severity=DiagnosticSeverity.ERROR,
            occurred_at=datetime(2026, 10, 10, 12, 30, tzinfo=timezone.utc),
            diagnostic_context=DiagnosticContext(trace_id=synthetic_uuid(10)),
            payload=payload,
        )
        await sink.emit(event)
        await recording_sink.emit(event)

    assert protected_value not in caplog.text
    event_serialization = json.dumps(
        [event.payload_json() for event in recording_sink.events],
        sort_keys=True,
    )
    assert protected_value not in event_serialization
