from __future__ import annotations

from uuid import UUID

import pytest

from spine.application.workflows.ask_admission_dispatch import (
    ASK_ADMISSION_WORKFLOW_TYPE,
    AskAdmissionWorkflowInput,
    TemporalWorkflowAlreadyStarted,
)
from spine.workflows.temporal.client import TemporalSdkWorkflowClient

RUN_ID = UUID("60000000-0000-0000-0000-000000000427")


def workflow_input() -> AskAdmissionWorkflowInput:
    return AskAdmissionWorkflowInput(
        command_id=UUID("30000000-0000-0000-0000-000000000427"),
        workspace_id=UUID("10000000-0000-0000-0000-000000000427"),
        environment_id=UUID("20000000-0000-0000-0000-000000000427"),
        session_id=UUID("40000000-0000-0000-0000-000000000427"),
        message_id=UUID("50000000-0000-0000-0000-000000000427"),
        run_id=RUN_ID,
        trace_id=UUID("70000000-0000-0000-0000-000000000427"),
    )


class FakeTemporalSdk:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, dict[str, object]]] = []
        self.handles: list[str] = []
        self.duplicate = False

    async def start_workflow(self, workflow, arg, **kwargs):
        if self.duplicate:
            error = type("WorkflowAlreadyStartedError", (Exception,), {})()
            raise error
        self.calls.append((workflow, arg, kwargs))
        return object()

    def get_workflow_handle(self, workflow_id):
        self.handles.append(workflow_id)
        return object()


@pytest.mark.asyncio
async def test_temporal_sdk_bridge_starts_with_stable_id_and_typed_payload() -> None:
    sdk = FakeTemporalSdk()
    client = TemporalSdkWorkflowClient(
        sdk, task_queue="qa", id_reuse_policy="reject_duplicate"
    )

    handle = await client.start_workflow(
        workflow_type=ASK_ADMISSION_WORKFLOW_TYPE,
        workflow_id=str(RUN_ID),
        input=workflow_input(),
    )

    assert handle.run_id == RUN_ID
    assert sdk.calls == [
        (
            ASK_ADMISSION_WORKFLOW_TYPE,
            workflow_input().model_dump(mode="json"),
            {
                "id": str(RUN_ID),
                "task_queue": "qa",
                "id_reuse_policy": "reject_duplicate",
            },
        )
    ]
    assert "question" not in sdk.calls[0][1]


@pytest.mark.asyncio
async def test_temporal_sdk_bridge_maps_duplicate_start_to_stable_identity() -> None:
    sdk = FakeTemporalSdk()
    sdk.duplicate = True
    client = TemporalSdkWorkflowClient(sdk, task_queue="qa")

    with pytest.raises(TemporalWorkflowAlreadyStarted) as raised:
        await client.start_workflow(
            workflow_type=ASK_ADMISSION_WORKFLOW_TYPE,
            workflow_id=str(RUN_ID),
            input=workflow_input(),
        )

    assert raised.value.workflow_id == str(RUN_ID)
