"""Diagnostic sink adapters."""

from __future__ import annotations

import json
import logging

from spine.application.diagnostics.events import (
    DiagnosticEvent,
    DiagnosticEventRegistry,
    DiagnosticSeverity,
)


_LOG_LEVELS = {
    DiagnosticSeverity.DEBUG: logging.DEBUG,
    DiagnosticSeverity.INFO: logging.INFO,
    DiagnosticSeverity.WARNING: logging.WARNING,
    DiagnosticSeverity.ERROR: logging.ERROR,
    DiagnosticSeverity.CRITICAL: logging.CRITICAL,
}


class RecordingDiagnosticSink:
    """Deterministic in-memory sink for composition and contract tests."""

    def __init__(self, registry: DiagnosticEventRegistry) -> None:
        self._registry = registry
        self._events: list[DiagnosticEvent] = []

    @property
    def events(self) -> tuple[DiagnosticEvent, ...]:
        return tuple(self._events)

    async def emit(self, event: DiagnosticEvent) -> None:
        self._events.append(self._registry.validate(event))


class JsonLoggingDiagnosticSink:
    """Emit validated events as one bounded standard-library JSON log record."""

    def __init__(
        self,
        registry: DiagnosticEventRegistry,
        *,
        logger: logging.Logger,
    ) -> None:
        self._registry = registry
        self._logger = logger

    async def emit(self, event: DiagnosticEvent) -> None:
        snapshot = self._registry.validate(event)
        record = {
            "event_type": snapshot.event_type,
            "schema_version": snapshot.schema_version,
            "severity": snapshot.severity.value,
            "occurred_at": snapshot.occurred_at.isoformat(),
            "trace_id": str(snapshot.trace_id),
            "correlation_id": (
                str(snapshot.correlation_id)
                if snapshot.correlation_id is not None
                else None
            ),
            "causation_id": (
                str(snapshot.causation_id)
                if snapshot.causation_id is not None
                else None
            ),
            "workspace_id": (
                str(snapshot.workspace_id)
                if snapshot.workspace_id is not None
                else None
            ),
            "environment_id": (
                str(snapshot.environment_id)
                if snapshot.environment_id is not None
                else None
            ),
            "payload": snapshot.payload_json(),
        }
        self._logger.log(
            _LOG_LEVELS[snapshot.severity],
            json.dumps(
                record,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )


__all__ = ["JsonLoggingDiagnosticSink", "RecordingDiagnosticSink"]
