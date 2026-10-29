from uuid import UUID

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from spine.application.persistence.workflow_runs import (
    ClarificationAnsweredDispatchPayload,
    ClarificationRequestedDispatchPayload,
    WorkflowCancelledDispatchPayload,
)
from spine.application.workflows.run_start_dispatch import WorkflowRunStartInput
from spine.workflows.temporal.requirements import RequirementsWorkflowDefinition


RUN_ID = UUID("64300000-0000-4000-8000-000000000001")
VERSION_ID = UUID("64300000-0000-4000-8000-000000000002")
CLARIFICATION_ID = UUID("64300000-0000-4000-8000-000000000003")
ARTIFACT_ID = UUID("64300000-0000-4000-8000-000000000004")
COMMAND_ID = UUID("64300000-0000-4000-8000-000000000005")
NEXT_CLARIFICATION_ID = UUID("64300000-0000-4000-8000-000000000006")


def workflow_input() -> WorkflowRunStartInput:
    return WorkflowRunStartInput(
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        command_id=UUID(int=3),
        run_id=RUN_ID,
        work_item_id=UUID(int=4),
        workflow_key="commercial.requirements",
        workflow_version_id=VERSION_ID,
        workflow_version="1.0.0",
        trace_id=UUID(int=5),
    )


@pytest.mark.asyncio
async def test_temporal_clarification_wait_resumes_the_same_pinned_run() -> None:
    async with await WorkflowEnvironment.start_time_skipping() as environment:
        async with Worker(
            environment.client,
            task_queue="requirements-643",
            workflows=[RequirementsWorkflowDefinition],
        ):
            handle = await environment.client.start_workflow(
                RequirementsWorkflowDefinition.run,
                workflow_input().model_dump(mode="json"),
                id=str(RUN_ID),
                task_queue="requirements-643",
            )
            await handle.signal(
                RequirementsWorkflowDefinition.clarification_requested,
                ClarificationRequestedDispatchPayload(
                    run_id=RUN_ID,
                    clarification_id=CLARIFICATION_ID,
                    artifact_id=ARTIFACT_ID,
                    artifact_version=1,
                    workflow_version_id=VERSION_ID,
                ).model_dump(mode="json"),
            )
            waiting = await handle.query(RequirementsWorkflowDefinition.status)
            await handle.signal(
                RequirementsWorkflowDefinition.clarification_answered,
                ClarificationAnsweredDispatchPayload(
                    command_id=COMMAND_ID,
                    run_id=RUN_ID,
                    clarification_id=CLARIFICATION_ID,
                    artifact_id=ARTIFACT_ID,
                    artifact_version=2,
                    workflow_version_id=VERSION_ID,
                ).model_dump(mode="json"),
            )
            resumed = await handle.query(RequirementsWorkflowDefinition.status)

            assert waiting["state"] == "waiting"
            assert resumed["state"] == "running"
            assert resumed["run_id"] == str(RUN_ID)
            assert resumed["workflow_version_id"] == str(VERSION_ID)
            assert resumed["artifact_version"] == 2
            assert resumed["last_command_id"] == str(COMMAND_ID)

            await handle.signal(
                RequirementsWorkflowDefinition.run_cancelled,
                WorkflowCancelledDispatchPayload(
                    command_id=UUID(int=6),
                    run_id=RUN_ID,
                    workflow_version_id=VERSION_ID,
                ).model_dump(mode="json"),
            )
            result = await handle.result()
            assert result["state"] == "cancelled"


@pytest.mark.asyncio
async def test_temporal_cancellation_is_durable_while_waiting() -> None:
    async with await WorkflowEnvironment.start_time_skipping() as environment:
        async with Worker(
            environment.client,
            task_queue="requirements-cancel-643",
            workflows=[RequirementsWorkflowDefinition],
        ):
            handle = await environment.client.start_workflow(
                RequirementsWorkflowDefinition.run,
                workflow_input().model_dump(mode="json"),
                id=f"{RUN_ID}-cancel",
                task_queue="requirements-cancel-643",
            )
            await handle.signal(
                RequirementsWorkflowDefinition.clarification_requested,
                ClarificationRequestedDispatchPayload(
                    run_id=RUN_ID,
                    clarification_id=CLARIFICATION_ID,
                    artifact_id=ARTIFACT_ID,
                    artifact_version=1,
                    workflow_version_id=VERSION_ID,
                ).model_dump(mode="json"),
            )
            await handle.signal(
                RequirementsWorkflowDefinition.run_cancelled,
                WorkflowCancelledDispatchPayload(
                    command_id=UUID(int=7),
                    run_id=RUN_ID,
                    workflow_version_id=VERSION_ID,
                ).model_dump(mode="json"),
            )

            result = await handle.result()
            assert result["state"] == "cancelled"
            assert result["clarification_id"] == str(CLARIFICATION_ID)


@pytest.mark.asyncio
async def test_temporal_wait_survives_worker_restart_and_sequences_questions() -> None:
    task_queue = "requirements-restart-643"
    async with await WorkflowEnvironment.start_time_skipping() as environment:
        async with Worker(
            environment.client,
            task_queue=task_queue,
            identity="requirements-643-restarted-worker",
            max_cached_workflows=0,
            workflows=[RequirementsWorkflowDefinition],
        ):
            handle = await environment.client.start_workflow(
                RequirementsWorkflowDefinition.run,
                workflow_input().model_dump(mode="json"),
                id=f"{RUN_ID}-restart",
                task_queue=task_queue,
            )
            await handle.signal(
                RequirementsWorkflowDefinition.clarification_requested,
                ClarificationRequestedDispatchPayload(
                    run_id=RUN_ID,
                    clarification_id=CLARIFICATION_ID,
                    artifact_id=ARTIFACT_ID,
                    artifact_version=1,
                    workflow_version_id=VERSION_ID,
                ).model_dump(mode="json"),
            )
            assert (await handle.query(RequirementsWorkflowDefinition.status))["state"] == "waiting"

        async with Worker(
            environment.client,
            task_queue=task_queue,
            identity="requirements-643-restarted-worker",
            max_cached_workflows=0,
            workflows=[RequirementsWorkflowDefinition],
        ):
            await handle.signal(
                RequirementsWorkflowDefinition.clarification_answered,
                ClarificationAnsweredDispatchPayload(
                    command_id=COMMAND_ID,
                    run_id=RUN_ID,
                    clarification_id=CLARIFICATION_ID,
                    artifact_id=ARTIFACT_ID,
                    artifact_version=2,
                    next_clarification_id=NEXT_CLARIFICATION_ID,
                    workflow_version_id=VERSION_ID,
                ).model_dump(mode="json"),
            )
            status = await handle.query(RequirementsWorkflowDefinition.status)
            assert status["state"] == "waiting"
            assert status["clarification_id"] == str(NEXT_CLARIFICATION_ID)
            assert status["artifact_version"] == 2
            await handle.signal(
                RequirementsWorkflowDefinition.run_cancelled,
                WorkflowCancelledDispatchPayload(
                    command_id=UUID(int=8),
                    run_id=RUN_ID,
                    workflow_version_id=VERSION_ID,
                ).model_dump(mode="json"),
            )
            assert (await handle.result())["state"] == "cancelled"
