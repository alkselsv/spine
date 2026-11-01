from uuid import UUID

import pytest

from spine.application.diagnostics.context import DiagnosticContext
from spine.application.persistence.outbox import OpaqueObjectReference, OutboxEventRegistry
from spine.application.persistence.workflow_runs import (
    CLARIFICATION_ANSWERED_EVENT_TYPE,
    WORKFLOW_SIGNAL_EVENT_SCHEMA_VERSION,
    ClarificationAnsweredDispatchPayload,
    register_workflow_run_outbox_event,
)
from spine.application.workflows.requirements_clarification_dispatch import (
    RequirementsWorkflowSignalDispatchConsumer,
)


RUN_ID = UUID("64300000-0000-4000-8000-000000000001")
TRACE_ID = UUID("64300000-0000-4000-8000-000000000002")
CORRELATION_ID = UUID("64300000-0000-4000-8000-000000000003")
CAUSATION_ID = UUID("64300000-0000-4000-8000-000000000004")


class TemporalFake:
    def __init__(self) -> None:
        self.answers = []

    async def signal_clarification_requested(self, payload):
        raise AssertionError(payload)

    async def signal_clarification_answered(self, payload):
        self.answers.append(payload)

    async def signal_run_cancelled(self, payload):
        raise AssertionError(payload)

    async def signal_requirements_accepted(self, payload):
        raise AssertionError(payload)


def answer_intent():
    registry = OutboxEventRegistry()
    register_workflow_run_outbox_event(registry)
    payload = ClarificationAnsweredDispatchPayload(
        command_id=UUID("64300000-0000-4000-8000-000000000005"),
        run_id=RUN_ID,
        clarification_id=UUID("64300000-0000-4000-8000-000000000006"),
        artifact_id=UUID("64300000-0000-4000-8000-000000000007"),
        artifact_version=2,
        workflow_version_id=UUID("64300000-0000-4000-8000-000000000008"),
    )
    return registry.build_intent(
        event_id=UUID("64300000-0000-4000-8000-000000000009"),
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        event_type=CLARIFICATION_ANSWERED_EVENT_TYPE,
        schema_version=WORKFLOW_SIGNAL_EVENT_SCHEMA_VERSION,
        payload=payload,
        aggregate=OpaqueObjectReference(
            object_type="workflow_run", object_id=RUN_ID, schema_version=1
        ),
        producer_deduplication_id="workflow-signal:643",
        trace_id=TRACE_ID,
        correlation_id=CORRELATION_ID,
        causation_id=CAUSATION_ID,
    )


@pytest.mark.asyncio
async def test_answer_delivery_signals_only_content_free_successor_lineage() -> None:
    temporal = TemporalFake()
    intent = answer_intent()
    consumer = RequirementsWorkflowSignalDispatchConsumer(temporal)

    await consumer.consume(
        intent,
        DiagnosticContext(
            trace_id=TRACE_ID,
            correlation_id=CORRELATION_ID,
            causation_id=CAUSATION_ID,
        ),
    )

    assert temporal.answers == [intent.payload]
    assert "answer" not in intent.payload_json()
