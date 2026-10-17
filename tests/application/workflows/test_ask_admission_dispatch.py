from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.diagnostics.context import DiagnosticContext
from spine.application.outbox.dispatch import TerminalOutboxConsumerFailure
from spine.application.outbox.dispatch import RetryableOutboxConsumerFailure
from spine.application.persistence.ask_admission import (
    AskAdmissionDispatchPayload,
    QA_ASK_ADMISSION_EVENT_SCHEMA_VERSION,
    QA_ASK_ADMISSION_EVENT_TYPE,
)
from spine.application.persistence.outbox import (
    OpaqueObjectReference,
    OutboxEventRegistry,
)
from spine.application.workflows.ask_admission_dispatch import (
    ASK_ADMISSION_WORKFLOW_TYPE,
    AskAdmissionCommandData,
    AskAdmissionCommandUnavailable,
    AskAdmissionDispatchConsumer,
    AskAdmissionReconciliationReport,
    AskAdmissionWorkflow,
    AskAdmissionWorkflowHandle,
    AskAdmissionWorkflowInput,
    AskAdmissionWorkflowLauncher,
    ScopedAskAdmissionCommandLoader,
    TemporalWorkflowAlreadyStarted,
    reconcile_ask_admissions,
    workflow_id_for_run,
)

WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000427")
ENVIRONMENT_ID = UUID("20000000-0000-0000-0000-000000000427")
COMMAND_ID = UUID("30000000-0000-0000-0000-000000000427")
SESSION_ID = UUID("40000000-0000-0000-0000-000000000427")
MESSAGE_ID = UUID("50000000-0000-0000-0000-000000000427")
RUN_ID = UUID("60000000-0000-0000-0000-000000000427")
TRACE_ID = UUID("70000000-0000-0000-0000-000000000427")
NOW = datetime(2026, 10, 17, tzinfo=timezone.utc)


def workflow_input() -> AskAdmissionWorkflowInput:
    return AskAdmissionWorkflowInput(
        command_id=COMMAND_ID,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        session_id=SESSION_ID,
        message_id=MESSAGE_ID,
        run_id=RUN_ID,
        trace_id=TRACE_ID,
    )


def admission_intent():
    registry = OutboxEventRegistry()
    from spine.application.persistence.ask_admission import (
        register_ask_admission_outbox_event,
    )

    register_ask_admission_outbox_event(registry)
    return registry.build_intent(
        event_id=UUID("80000000-0000-0000-0000-000000000427"),
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
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
        producer_deduplication_id="qa-ask-admission:427",
        trace_id=TRACE_ID,
    )


class FakeTemporal:
    def __init__(self) -> None:
        self.started: list[tuple[str, str, AskAdmissionWorkflowInput]] = []

    async def start_workflow(self, *, workflow_type, workflow_id, input):
        if self.started:
            raise TemporalWorkflowAlreadyStarted(workflow_id)
        assert workflow_type == ASK_ADMISSION_WORKFLOW_TYPE
        self.started.append((workflow_type, workflow_id, input))
        return AskAdmissionWorkflowHandle(workflow_id=workflow_id, run_id=input.run_id)

    async def get_workflow(self, *, workflow_id):
        for _, existing_id, input in self.started:
            if existing_id == workflow_id:
                return AskAdmissionWorkflowHandle(
                    workflow_id=existing_id, run_id=input.run_id
                )
        raise LookupError("missing workflow")


class FakeCommandSource:
    def __init__(self, command: AskAdmissionCommandData | None) -> None:
        self.command = command
        self.requested: list[tuple[UUID, UUID, UUID, UUID, UUID]] = []

    async def load_exact(self, workflow_input):
        self.requested.append(
            (
                workflow_input.workspace_id,
                workflow_input.environment_id,
                workflow_input.run_id,
                workflow_input.session_id,
                workflow_input.message_id,
            )
        )
        return self.command


class FakeAuthorizer:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed

    async def authorize(self, **kwargs):
        del kwargs
        return self.allowed


@pytest.mark.asyncio
async def test_duplicate_admission_delivery_resolves_one_stable_workflow() -> None:
    temporal = FakeTemporal()
    consumer = AskAdmissionDispatchConsumer(AskAdmissionWorkflowLauncher(temporal))
    context = DiagnosticContext(
        trace_id=TRACE_ID,
        correlation_id=None,
        causation_id=admission_intent().event_id,
    )

    await consumer.consume(admission_intent(), context)
    await consumer.consume(admission_intent(), context)

    assert len(temporal.started) == 1
    assert temporal.started[0][1] == workflow_id_for_run(RUN_ID)
    assert temporal.started[0][2].model_dump() == workflow_input().model_dump()


def test_workflow_input_contains_no_protected_question() -> None:
    assert "question" not in workflow_input().model_dump()


@pytest.mark.asyncio
async def test_invalid_intent_is_terminal_and_does_not_start_workflow() -> None:
    temporal = FakeTemporal()
    consumer = AskAdmissionDispatchConsumer(AskAdmissionWorkflowLauncher(temporal))
    intent = admission_intent().model_copy(
        update={
            "aggregate": OpaqueObjectReference(
                object_type="answer_question_run",
                object_id=UUID("90000000-0000-0000-0000-000000000427"),
                schema_version=1,
            )
        }
    )
    context = DiagnosticContext(
        trace_id=TRACE_ID,
        correlation_id=None,
        causation_id=intent.event_id,
    )

    with pytest.raises(TerminalOutboxConsumerFailure, match="qa.ask.invalid_intent"):
        await consumer.consume(intent, context)
    assert temporal.started == []


@pytest.mark.asyncio
async def test_temporal_start_failure_is_retryable_and_does_not_ack_intent() -> None:
    class UnavailableTemporal(FakeTemporal):
        async def start_workflow(self, **kwargs):
            del kwargs
            raise RuntimeError("transport unavailable")

    temporal = UnavailableTemporal()
    consumer = AskAdmissionDispatchConsumer(AskAdmissionWorkflowLauncher(temporal))
    intent = admission_intent()
    context = DiagnosticContext(
        trace_id=TRACE_ID,
        correlation_id=None,
        causation_id=intent.event_id,
    )

    with pytest.raises(
        RetryableOutboxConsumerFailure, match="qa.ask.temporal_unavailable"
    ):
        await consumer.consume(intent, context)


def test_workflow_cancellation_seam_is_deterministic_and_non_executing() -> None:
    workflow = AskAdmissionWorkflow()

    assert workflow.cancellation_requested is False
    workflow.request_cancellation()
    workflow.request_cancellation()
    assert workflow.cancellation_requested is True


@pytest.mark.asyncio
async def test_scoped_command_loader_rejects_denied_or_mismatched_command() -> None:
    command = AskAdmissionCommandData(
        command_id=COMMAND_ID,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        session_id=SESSION_ID,
        message_id=MESSAGE_ID,
        run_id=RUN_ID,
        question="protected question",
    )
    source = FakeCommandSource(command)
    loader = ScopedAskAdmissionCommandLoader(source, FakeAuthorizer(False))

    with pytest.raises(AskAdmissionCommandUnavailable):
        await loader.load(workflow_input())
    assert source.requested == [
        (WORKSPACE_ID, ENVIRONMENT_ID, RUN_ID, SESSION_ID, MESSAGE_ID)
    ]


@pytest.mark.asyncio
async def test_reconciliation_retries_committed_admission_without_second_start() -> (
    None
):
    temporal = FakeTemporal()
    launcher = AskAdmissionWorkflowLauncher(temporal)

    class Source:
        async def list_unstarted_admissions(self, *, limit: int):
            assert limit == 2
            return (workflow_input(), workflow_input())

    report = await reconcile_ask_admissions(Source(), launcher, limit=2)

    assert report == AskAdmissionReconciliationReport(
        inspected=2, started=1, resumed=1, failed=0
    )
    assert len(temporal.started) == 1
