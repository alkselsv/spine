"""SDK-facing Temporal client bridge for the Ask workflow port.

The adapter deliberately accepts the installed SDK client as an opaque object.
The composition root supplies the SDK's ID-reuse policy and task queue, while
the application layer sees only typed Spine workflow identities.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from spine.application.workflows.ask_admission_dispatch import (
    ASK_ADMISSION_WORKFLOW_TYPE,
    AskAdmissionWorkflowHandle,
    AskAdmissionWorkflowInput,
    AskAdmissionWorkflowClient,
    TemporalWorkflowAlreadyStarted,
)


class TemporalSdkClient(Protocol):
    async def start_workflow(
        self,
        workflow: str,
        arg: object,
        *,
        id: str,
        task_queue: str,
        id_reuse_policy: object | None = None,
    ) -> object: ...

    def get_workflow_handle(self, workflow_id: str) -> object: ...


class TemporalSdkWorkflowHandle(Protocol):
    async def signal(self, signal: str, arg: object | None = None) -> None: ...

    async def describe(self) -> object: ...


class TemporalSdkWorkflowClient(AskAdmissionWorkflowClient):
    """Adapt the official SDK client without leaking its types inward."""

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
        input: AskAdmissionWorkflowInput,
    ) -> AskAdmissionWorkflowHandle:
        if workflow_type != ASK_ADMISSION_WORKFLOW_TYPE:
            raise ValueError("Unsupported Ask workflow type")
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
        return AskAdmissionWorkflowHandle(workflow_id=workflow_id, run_id=input.run_id)

    async def get_workflow(self, *, workflow_id: str) -> AskAdmissionWorkflowHandle:
        self._client.get_workflow_handle(workflow_id)
        return AskAdmissionWorkflowHandle(
            workflow_id=workflow_id,
            run_id=UUID(workflow_id),
        )

    async def request_cancellation(self, *, workflow_id: str) -> None:
        """Signal the already-started workflow; outbox retry covers start ordering."""

        handle = self._client.get_workflow_handle(workflow_id)
        description = await handle.describe()  # type: ignore[attr-defined]
        status = getattr(description, "status", None)
        status_name = getattr(status, "name", str(status)).lower()
        if status_name not in {"running", "workflowexecutionstatus.running"}:
            return
        await handle.signal("request_cancellation")  # type: ignore[attr-defined]


__all__ = ["TemporalSdkClient", "TemporalSdkWorkflowClient"]
