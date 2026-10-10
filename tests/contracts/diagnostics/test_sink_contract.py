from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from uuid import UUID

import pytest

from spine.application.diagnostics import (
    DiagnosticContext,
    DiagnosticContextMismatchError,
    DiagnosticEvent,
    DiagnosticEventRegistry,
    DiagnosticSeverity,
    DiagnosticSink,
    FailureDiagnosticPayload,
    UnsupportedDiagnosticEventError,
)
from spine.infrastructure.diagnostics import (
    JsonLoggingDiagnosticSink,
    RecordingDiagnosticSink,
)


def synthetic_uuid(value: int) -> UUID:
    return UUID(int=value)


def event_snapshot(event: DiagnosticEvent) -> dict[str, object]:
    return {
        "causation_id": str(event.causation_id) if event.causation_id else None,
        "correlation_id": str(event.correlation_id) if event.correlation_id else None,
        "environment_id": str(event.environment_id) if event.environment_id else None,
        "event_type": event.event_type,
        "occurred_at": event.occurred_at.isoformat(),
        "payload": event.payload_json(),
        "schema_version": event.schema_version,
        "severity": event.severity.value,
        "trace_id": str(event.trace_id),
        "workspace_id": str(event.workspace_id) if event.workspace_id else None,
    }


class CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@dataclass(frozen=True)
class DiagnosticSinkHarness:
    registry: DiagnosticEventRegistry
    sink: DiagnosticSink
    observations: Callable[[], tuple[dict[str, object], ...]]


@pytest.fixture(params=("recording", "logging"))
def diagnostic_sink_harness(request: pytest.FixtureRequest) -> DiagnosticSinkHarness:
    registry = DiagnosticEventRegistry.with_default_families()
    if request.param == "recording":
        sink = RecordingDiagnosticSink(registry)
        return DiagnosticSinkHarness(
            registry=registry,
            sink=sink,
            observations=lambda: tuple(event_snapshot(event) for event in sink.events),
        )

    handler = CaptureHandler()
    logger = logging.Logger("spine.contracts.diagnostics", level=logging.DEBUG)
    logger.addHandler(handler)
    sink = JsonLoggingDiagnosticSink(registry, logger=logger)
    return DiagnosticSinkHarness(
        registry=registry,
        sink=sink,
        observations=lambda: tuple(
            json.loads(record.getMessage()) for record in handler.records
        ),
    )


def registered_event(registry: DiagnosticEventRegistry) -> DiagnosticEvent:
    return registry.build_event(
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
        payload=FailureDiagnosticPayload(failure_code="authorization.denied"),
    )


@pytest.mark.asyncio
async def test_sink_emits_the_same_validated_snapshot(
    diagnostic_sink_harness: DiagnosticSinkHarness,
) -> None:
    event = registered_event(diagnostic_sink_harness.registry)

    await diagnostic_sink_harness.sink.emit(event)

    assert diagnostic_sink_harness.observations() == (event_snapshot(event),)


@pytest.mark.asyncio
async def test_sink_rejects_unregistered_event_before_observation(
    diagnostic_sink_harness: DiagnosticSinkHarness,
) -> None:
    event = registered_event(diagnostic_sink_harness.registry).model_copy(
        update={"event_type": "unregistered.event"}
    )

    with pytest.raises(UnsupportedDiagnosticEventError):
        await diagnostic_sink_harness.sink.emit(event)

    assert diagnostic_sink_harness.observations() == ()


@pytest.mark.asyncio
async def test_sink_revalidates_mutated_payload_before_observation(
    diagnostic_sink_harness: DiagnosticSinkHarness,
) -> None:
    event = registered_event(diagnostic_sink_harness.registry)
    object.__setattr__(event.payload, "failure_code", "Bearer production-token")

    with pytest.raises(UnsupportedDiagnosticEventError):
        await diagnostic_sink_harness.sink.emit(event)

    assert diagnostic_sink_harness.observations() == ()


@pytest.mark.asyncio
async def test_sink_rejects_arbitrary_dictionary_before_observation(
    diagnostic_sink_harness: DiagnosticSinkHarness,
) -> None:
    with pytest.raises(UnsupportedDiagnosticEventError):
        await diagnostic_sink_harness.sink.emit(  # type: ignore[arg-type]
            {"event_type": "failure.observed"}
        )

    assert diagnostic_sink_harness.observations() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "substitution",
    (
        {"trace_id": synthetic_uuid(20)},
        {"correlation_id": synthetic_uuid(21)},
        {"causation_id": synthetic_uuid(22)},
        {"workspace_id": synthetic_uuid(23)},
        {"environment_id": synthetic_uuid(24)},
    ),
)
async def test_sink_rejects_lineage_or_scope_substitution_before_observation(
    diagnostic_sink_harness: DiagnosticSinkHarness,
    substitution: dict[str, object],
) -> None:
    event = registered_event(diagnostic_sink_harness.registry).model_copy(
        update=substitution
    )

    with pytest.raises(DiagnosticContextMismatchError):
        await diagnostic_sink_harness.sink.emit(event)

    assert diagnostic_sink_harness.observations() == ()
