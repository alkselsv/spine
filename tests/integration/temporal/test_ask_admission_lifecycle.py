from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from temporalio.common import WorkflowIDReusePolicy
from temporalio.api.enums.v1 import EventType
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from spine.application.authorized_ask import AuthorizedAskResult
from spine.application.diagnostics import DiagnosticContext
from spine.application.persistence.ask_admission import (
    AskAdmissionDispatchPayload,
    AskCancellationDispatchPayload,
    QA_ASK_ADMISSION_EVENT_SCHEMA_VERSION,
    QA_ASK_ADMISSION_EVENT_TYPE,
    QA_ASK_CANCELLATION_EVENT_SCHEMA_VERSION,
    QA_ASK_CANCELLATION_EVENT_TYPE,
    register_ask_admission_outbox_event,
)
from spine.application.persistence.outbox import OpaqueObjectReference
from spine.application.workflows.ask_admission_dispatch import (
    ASK_ADMISSION_WORKFLOW_TYPE,
    AskAdmissionDispatchConsumer,
    AskAdmissionWorkflowLauncher,
    AskCancellationDispatchConsumer,
    AskAdmissionWorkflowInput,
)
from spine.domain.capabilities.answer_question import AnswerQuestionOutcome
from spine.workflows.temporal.definitions import (
    AskAdmissionWorkflowDefinition,
    build_ask_admission_activity_bindings,
)
from spine.workflows.temporal.client import TemporalSdkWorkflowClient
from tests.contracts.persistence.outbox_events import create_outbox_event_registry
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


def _admission_intent():
    events = create_outbox_event_registry()
    register_ask_admission_outbox_event(events)
    return events.build_intent(
        event_id=UUID("80000000-0000-0000-0000-000000000430"),
        workspace_id=_workflow_input().workspace_id,
        environment_id=_workflow_input().environment_id,
        event_type=QA_ASK_ADMISSION_EVENT_TYPE,
        schema_version=QA_ASK_ADMISSION_EVENT_SCHEMA_VERSION,
        payload=AskAdmissionDispatchPayload(
            command_id=COMMAND_ID,
            session_id=SESSION_ID,
            message_id=MESSAGE_ID,
            run_id=RUN_ID,
        ),
        aggregate=OpaqueObjectReference(
            object_type="answer_question_run",
            object_id=RUN_ID,
            schema_version=1,
        ),
        producer_deduplication_id=f"qa-ask-admission:{RUN_ID}",
        trace_id=TRACE_ID,
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


class WorkerRestartAskActivity:
    def __init__(self) -> None:
        self.recovered_attempt = asyncio.Event()
        self.execute_calls = 0
        self.cancel_calls = 0

    async def execute(self, workflow_input: AskAdmissionWorkflowInput) -> AuthorizedAskResult:
        assert workflow_input.run_id == RUN_ID
        self.execute_calls += 1
        self.recovered_attempt.set()
        return AuthorizedAskResult(
            command_id=COMMAND_ID,
            session_id=SESSION_ID,
            message_id=MESSAGE_ID,
            output=safe_output(AnswerQuestionOutcome.ABSTAINED),
            accepted_answer=None,
        )

    async def cancel(self, workflow_input: AskAdmissionWorkflowInput) -> AuthorizedAskResult:
        assert workflow_input.run_id == RUN_ID
        self.cancel_calls += 1
        return _cancelled_result()


@pytest.mark.asyncio
async def test_temporal_duplicate_admission_delivery_converges_on_one_run() -> None:
    activity = BlockingAskActivity()
    execute_activity, cancel_activity = build_ask_admission_activity_bindings(activity)
    task_queue = f"{TASK_QUEUE}-duplicate-admission"
    event = _admission_intent()
    context = DiagnosticContext(
        trace_id=TRACE_ID,
        correlation_id=event.correlation_id,
        causation_id=event.event_id,
    )

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        class RecordingTemporalClient:
            def __init__(self) -> None:
                self.start_ids: list[str] = []
                self.lookup_ids: list[str] = []

            async def start_workflow(self, *args, **kwargs):
                self.start_ids.append(kwargs["id"])
                return await environment.client.start_workflow(*args, **kwargs)

            def get_workflow_handle(self, workflow_id: str):
                self.lookup_ids.append(workflow_id)
                return environment.client.get_workflow_handle(workflow_id)

        recording_client = RecordingTemporalClient()
        temporal_client = TemporalSdkWorkflowClient(
            recording_client,
            task_queue=task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
        consumer = AskAdmissionDispatchConsumer(AskAdmissionWorkflowLauncher(temporal_client))
        async with Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[AskAdmissionWorkflowDefinition],
            activities=[execute_activity, cancel_activity],
        ):
            await consumer.consume(event, context)
            handle = environment.client.get_workflow_handle(str(RUN_ID))
            await asyncio.wait_for(activity.started.wait(), timeout=10)

            # A redelivered outbox event must resolve to the active execution
            # with the canonical run ID instead of creating a second run.
            await consumer.consume(event, context)
            await handle.signal("request_cancellation")
            result = await asyncio.wait_for(handle.result(), timeout=10)

    assert result["output"]["outcome"] == AnswerQuestionOutcome.CANCELLED.value
    assert activity.execute_calls >= 1
    assert activity.cancel_calls == 1
    assert recording_client.start_ids == [str(RUN_ID), str(RUN_ID)]
    assert recording_client.lookup_ids == [str(RUN_ID)]


@pytest.mark.asyncio
async def test_temporal_ask_workflow_cancellation_signal_returns_terminal_result() -> None:
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
    assert activity.execute_calls >= 1
    assert activity.cancel_calls == 1


@pytest.mark.asyncio
async def test_temporal_ask_workflow_resumes_after_worker_restart() -> None:
    activity = WorkerRestartAskActivity()
    execute_activity, cancel_activity = build_ask_admission_activity_bindings(activity)

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        handle = await environment.client.start_workflow(
            ASK_ADMISSION_WORKFLOW_TYPE,
            _workflow_input().model_dump(mode="json"),
            id=f"{WORKFLOW_ID}-worker-restart",
            task_queue=f"{TASK_QUEUE}-worker-restart",
            result_type=dict,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
        async with Worker(
            environment.client,
            task_queue=f"{TASK_QUEUE}-worker-restart",
            identity="issue-430-restarted-worker",
            max_cached_workflows=0,
            workflows=[AskAdmissionWorkflowDefinition],
            activities=[],
        ):
            for _ in range(100):
                history = await handle.fetch_history()
                if any(
                    event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
                    for event in history.events
                ):
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("Temporal did not persist the execution activity before worker restart")

        async with Worker(
            environment.client,
            task_queue=f"{TASK_QUEUE}-worker-restart",
            identity="issue-430-restarted-worker",
            max_cached_workflows=0,
            workflows=[AskAdmissionWorkflowDefinition],
            activities=[execute_activity, cancel_activity],
        ):
            await asyncio.wait_for(activity.recovered_attempt.wait(), timeout=10)
            result = await asyncio.wait_for(handle.result(), timeout=10)

    assert result["output"]["outcome"] == AnswerQuestionOutcome.ABSTAINED.value
    assert activity.execute_calls == 1


@pytest.mark.asyncio
async def test_temporal_ask_cancellation_resumes_after_worker_restart() -> None:
    activity = BlockingAskActivity()
    execute_activity, cancel_activity = build_ask_admission_activity_bindings(activity)
    task_queue = f"{TASK_QUEUE}-cancel-worker-restart"
    workflow_id = str(RUN_ID)

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        handle = await environment.client.start_workflow(
            ASK_ADMISSION_WORKFLOW_TYPE,
            _workflow_input().model_dump(mode="json"),
            id=workflow_id,
            task_queue=task_queue,
            result_type=dict,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
        async with Worker(
            environment.client,
            task_queue=task_queue,
            identity="issue-430-cancel-worker-restart",
            max_cached_workflows=0,
            workflows=[AskAdmissionWorkflowDefinition],
            activities=[execute_activity, cancel_activity],
        ):
            for _ in range(100):
                history = await handle.fetch_history()
                if any(
                    event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
                    for event in history.events
                ):
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("Temporal did not persist the execution activity before cancellation")
            await asyncio.wait_for(activity.started.wait(), timeout=10)
        event_id = UUID("70000000-0000-0000-0000-000000000430")
        events = create_outbox_event_registry()
        register_ask_admission_outbox_event(events)
        cancellation_intent = events.build_intent(
            event_id=event_id,
            workspace_id=_workflow_input().workspace_id,
            environment_id=_workflow_input().environment_id,
            event_type=QA_ASK_CANCELLATION_EVENT_TYPE,
            schema_version=QA_ASK_CANCELLATION_EVENT_SCHEMA_VERSION,
            payload=AskCancellationDispatchPayload(run_id=RUN_ID),
            aggregate=OpaqueObjectReference(
                object_type="answer_question_run",
                object_id=RUN_ID,
                schema_version=1,
            ),
            producer_deduplication_id=f"qa-ask-cancellation:{RUN_ID}",
            trace_id=TRACE_ID,
        )
        dispatch = AskCancellationDispatchConsumer(
            TemporalSdkWorkflowClient(
                environment.client,
                task_queue=task_queue,
            )
        )
        diagnostic_context = DiagnosticContext(
            trace_id=TRACE_ID,
            correlation_id=_workflow_input().correlation_id,
            causation_id=event_id,
        )

        async with Worker(
            environment.client,
            task_queue=task_queue,
            identity="issue-430-cancel-worker-restart",
            max_cached_workflows=0,
            workflows=[AskAdmissionWorkflowDefinition],
            activities=[execute_activity, cancel_activity],
        ):
            # This models the dispatcher recovering after restart and replaying
            # the committed cancellation intent through its normal consumer.
            await dispatch.consume(cancellation_intent, diagnostic_context)
            result = await asyncio.wait_for(handle.result(), timeout=10)

    assert result["output"]["outcome"] == AnswerQuestionOutcome.CANCELLED.value
    assert activity.execute_calls >= 1
    assert activity.cancel_calls == 1


