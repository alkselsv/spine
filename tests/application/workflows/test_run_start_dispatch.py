from __future__ import annotations

from uuid import UUID

import pytest

from spine.application.diagnostics.context import DiagnosticContext
from spine.application.outbox.dispatch import RetryableOutboxConsumerFailure
from spine.application.persistence.outbox import OpaqueObjectReference, OutboxEventRegistry
from spine.application.persistence.workflow_runs import (
    START_WORKFLOW_EVENT_SCHEMA_VERSION,
    START_WORKFLOW_EVENT_TYPE,
    StartWorkflowDispatchPayload,
    register_workflow_run_outbox_event,
)
from spine.application.workflows.run_start_dispatch import (
    TemporalWorkflowAlreadyStarted,
    WorkflowRunStartDispatchConsumer,
    WorkflowRunStartHandle,
    WorkflowRunStartInput,
    WorkflowRunStartLauncher,
    workflow_id_for_run,
)


RUN_ID = UUID("61000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("61000000-0000-4000-8000-000000000002")
ENVIRONMENT_ID = UUID("61000000-0000-4000-8000-000000000003")
COMMAND_ID = UUID("61000000-0000-4000-8000-000000000004")
WORK_ITEM_ID = UUID("61000000-0000-4000-8000-000000000005")
VERSION_ID = UUID("61000000-0000-4000-8000-000000000006")
TRACE_ID = UUID("61000000-0000-4000-8000-000000000007")
CORRELATION_ID = UUID("61000000-0000-4000-8000-000000000008")
CAUSATION_ID = UUID("61000000-0000-4000-8000-000000000009")


class TemporalFake:
    def __init__(self) -> None:
        self.started: dict[str, WorkflowRunStartInput] = {}
        self.start_calls = 0

    async def start_workflow(self, *, workflow_type, workflow_id, input):
        self.start_calls += 1
        if workflow_id in self.started:
            raise TemporalWorkflowAlreadyStarted(workflow_id)
        self.started[workflow_id] = input
        return WorkflowRunStartHandle(workflow_id=workflow_id, run_id=input.run_id)

    async def get_workflow(self, *, workflow_id):
        value = self.started[workflow_id]
        return WorkflowRunStartHandle(workflow_id=workflow_id, run_id=value.run_id)


def _intent():
    registry = OutboxEventRegistry()
    register_workflow_run_outbox_event(registry)
    return registry.build_intent(
        event_id=UUID("61000000-0000-4000-8000-000000000010"),
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        event_type=START_WORKFLOW_EVENT_TYPE,
        schema_version=START_WORKFLOW_EVENT_SCHEMA_VERSION,
        payload=StartWorkflowDispatchPayload(
            command_id=COMMAND_ID,
            run_id=RUN_ID,
            work_item_id=WORK_ITEM_ID,
            workflow_key="commercial.requirements",
            workflow_version_id=VERSION_ID,
            workflow_version="1.0.0",
        ),
        aggregate=OpaqueObjectReference(
            object_type="workflow_run", object_id=RUN_ID, schema_version=1
        ),
        producer_deduplication_id="workflow-start:61000000-0000-4000-8000-000000000001",
        trace_id=TRACE_ID,
        correlation_id=CORRELATION_ID,
        causation_id=CAUSATION_ID,
    )


@pytest.mark.asyncio
async def test_duplicate_delivery_resolves_to_one_stable_temporal_run() -> None:
    temporal = TemporalFake()
    consumer = WorkflowRunStartDispatchConsumer(WorkflowRunStartLauncher(temporal))
    intent = _intent()
    context = DiagnosticContext(
        trace_id=TRACE_ID,
        correlation_id=CORRELATION_ID,
        causation_id=CAUSATION_ID,
    )

    await consumer.consume(intent, context)
    await consumer.consume(intent, context)

    assert temporal.start_calls == 2
    assert tuple(temporal.started) == (workflow_id_for_run(RUN_ID),)
    assert temporal.started[workflow_id_for_run(RUN_ID)].workflow_version_id == VERSION_ID
    assert "message" not in intent.payload_json()


@pytest.mark.asyncio
async def test_temporal_delivery_failure_remains_retryable() -> None:
    class UnavailableTemporal(TemporalFake):
        async def start_workflow(self, **kwargs):
            raise RuntimeError("protected transport detail")

    consumer = WorkflowRunStartDispatchConsumer(
        WorkflowRunStartLauncher(UnavailableTemporal())
    )
    intent = _intent()

    with pytest.raises(RetryableOutboxConsumerFailure) as failure:
        await consumer.consume(
            intent,
            DiagnosticContext(
                trace_id=TRACE_ID,
                correlation_id=CORRELATION_ID,
                causation_id=CAUSATION_ID,
            ),
        )

    assert failure.value.code == "workflow.start.temporal_unavailable"
