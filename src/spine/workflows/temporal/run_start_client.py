"""Temporal SDK adapter for the canonical Workflow Run start port."""

from __future__ import annotations

from uuid import UUID

from spine.application.workflows.run_start_dispatch import (
    TemporalWorkflowAlreadyStarted,
    WorkflowRunStartHandle,
    WorkflowRunStartInput,
    WorkflowRunStartClient,
    workflow_id_for_run,
    WORKFLOW_RUN_START_TYPE,
)
from spine.application.persistence.workflow_runs import (
    ClarificationAnsweredDispatchPayload,
    ClarificationRequestedDispatchPayload,
    WorkflowCancelledDispatchPayload,
    RequirementsAcceptedDispatchPayload,
)
from spine.workflows.temporal.client import TemporalSdkClient


class TemporalSdkWorkflowRunClient(WorkflowRunStartClient):
    def __init__(
        self,
        client: TemporalSdkClient,
        *,
        task_queue: str,
        id_reuse_policy: object | None = None,
    ) -> None:
        if not task_queue or len(task_queue) > 255:
            raise ValueError("Temporal task_queue must be a bounded non-blank string")
        self._client = client
        self._task_queue = task_queue
        self._id_reuse_policy = id_reuse_policy

    async def start_workflow(
        self,
        *,
        workflow_type: str,
        workflow_id: str,
        input: WorkflowRunStartInput,
    ) -> WorkflowRunStartHandle:
        if workflow_type != WORKFLOW_RUN_START_TYPE or workflow_id != input.workflow_id:
            raise ValueError("Workflow Run start identity is invalid")
        try:
            await self._client.start_workflow(
                workflow_type,
                input.model_dump(mode="json"),
                id=workflow_id,
                task_queue=self._task_queue,
                id_reuse_policy=self._id_reuse_policy,
            )
        except Exception as error:
            if error.__class__.__name__ == "WorkflowAlreadyStartedError":
                raise TemporalWorkflowAlreadyStarted(workflow_id) from None
            raise
        return WorkflowRunStartHandle(workflow_id=workflow_id, run_id=input.run_id)

    async def get_workflow(self, *, workflow_id: str) -> WorkflowRunStartHandle:
        self._client.get_workflow_handle(workflow_id)
        return WorkflowRunStartHandle(
            workflow_id=workflow_id,
            run_id=UUID(workflow_id),
        )

    async def signal_clarification_requested(
        self, payload: ClarificationRequestedDispatchPayload
    ) -> None:
        await self._signal(
            run_id=payload.run_id,
            signal="clarification_requested",
            payload=payload.model_dump(mode="json"),
        )

    async def signal_clarification_answered(
        self, payload: ClarificationAnsweredDispatchPayload
    ) -> None:
        await self._signal(
            run_id=payload.run_id,
            signal="clarification_answered",
            payload=payload.model_dump(mode="json"),
        )

    async def signal_run_cancelled(
        self, payload: WorkflowCancelledDispatchPayload
    ) -> None:
        await self._signal(
            run_id=payload.run_id,
            signal="run_cancelled",
            payload=payload.model_dump(mode="json"),
        )

    async def signal_requirements_accepted(
        self, payload: RequirementsAcceptedDispatchPayload
    ) -> None:
        await self._signal(
            run_id=payload.run_id,
            signal="requirements_accepted",
            payload=payload.model_dump(mode="json"),
        )

    async def _signal(self, *, run_id: UUID, signal: str, payload: object) -> None:
        handle = self._client.get_workflow_handle(workflow_id_for_run(run_id))
        await handle.signal(signal, payload)  # type: ignore[attr-defined]


__all__ = ["TemporalSdkWorkflowRunClient"]
