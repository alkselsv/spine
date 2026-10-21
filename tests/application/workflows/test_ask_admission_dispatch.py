from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.diagnostics.context import DiagnosticContext
from spine.application.outbox.dispatch import TerminalOutboxConsumerFailure
from spine.application.outbox.dispatch import RetryableOutboxConsumerFailure
from spine.application.persistence.ask_admission import (
    AskAdmissionDispatchPayload,
    AskCaptureSubscription,
    AskCancellationDispatchPayload,
    QA_ASK_ADMISSION_EVENT_SCHEMA_VERSION,
    QA_ASK_ADMISSION_EVENT_TYPE,
    QA_ASK_CANCELLATION_EVENT_SCHEMA_VERSION,
    QA_ASK_CANCELLATION_EVENT_TYPE,
)
from spine.domain.common import ActorKind, ActorRef, EnvironmentKind
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
    AskAdmissionWorkflowExecutor,
    AskAdmissionWorkflowHandle,
    AskAdmissionWorkflowInput,
    AskAdmissionWorkflowLauncher,
    AskCancellationDispatchConsumer,
    ScopedAskAdmissionCommandLoader,
    TemporalWorkflowAlreadyStarted,
    reconcile_ask_admissions,
    workflow_id_for_run,
)
from spine.application.authorized_ask import AuthorizedAskResult
from spine.application.qa_observation.contracts import CaptureEnvelope
from tests.application.qa_observation.factories import (
    execution_identity,
    same_execution_capture,
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
        causation_id=UUID("80000000-0000-0000-0000-000000000427"),
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


def test_capture_enabled_dispatch_uses_compatible_v2_workflow_metadata() -> None:
    capture_id = UUID("90000000-0000-0000-0000-000000000427")
    registry = OutboxEventRegistry()
    from spine.application.persistence.ask_admission import (
        register_ask_admission_outbox_event,
    )

    register_ask_admission_outbox_event(registry)
    intent = registry.build_intent(
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
            capture_id=capture_id,
            schema_version=2,
        ),
        aggregate=OpaqueObjectReference(
            object_type="answer_question_run", object_id=RUN_ID, schema_version=1
        ),
        producer_deduplication_id="qa-ask-admission:observer-427",
        trace_id=TRACE_ID,
    )

    workflow = AskAdmissionWorkflowInput.from_intent(intent)
    restored = AskAdmissionWorkflowInput.model_validate_json(
        workflow.model_dump_json()
    )

    assert restored.schema_version == 2
    assert restored.capture_id == capture_id
    assert "question" not in restored.model_dump(mode="json")


def test_observer_subscription_dispatch_uses_typed_v3_routing_metadata() -> None:
    subscription = AskCaptureSubscription(
        capture_id=UUID("90000000-0000-0000-0000-000000000427"),
        subscription_id=UUID("91000000-0000-0000-0000-000000000427"),
        expires_at=NOW + timedelta(minutes=5),
    )
    registry = OutboxEventRegistry()
    from spine.application.persistence.ask_admission import (
        register_ask_admission_outbox_event,
    )

    register_ask_admission_outbox_event(registry)
    intent = registry.build_intent(
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
            capture_id=subscription.capture_id,
            capture_subscription=subscription,
            schema_version=3,
        ),
        aggregate=OpaqueObjectReference(
            object_type="answer_question_run", object_id=RUN_ID, schema_version=1
        ),
        producer_deduplication_id="qa-ask-admission:observer-subscription-427",
        trace_id=TRACE_ID,
    )

    workflow = AskAdmissionWorkflowInput.from_intent(intent)
    restored = AskAdmissionWorkflowInput.model_validate_json(workflow.model_dump_json())

    assert restored.schema_version == 3
    assert restored.capture_id == subscription.capture_id
    assert restored.capture_subscription == subscription
    assert "question" not in restored.model_dump(mode="json")
    assert "answer" not in restored.model_dump(mode="json")
    assert "context_bundle" not in restored.model_dump(mode="json")


class FakeTemporal:
    def __init__(self) -> None:
        self.started: list[tuple[str, str, AskAdmissionWorkflowInput]] = []
        self.cancellation_requests: list[str] = []

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

    async def request_cancellation(self, *, workflow_id):
        self.cancellation_requests.append(workflow_id)


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


@pytest.mark.asyncio
async def test_workflow_cancel_before_start_runs_only_cancellation_activity() -> None:
    result = object()

    class Executor:
        async def execute(self, workflow_input):
            pytest.fail("cancellation before start must skip execution")

        async def cancel(self, workflow_input):
            assert workflow_input.run_id == RUN_ID
            return result

    workflow = AskAdmissionWorkflow(Executor())

    assert workflow.cancellation_requested is False
    workflow.request_cancellation()
    workflow.request_cancellation()
    assert workflow.cancellation_requested is True
    assert await workflow.execute(workflow_input()) is result


@pytest.mark.asyncio
async def test_cancellation_dispatch_signals_the_stable_temporal_workflow() -> None:
    registry = OutboxEventRegistry()
    from spine.application.persistence.ask_admission import (
        register_ask_admission_outbox_event,
    )

    register_ask_admission_outbox_event(registry)
    event_id = UUID("80000000-0000-0000-0000-000000000429")
    intent = registry.build_intent(
        event_id=event_id,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        event_type=QA_ASK_CANCELLATION_EVENT_TYPE,
        schema_version=QA_ASK_CANCELLATION_EVENT_SCHEMA_VERSION,
        payload=AskCancellationDispatchPayload(run_id=RUN_ID),
        aggregate=OpaqueObjectReference(
            object_type="answer_question_run", object_id=RUN_ID, schema_version=1
        ),
        producer_deduplication_id=f"qa-ask-cancellation:{RUN_ID}",
        trace_id=TRACE_ID,
    )
    context = DiagnosticContext(
        trace_id=TRACE_ID, correlation_id=None, causation_id=event_id
    )
    client = FakeTemporal()

    consumer = AskCancellationDispatchConsumer(client)
    delivered = await consumer.consume(intent, context)
    replayed = await consumer.consume(intent, context)

    assert delivered.diagnostic_context == context
    assert replayed.diagnostic_context == context
    assert client.cancellation_requests == [str(RUN_ID), str(RUN_ID)]


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
async def test_workflow_executor_releases_only_scoped_admitted_identity_to_capability() -> None:
    command = AskAdmissionCommandData(
        command_id=COMMAND_ID,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        session_id=SESSION_ID,
        message_id=MESSAGE_ID,
        run_id=RUN_ID,
        question="secret question",
        subject=ActorRef(kind=ActorKind.HUMAN, id=UUID(int=427)),
        environment=EnvironmentKind.PRODUCTION,
        idempotency_key="ask-427",
    )

    class Loader:
        async def load(self, workflow_input):
            assert workflow_input.model_dump().get("question") is None
            return command

    class Service:
        def __init__(self) -> None:
            self.commands = []

        async def execute_admitted(self, execution):
            self.commands.append(execution)
            return SimpleNamespace(
                command_id=execution.command_id,
                session_id=execution.session_id,
                message_id=execution.message_id,
                run_id=execution.run_id,
                attempt_id=UUID(int=RUN_ID.int + len(self.commands)),
            )

    service = Service()
    timings = []
    capture_id = UUID("a0000000-0000-0000-0000-000000000427")

    class ElapsedPort:
        async def record_worker_execution_elapsed(self, sample):
            timings.append(sample)

    executor = AskAdmissionWorkflowExecutor(
        loader=Loader(),
        service=service,
        persistence_context_factory=lambda workflow_input: object(),
        monotonic_ns=iter((100, 130, 500, 570)).__next__,
        execution_elapsed_port=ElapsedPort(),
    )

    capture_input = workflow_input().model_copy(
        update={"capture_id": capture_id, "schema_version": 2}
    )
    result = await executor.execute(capture_input)
    retry_result = await executor.execute(capture_input)

    assert result.run_id == RUN_ID
    assert retry_result.run_id == RUN_ID
    assert service.commands[0].question == "secret question"
    assert service.commands[0].message_id == MESSAGE_ID
    assert service.commands[0].run_id == RUN_ID
    assert service.commands[0].logical_execution_key == (
        f"durable-ask:{COMMAND_ID}"
    )
    assert (
        service.commands[1].logical_execution_key
        == service.commands[0].logical_execution_key
    )
    assert "secret question" not in service.commands[0].logical_execution_key
    assert [sample.run_id for sample in timings] == [RUN_ID, RUN_ID]
    assert [sample.attempt_id for sample in timings] == [
        UUID(int=RUN_ID.int + 1),
        UUID(int=RUN_ID.int + 2),
    ]
    assert [sample.worker_execution_elapsed_ns for sample in timings] == [30, 70]
    assert [sample.capture_id for sample in timings] == [capture_id, capture_id]


@pytest.mark.asyncio
async def test_workflow_executor_does_not_emit_duration_for_incomplete_attempt() -> None:
    command = AskAdmissionCommandData(
        command_id=COMMAND_ID,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        session_id=SESSION_ID,
        message_id=MESSAGE_ID,
        run_id=RUN_ID,
        question="secret question",
        subject=ActorRef(kind=ActorKind.HUMAN, id=UUID(int=427)),
        environment=EnvironmentKind.PRODUCTION,
        idempotency_key="ask-427",
    )

    class Loader:
        async def load(self, workflow_input):
            return command

    class Service:
        async def execute_admitted(self, execution):
            raise RuntimeError("incomplete attempt")

    timings = []

    class ElapsedPort:
        async def record_worker_execution_elapsed(self, sample):
            timings.append(sample)

    executor = AskAdmissionWorkflowExecutor(
        loader=Loader(),
        service=Service(),
        persistence_context_factory=lambda workflow_input: object(),
        monotonic_ns=iter((100, 130)).__next__,
        execution_elapsed_port=ElapsedPort(),
    )

    with pytest.raises(RuntimeError, match="incomplete attempt"):
        await executor.execute(workflow_input())

    assert timings == []


@pytest.mark.asyncio
async def test_workflow_executor_uses_current_authorization_generation() -> None:
    command = AskAdmissionCommandData(
        command_id=COMMAND_ID,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        session_id=SESSION_ID,
        message_id=MESSAGE_ID,
        run_id=RUN_ID,
        question="secret question",
        subject=ActorRef(kind=ActorKind.HUMAN, id=UUID(int=427)),
        environment=EnvironmentKind.PRODUCTION,
        idempotency_key="ask-427",
    )

    class Loader:
        async def load(self, workflow_input):
            return command

    class Service:
        def __init__(self) -> None:
            self.commands = []

        async def execute_admitted(self, execution):
            self.commands.append(execution)
            return SimpleNamespace(
                command_id=execution.command_id,
                session_id=execution.session_id,
                message_id=execution.message_id,
                run_id=execution.run_id,
                attempt_id=UUID(int=RUN_ID.int + 1),
            )

    resolved = []

    async def current_generation(workflow, loaded_command):
        resolved.append((workflow, loaded_command))
        return 4

    service = Service()
    executor = AskAdmissionWorkflowExecutor(
        loader=Loader(),
        service=service,
        persistence_context_factory=lambda workflow_input: object(),
        authorization_generation_resolver=current_generation,
    )

    await executor.execute(workflow_input())

    assert resolved == [(workflow_input(), command)]
    assert service.commands[0].request_context.authorization_generation == 4


@pytest.mark.asyncio
async def test_workflow_executor_cancellation_reuses_admitted_attempt_identity() -> None:
    command = AskAdmissionCommandData(
        command_id=COMMAND_ID,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        session_id=SESSION_ID,
        message_id=MESSAGE_ID,
        run_id=RUN_ID,
        question="secret question",
        subject=ActorRef(kind=ActorKind.HUMAN, id=UUID(int=427)),
        environment=EnvironmentKind.PRODUCTION,
        idempotency_key="ask-427",
    )

    class Loader:
        async def load(self, workflow_input):
            return command

    class Service:
        def __init__(self):
            self.commands = []

        async def execute_admitted(self, execution):
            self.commands.append(execution)
            return SimpleNamespace(
                command_id=execution.command_id,
                session_id=execution.session_id,
                message_id=execution.message_id,
                run_id=execution.run_id,
            )

    service = Service()
    started = []

    async def start_run(value):
        started.append(value)

    executor = AskAdmissionWorkflowExecutor(
        loader=Loader(),
        service=service,
        persistence_context_factory=lambda workflow_input: object(),
        run_starter=start_run,
    )

    await executor.cancel(workflow_input())

    cancellation = service.commands[0]
    assert cancellation.cancelled is True
    assert cancellation.logical_execution_key == f"durable-ask:{COMMAND_ID}"
    assert cancellation.idempotency_key.value == "ask-427"
    assert cancellation.question == "secret question"
    assert started == [workflow_input()]


@pytest.mark.asyncio
@pytest.mark.parametrize("publish_fails", [False, True])
async def test_observer_capture_publishes_only_after_canonical_terminal_commit(
    publish_fails: bool,
) -> None:
    identity = execution_identity()
    capture = same_execution_capture()
    subscription = AskCaptureSubscription(
        capture_id=UUID("a1000000-0000-0000-0000-000000000427"),
        subscription_id=UUID("a2000000-0000-0000-0000-000000000427"),
        expires_at=NOW + timedelta(minutes=5),
    )
    workflow = AskAdmissionWorkflowInput(
        command_id=identity.command_id,
        workspace_id=identity.workspace_id,
        environment_id=identity.environment_id,
        session_id=identity.session_id,
        message_id=identity.message_id,
        run_id=identity.run_id,
        trace_id=capture.question.correlation.trace_id,
        capture_id=subscription.capture_id,
        capture_subscription=subscription,
        schema_version=3,
    )
    command = AskAdmissionCommandData(
        command_id=identity.command_id,
        workspace_id=identity.workspace_id,
        environment_id=identity.environment_id,
        session_id=identity.session_id,
        message_id=identity.message_id,
        run_id=identity.run_id,
        question=capture.question.subject.text,
        subject=identity.requester,
        environment=EnvironmentKind.PRODUCTION,
        idempotency_key="observer-command",
    )
    committed = False
    publications: list[CaptureEnvelope] = []

    class Loader:
        async def load(self, workflow_input):
            assert workflow_input.capture_subscription == subscription
            return command

    class Service:
        async def execute_admitted(self, execution):
            nonlocal committed
            assert execution.capture_subscription == subscription
            committed = True
            return AuthorizedAskResult(
                command_id=identity.command_id,
                session_id=identity.session_id,
                message_id=identity.message_id,
                output=capture.answer,
                accepted_answer=None,
                same_execution_capture=capture,
                capture_subscription=subscription,
                attempt_number=identity.attempt_number,
            )

    class Publisher:
        async def publish(self, envelope: CaptureEnvelope):
            assert committed
            publications.append(envelope)
            if publish_fails:
                raise RuntimeError("transport failure")

    executor = AskAdmissionWorkflowExecutor(
        loader=Loader(),
        service=Service(),
        persistence_context_factory=lambda workflow_input: object(),
        monotonic_ns=iter((1_000, 1_250)).__next__,
        capture_publisher=Publisher(),
    )

    result = await executor.execute(workflow)

    assert result.output == capture.answer
    assert len(publications) == 1
    envelope = publications[0]
    assert envelope.capture == capture
    assert envelope.identity.attempt_id == identity.attempt_id
    assert envelope.identity.attempt_number == identity.attempt_number
    assert envelope.worker_execution_elapsed_ns == 250


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
