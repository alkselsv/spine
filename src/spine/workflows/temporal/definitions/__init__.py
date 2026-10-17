"""Temporal SDK workflow and activity names for durable Ask execution."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Protocol

from temporalio import activity, workflow

from spine.application.authorized_ask import AuthorizedAskResult
from spine.application.workflows.ask_admission_dispatch import (
    ASK_ADMISSION_WORKFLOW_TYPE,
    AskAdmissionWorkflow,
    AskAdmissionWorkflowInput,
)

ASK_EXECUTE_ACTIVITY_NAME = "spine.qa.ask.execute:v1"
ASK_CANCEL_ACTIVITY_NAME = "spine.qa.ask.cancel:v1"
_ACTIVITY_START_TO_CLOSE_TIMEOUT = timedelta(minutes=15)


class AskAdmissionActivity(Protocol):
    async def execute(
        self, workflow_input: AskAdmissionWorkflowInput
    ) -> AuthorizedAskResult: ...

    async def cancel(
        self, workflow_input: AskAdmissionWorkflowInput
    ) -> AuthorizedAskResult: ...


@workflow.defn(name=ASK_ADMISSION_WORKFLOW_TYPE)
class AskAdmissionWorkflowDefinition:
    """Durable execution that arbitrates cancellation through Temporal signals."""

    def __init__(self) -> None:
        self._lifecycle = AskAdmissionWorkflow()

    @workflow.signal
    def request_cancellation(self) -> None:
        self._lifecycle.request_cancellation()

    @workflow.run
    async def run(self, workflow_input: AskAdmissionWorkflowInput) -> AuthorizedAskResult:
        if self._lifecycle.cancellation_requested:
            cancelled = await workflow.execute_activity(
                ASK_CANCEL_ACTIVITY_NAME,
                workflow_input.model_dump(mode="json"),
                start_to_close_timeout=_ACTIVITY_START_TO_CLOSE_TIMEOUT,
                result_type=dict,
            )
            return AuthorizedAskResult.model_validate(cancelled)
        execute_task = asyncio.create_task(
            workflow.execute_activity(
                ASK_EXECUTE_ACTIVITY_NAME,
                workflow_input.model_dump(mode="json"),
                start_to_close_timeout=_ACTIVITY_START_TO_CLOSE_TIMEOUT,
                result_type=dict,
            )
        )
        await workflow.wait_condition(
            lambda: self._lifecycle.cancellation_requested or execute_task.done()
        )
        if self._lifecycle.cancellation_requested:
            execute_task.cancel()
            try:
                await execute_task
            except asyncio.CancelledError:
                pass
            cancelled = await workflow.execute_activity(
                ASK_CANCEL_ACTIVITY_NAME,
                workflow_input.model_dump(mode="json"),
                start_to_close_timeout=_ACTIVITY_START_TO_CLOSE_TIMEOUT,
                result_type=dict,
            )
            return AuthorizedAskResult.model_validate(cancelled)
        result = await execute_task
        return AuthorizedAskResult.model_validate(result)


def build_ask_admission_activity_bindings(
    service: AskAdmissionActivity,
) -> tuple[object, object]:
    """Adapt the application service to explicitly named Temporal activities."""

    @activity.defn(name=ASK_EXECUTE_ACTIVITY_NAME)
    async def execute(payload: dict[str, object]) -> dict[str, object]:
        result = await service.execute(AskAdmissionWorkflowInput.model_validate(payload))
        return result.model_dump(mode="json")

    @activity.defn(name=ASK_CANCEL_ACTIVITY_NAME)
    async def cancel(payload: dict[str, object]) -> dict[str, object]:
        result = await service.cancel(AskAdmissionWorkflowInput.model_validate(payload))
        return result.model_dump(mode="json")

    return execute, cancel


__all__ = [
    "ASK_CANCEL_ACTIVITY_NAME",
    "ASK_EXECUTE_ACTIVITY_NAME",
    "AskAdmissionActivity",
    "AskAdmissionWorkflowDefinition",
    "build_ask_admission_activity_bindings",
]
