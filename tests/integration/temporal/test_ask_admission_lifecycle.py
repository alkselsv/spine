from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from temporalio.common import WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from spine.application.authorized_ask import AuthorizedAskResult
from spine.application.workflows.ask_admission_dispatch import (
    ASK_ADMISSION_WORKFLOW_TYPE,
    AskAdmissionWorkflowInput,
)
from spine.domain.capabilities.answer_question import AnswerQuestionOutcome
from spine.workflows.temporal.definitions import (
    AskAdmissionWorkflowDefinition,
    build_ask_admission_activity_bindings,
)
from tests.api.test_authorized_ask import (
    COMMAND_ID,
    MESSAGE_ID,
    RUN_ID,
    SESSION_ID,
    TRACE_ID,
    safe_output,
)

WORKFLOW_ID = "40000000-0000-0000-0000-000000000001"
TASK_QUEUE = "issue-430-ask-lifecycle"


def _workflow_input() -> AskAdmissionWorkflowInput:
    return AskAdmissionWorkflowInput(
        command_id=COMMAND_ID,
        workspace_id=UUID("10000000-0000-0000-0000-000000000001"),
        environment_id=UUID("20000000-0000-0000-0000-000000000001"),
        session_id=SESSION_ID,
        message_id=MESSAGE_ID,
        run_id=RUN_ID,
        trace_id=TRACE_ID,
    )


def _cancelled_result() -> AuthorizedAskResult:
    return AuthorizedAskResult(
        command_id=COMMAND_ID,
        session_id=SESSION_ID,
        message_id=MESSAGE_ID,
        output=safe_output(AnswerQuestionOutcome.CANCELLED),
        accepted_answer=None,
    )


class BlockingAskActivity:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.execute_calls = 0
        self.cancel_calls = 0

    async def execute(self, workflow_input: AskAdmissionWorkflowInput) -> AuthorizedAskResult:
        assert workflow_input.run_id == RUN_ID
        self.execute_calls += 1
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("blocked execution unexpectedly resumed")

    async def cancel(self, workflow_input: AskAdmissionWorkflowInput) -> AuthorizedAskResult:
        assert workflow_input.run_id == RUN_ID
        self.cancel_calls += 1
        return _cancelled_result()


@pytest.mark.asyncio
async def test_temporal_ask_workflow_cancellation_signal_persists_terminal_result() -> None:
    activity = BlockingAskActivity()
    execute_activity, cancel_activity = build_ask_admission_activity_bindings(activity)

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        async with Worker(
            environment.client,
            task_queue=TASK_QUEUE,
            workflows=[AskAdmissionWorkflowDefinition],
            activities=[execute_activity, cancel_activity],
        ):
            handle = await environment.client.start_workflow(
                ASK_ADMISSION_WORKFLOW_TYPE,
                _workflow_input().model_dump(mode="json"),
                id=WORKFLOW_ID,
                task_queue=TASK_QUEUE,
                result_type=dict,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
            await asyncio.wait_for(activity.started.wait(), timeout=10)
            await handle.signal("request_cancellation")
            result = await asyncio.wait_for(handle.result(), timeout=10)

    assert result["output"]["outcome"] == AnswerQuestionOutcome.CANCELLED.value
    assert activity.execute_calls == 1
    assert activity.cancel_calls == 1


