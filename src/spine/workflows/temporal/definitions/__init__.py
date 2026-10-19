"""Temporal SDK workflow and activity names for durable Ask execution."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Literal, Protocol, Union
from uuid import UUID

from pydantic import ConfigDict
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
from spine.domain.capabilities.answer_question import AnswerQuestionOutcome
from spine.domain.common import DefinitionModel

ASK_EXECUTE_ACTIVITY_NAME = "spine.qa.ask.execute:v1"
ASK_CANCEL_ACTIVITY_NAME = "spine.qa.ask.cancel:v1"
_ACTIVITY_START_TO_CLOSE_TIMEOUT = timedelta(minutes=15)


class AskAdmissionActivityResult(DefinitionModel):
    """Legacy v1 payload retained only to decode already-recorded histories."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["spine.qa.ask-activity-result:v1"] = (
        "spine.qa.ask-activity-result:v1"
    )
    command_id: UUID
    session_id: UUID
    message_id: UUID
    output: AnswerQuestionOutput
    accepted_answer: AcceptedGroundedAnswer | None
    replay: bool


class SafeAskActivityResult(DefinitionModel):
    """Content-free terminal acknowledgement safe for Temporal history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["spine.qa.ask-activity-result:v2"] = (
        "spine.qa.ask-activity-result:v2"
    )
    command_id: UUID
    session_id: UUID
    message_id: UUID
    run_id: UUID
    attempt_id: UUID
    outcome: AnswerQuestionOutcome
    replay: bool


def _serialize_ask_admission_activity_result(
    result: AuthorizedAskResult,
) -> dict[str, object]:
    payload = SafeAskActivityResult(
        command_id=result.command_id,
        session_id=result.session_id,
        message_id=result.message_id,
        run_id=result.run_id,
        attempt_id=result.attempt_id,
        outcome=result.output.outcome,
        replay=result.replay,
    )
    return payload.model_dump(mode="json")


def _deserialize_ask_admission_activity_result(
    payload: dict[str, object],
) -> Union[AuthorizedAskResult, SafeAskActivityResult]:
    if payload.get("schema_version") == "spine.qa.ask-activity-result:v1":
        result = AskAdmissionActivityResult.model_validate_json(json.dumps(payload))
        return AuthorizedAskResult(
            command_id=result.command_id,
            session_id=result.session_id,
            message_id=result.message_id,
            output=result.output,
            accepted_answer=result.accepted_answer,
            replay=result.replay,
        )
    return SafeAskActivityResult.model_validate_json(json.dumps(payload))


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
    async def run(
        self, workflow_input: AskAdmissionWorkflowInput
    ) -> Union[AuthorizedAskResult, SafeAskActivityResult]:
        if self._lifecycle.cancellation_requested:
            cancelled = await workflow.execute_activity(
                ASK_CANCEL_ACTIVITY_NAME,
                workflow_input.model_dump(mode="json"),
                start_to_close_timeout=_ACTIVITY_START_TO_CLOSE_TIMEOUT,
                result_type=dict,
            )
            return _deserialize_ask_admission_activity_result(cancelled)
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
            return _deserialize_ask_admission_activity_result(cancelled)
        result = await execute_task
        return _deserialize_ask_admission_activity_result(result)


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
        return _serialize_ask_admission_activity_result(result)

    @activity.defn(name=ASK_CANCEL_ACTIVITY_NAME)
    async def cancel(payload: dict[str, object]) -> dict[str, object]:
        result = await service.cancel(parse_workflow_input(payload))
        return _serialize_ask_admission_activity_result(result)

    return execute, cancel


__all__ = [
    "ASK_CANCEL_ACTIVITY_NAME",
    "ASK_EXECUTE_ACTIVITY_NAME",
    "AskAdmissionActivity",
    "AskAdmissionWorkflowDefinition",
    "build_ask_admission_activity_bindings",
]
