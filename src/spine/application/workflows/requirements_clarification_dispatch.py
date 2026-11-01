"""Content-free outbox delivery into one durable requirements workflow."""

from __future__ import annotations

from typing import Protocol

from spine.application.diagnostics.context import DiagnosticContext
from spine.application.outbox.dispatch import (
    OutboxConsumerSuccess,
    RetryableOutboxConsumerFailure,
    TerminalOutboxConsumerFailure,
)
from spine.application.persistence.outbox import OutboxIntent
from spine.application.persistence.workflow_runs import (
    CLARIFICATION_ANSWERED_EVENT_TYPE,
    CLARIFICATION_REQUESTED_EVENT_TYPE,
    WORKFLOW_CANCELLED_EVENT_TYPE,
    REQUIREMENTS_ACCEPTED_EVENT_TYPE,
    WORKFLOW_SIGNAL_EVENT_SCHEMA_VERSION,
    ClarificationAnsweredDispatchPayload,
    ClarificationRequestedDispatchPayload,
    WorkflowCancelledDispatchPayload,
    RequirementsAcceptedDispatchPayload,
)


class RequirementsWorkflowSignalClient(Protocol):
    async def signal_clarification_requested(
        self, payload: ClarificationRequestedDispatchPayload
    ) -> None: ...

    async def signal_clarification_answered(
        self, payload: ClarificationAnsweredDispatchPayload
    ) -> None: ...

    async def signal_run_cancelled(
        self, payload: WorkflowCancelledDispatchPayload
    ) -> None: ...

    async def signal_requirements_accepted(
        self, payload: RequirementsAcceptedDispatchPayload
    ) -> None: ...


class RequirementsWorkflowSignalDispatchConsumer:
    """Deliver canonical wait/resume/cancel transitions to Temporal."""

    def __init__(self, client: RequirementsWorkflowSignalClient) -> None:
        self._client = client

    async def consume(
        self,
        intent: OutboxIntent,
        diagnostic_context: DiagnosticContext,
    ) -> OutboxConsumerSuccess:
        if (
            intent.schema_version != WORKFLOW_SIGNAL_EVENT_SCHEMA_VERSION
            or intent.environment_id is None
            or intent.aggregate is None
            or intent.aggregate.object_type != "workflow_run"
            or intent.aggregate.object_id != getattr(intent.payload, "run_id", None)
        ):
            raise TerminalOutboxConsumerFailure("workflow.signal.invalid_intent")
        expected = DiagnosticContext(
            trace_id=intent.trace_id,
            correlation_id=intent.correlation_id,
            causation_id=intent.causation_id or intent.event_id,
        )
        if diagnostic_context != expected:
            raise TerminalOutboxConsumerFailure("workflow.signal.invalid_intent")
        try:
            if (
                intent.event_type == CLARIFICATION_REQUESTED_EVENT_TYPE
                and type(intent.payload) is ClarificationRequestedDispatchPayload
            ):
                await self._client.signal_clarification_requested(intent.payload)
            elif (
                intent.event_type == CLARIFICATION_ANSWERED_EVENT_TYPE
                and type(intent.payload) is ClarificationAnsweredDispatchPayload
            ):
                await self._client.signal_clarification_answered(intent.payload)
            elif (
                intent.event_type == WORKFLOW_CANCELLED_EVENT_TYPE
                and type(intent.payload) is WorkflowCancelledDispatchPayload
            ):
                await self._client.signal_run_cancelled(intent.payload)
            elif (
                intent.event_type == REQUIREMENTS_ACCEPTED_EVENT_TYPE
                and type(intent.payload) is RequirementsAcceptedDispatchPayload
            ):
                await self._client.signal_requirements_accepted(intent.payload)
            else:
                raise TerminalOutboxConsumerFailure("workflow.signal.invalid_intent")
        except TerminalOutboxConsumerFailure:
            raise
        except Exception:
            raise RetryableOutboxConsumerFailure(
                "workflow.signal.temporal_unavailable"
            ) from None
        return OutboxConsumerSuccess(diagnostic_context=expected)


__all__ = [
    "RequirementsWorkflowSignalClient",
    "RequirementsWorkflowSignalDispatchConsumer",
]
