"""Bridge OperationalObservation:v1 to the existing safe diagnostic sink."""

from __future__ import annotations

from spine.application.diagnostics import (
    DiagnosticContext,
    DiagnosticEventRegistry,
    DiagnosticSeverity,
    DiagnosticSink,
    OperationalObservationDiagnosticPayload,
)
from spine.application.telemetry import (
    OperationalObservation,
    OperationalObservationOutcome,
    OperationalObservationRegistry,
)


class DiagnosticOperationalObservationSink:
    def __init__(
        self,
        *,
        observation_registry: OperationalObservationRegistry,
        diagnostic_registry: DiagnosticEventRegistry,
        diagnostic_sink: DiagnosticSink,
    ) -> None:
        self._observation_registry = observation_registry
        self._diagnostic_registry = diagnostic_registry
        self._diagnostic_sink = diagnostic_sink

    async def emit(self, observation: OperationalObservation) -> None:
        value = self._observation_registry.validate(observation)
        event = self._diagnostic_registry.build_event(
            event_type="operational.observation",
            schema_version=1,
            severity=(
                DiagnosticSeverity.ERROR
                if value.outcome
                in {
                    OperationalObservationOutcome.FAILED,
                    OperationalObservationOutcome.QUARANTINED,
                }
                else DiagnosticSeverity.WARNING
                if value.outcome
                in {
                    OperationalObservationOutcome.RETRY_SCHEDULED,
                    OperationalObservationOutcome.DENIED,
                }
                else DiagnosticSeverity.INFO
            ),
            occurred_at=value.observed_at,
            diagnostic_context=DiagnosticContext(
                trace_id=value.trace_id,
                correlation_id=value.correlation_id,
                causation_id=value.causation_id,
            ),
            workspace_id=value.workspace_id,
            environment_id=value.environment_id,
            payload=OperationalObservationDiagnosticPayload(
                observation_id=value.observation_id,
                component=value.component.value,
                stage=value.stage.value,
                operation=value.operation.value,
                outcome=value.outcome.value,
                safe_failure_code=(
                    value.safe_failure_code.value
                    if value.safe_failure_code is not None
                    else None
                ),
                duration_ms=value.duration_ms,
                attempt_number=value.attempt_number,
                run_id=value.run_id,
                attempt_id=value.attempt_id,
                message_id=value.message_id,
            ),
        )
        await self._diagnostic_sink.emit(event)


__all__ = ["DiagnosticOperationalObservationSink"]
