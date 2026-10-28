from __future__ import annotations

from uuid import UUID

import pytest

from spine.application.workflows.run_start_dispatch import (
    TemporalWorkflowAlreadyStarted,
    WorkflowRunStartInput,
)
from spine.workflows.temporal.run_start_client import TemporalSdkWorkflowRunClient


class WorkflowAlreadyStartedError(Exception):
    pass


class SdkHandle:
    pass


class SdkClientFake:
    def __init__(self) -> None:
        self.started = []
        self.fail_duplicate = False

    async def start_workflow(self, workflow, arg, *, id, task_queue, id_reuse_policy=None):
        self.started.append((workflow, arg, id, task_queue))
        if self.fail_duplicate:
            raise WorkflowAlreadyStartedError()

    def get_workflow_handle(self, workflow_id: str):
        return SdkHandle()


def _input() -> WorkflowRunStartInput:
    return WorkflowRunStartInput(
        workspace_id=UUID("62000000-0000-4000-8000-000000000001"),
        environment_id=UUID("62000000-0000-4000-8000-000000000002"),
        command_id=UUID("62000000-0000-4000-8000-000000000003"),
        run_id=UUID("62000000-0000-4000-8000-000000000004"),
        work_item_id=UUID("62000000-0000-4000-8000-000000000005"),
        workflow_key="commercial.requirements",
        workflow_version_id=UUID("62000000-0000-4000-8000-000000000006"),
        workflow_version="1.0.0",
        trace_id=UUID("62000000-0000-4000-8000-000000000007"),
    )


@pytest.mark.asyncio
async def test_temporal_client_uses_stable_run_id_and_safe_typed_input() -> None:
    sdk = SdkClientFake()
    client = TemporalSdkWorkflowRunClient(sdk, task_queue="spine")
    workflow_input = _input()

    handle = await client.start_workflow(
        workflow_type="spine.workflow.run-start:v1",
        workflow_id=workflow_input.workflow_id,
        input=workflow_input,
    )

    assert handle.workflow_id == str(workflow_input.run_id)
    assert sdk.started[0][2] == str(workflow_input.run_id)
    assert sdk.started[0][1]["workflow_version_id"] == str(workflow_input.workflow_version_id)
    assert "message" not in sdk.started[0][1]


@pytest.mark.asyncio
async def test_temporal_client_translates_duplicate_start_to_safe_identity() -> None:
    sdk = SdkClientFake()
    sdk.fail_duplicate = True
    client = TemporalSdkWorkflowRunClient(sdk, task_queue="spine")
    workflow_input = _input()

    with pytest.raises(TemporalWorkflowAlreadyStarted) as failure:
        await client.start_workflow(
            workflow_type="spine.workflow.run-start:v1",
            workflow_id=workflow_input.workflow_id,
            input=workflow_input,
        )

    assert failure.value.workflow_id == str(workflow_input.run_id)
