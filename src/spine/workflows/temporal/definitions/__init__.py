"""Temporal SDK workflow and activity names for durable Ask execution."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Protocol
from uuid import UUID

from temporalio import activity, workflow
from temporalio.exceptions import (
    ActivityError,
    CancelledError as TemporalCancelledError,
)

from spine.application.authorized_ask import AuthorizedAskResult
from spine.application.workflows.ask_admission_dispatch import (
    ASK_ADMISSION_WORKFLOW_TYPE,
    AskAdmissionWorkflow,
    AskAdmissionWorkflowInput,
)
from spine.domain.capabilities.answer_disclosure import AcceptedGroundedAnswer
from spine.domain.capabilities.answer_question import AnswerQuestionOutput

ASK_EXECUTE_ACTIVITY_NAME = "spine.qa.ask.execute:v1"
ASK_CANCEL_ACTIVITY_NAME = "spine.qa.ask.cancel:v1"
_ACTIVITY_START_TO_CLOSE_TIMEOUT = timedelta(minutes=15)


def _result_to_payload(result: AuthorizedAskResult) -> dict[str, object]:
    return {
        "command_id": str(result.command_id),
        "session_id": str(result.session_id),
        "message_id": str(result.message_id),
        "output": result.output.model_dump(mode="json"),
        "accepted_answer": (
            result.accepted_answer.model_dump(mode="json")
            if result.accepted_answer is not None
            else None
        ),
        "replay": result.replay,
    }


def _result_from_payload(payload: dict[str, object]) -> AuthorizedAskResult:
    output = AnswerQuestionOutput.model_validate_json(json.dumps(payload["output"]))
    accepted_answer_payload = payload["accepted_answer"]
    accepted_answer = (
        AcceptedGroundedAnswer.model_validate_json(
            json.dumps(accepted_answer_payload)
        )
        if accepted_answer_payload is not None
        else None
    )
    return AuthorizedAskResult(
        command_id=UUID(str(payload["command_id"])),
        session_id=UUID(str(payload["session_id"])),
        message_id=UUID(str(payload["message_id"])),
        output=output,
        accepted_answer=accepted_answer,
        replay=bool(payload["replay"]),
    )


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
            return _result_from_payload(cancelled)
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
            except ActivityError as error:
                if not isinstance(error.cause, TemporalCancelledError):
                    raise
            except (asyncio.CancelledError, TemporalCancelledError):
                pass
            cancelled = await workflow.execute_activity(
                ASK_CANCEL_ACTIVITY_NAME,
                workflow_input.model_dump(mode="json"),
                start_to_close_timeout=_ACTIVITY_START_TO_CLOSE_TIMEOUT,
                result_type=dict,
            )
            return _result_from_payload(cancelled)
        result = await execute_task
        return _result_from_payload(result)


def build_ask_admission_activity_bindings(
    service: AskAdmissionActivity,
) -> tuple[object, object]:
    """Adapt the application service to explicitly named Temporal activities."""

    def parse_workflow_input(payload: dict[str, object]) -> AskAdmissionWorkflowInput:
        # Temporal's JSON payload converter decodes UUID values as strings. Use
        # Pydantic's JSON path so strict models accept their declared wire form.
        return AskAdmissionWorkflowInput.model_validate_json(json.dumps(payload))

    @activity.defn(name=ASK_EXECUTE_ACTIVITY_NAME)
    async def execute(payload: dict[str, object]) -> dict[str, object]:
        result = await service.execute(parse_workflow_input(payload))
        return _result_to_payload(result)

    @activity.defn(name=ASK_CANCEL_ACTIVITY_NAME)
    async def cancel(payload: dict[str, object]) -> dict[str, object]:
        result = await service.cancel(parse_workflow_input(payload))
        return _result_to_payload(result)

    return execute, cancel


__all__ = [
    "ASK_CANCEL_ACTIVITY_NAME",
    "ASK_EXECUTE_ACTIVITY_NAME",
    "AskAdmissionActivity",
    "AskAdmissionWorkflowDefinition",
    "build_ask_admission_activity_bindings",
]
