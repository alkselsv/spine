from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.workflow_runs import (
    AddContextCommandV1,
    AnswerClarificationCommandV1,
    ConfiguredWorkflowVersionResolver,
    PinnedWorkflowVersion,
    StartWorkflowCommand,
    WorkflowCommand,
    WorkflowArtifactRef,
    WorkflowVersionUnavailableError,
    WorkflowRunAdmissionService,
    workflow_run_snapshot,
)
from spine.domain.common import ActorKind, ActorRef, EnvironmentKind
from spine.domain.workflows import (
    ClientRequestV1,
    WorkflowCommandReceipt,
    WorkflowRun,
    WorkflowRunState,
)
from spine.domain.workflows import WorkflowEvent, WorkflowEventPage


def test_add_context_command_is_versioned_typed_and_rejects_unknown_fields() -> None:
    command = WorkflowCommand(
        schema_version=1,
        command_id=UUID("63900000-0000-4000-8000-000000000001"),
        idempotency_key="context-639",
        run_id=UUID("63900000-0000-4000-8000-000000000002"),
        workspace_id=UUID("63900000-0000-4000-8000-000000000003"),
        environment_id=UUID("63900000-0000-4000-8000-000000000004"),
        actor=ActorRef(
            kind=ActorKind.HUMAN,
            id=UUID("63900000-0000-4000-8000-000000000005"),
        ),
        command_type="add_context",
        command_schema_version=1,
        expected_run_version=1,
        payload=AddContextCommandV1(message="The launch date is 20 December."),
        artifact_refs=(),
        trace_id=UUID("63900000-0000-4000-8000-000000000006"),
        correlation_id=UUID("63900000-0000-4000-8000-000000000007"),
        causation_id=UUID("63900000-0000-4000-8000-000000000008"),
    )

    assert command.payload.message == "The launch date is 20 December."
    with pytest.raises(ValueError):
        WorkflowCommand.model_validate(
            {**command.model_dump(mode="python"), "unexpected": "rejected"}
        )


def test_answer_clarification_command_requires_exact_originating_artifact() -> None:
    command = WorkflowCommand(
        command_id=UUID("64300000-0000-4000-8000-000000000001"),
        idempotency_key="clarification-643",
        run_id=UUID("64300000-0000-4000-8000-000000000002"),
        workspace_id=UUID("64300000-0000-4000-8000-000000000003"),
        environment_id=UUID("64300000-0000-4000-8000-000000000004"),
        actor=ActorRef(kind=ActorKind.HUMAN, id=UUID("64300000-0000-4000-8000-000000000005")),
        command_type="answer_clarification",
        command_schema_version=1,
        expected_run_version=2,
        payload=AnswerClarificationCommandV1(
            clarification_id=UUID("64300000-0000-4000-8000-000000000006"),
            requirement_id=UUID("64300000-0000-4000-8000-000000000007"),
            answer="The delivery deadline is December 20.",
        ),
        artifact_refs=(WorkflowArtifactRef(
            artifact_id=UUID("64300000-0000-4000-8000-000000000008"),
            schema_name="requirements",
            schema_version="requirements:v1",
            artifact_version=1,
        ),),
        trace_id=UUID("64300000-0000-4000-8000-000000000009"),
        correlation_id=UUID("64300000-0000-4000-8000-000000000010"),
        causation_id=UUID("64300000-0000-4000-8000-000000000011"),
    )

    assert command.payload.answer == "The delivery deadline is December 20."
    with pytest.raises(ValueError, match="exact requirements artifact"):
        WorkflowCommand.model_validate(
            command.model_dump(mode="python") | {"artifact_refs": ()}
        )


class AdmissionPort:
    def __init__(self, receipt: WorkflowCommandReceipt) -> None:
        self.receipt = receipt
        self.commands: list[StartWorkflowCommand] = []
        self.continuations: list[WorkflowCommand] = []

    async def admit(self, command: StartWorkflowCommand) -> WorkflowCommandReceipt:
        self.commands.append(command)
        return self.receipt

    async def get_run(self, *, workspace_id: UUID, environment_id: UUID, run_id: UUID):
        return None

    async def dispatch(self, command: WorkflowCommand) -> WorkflowCommandReceipt:
        self.continuations.append(command)
        return self.receipt

    async def list_events(
        self, *, workspace_id: UUID, environment_id: UUID, run_id: UUID,
        after_sequence: int | None, limit: int,
    ) -> WorkflowEventPage:
        del workspace_id, environment_id, run_id, after_sequence, limit
        return WorkflowEventPage(
            items=(
                WorkflowEvent(
                    event_id=UUID("63900000-0000-4000-8000-000000000020"),
                    run_id=UUID("63900000-0000-4000-8000-000000000002"),
                    sequence=1,
                    event_type="workflow.context-added",
                    event_schema_version=1,
                    run_version=2,
                    summary="Additional context received.",
                    correlation_id=UUID("63900000-0000-4000-8000-000000000007"),
                    causation_id=UUID("63900000-0000-4000-8000-000000000001"),
                    occurred_at=datetime(2026, 10, 28, tzinfo=timezone.utc),
                ),
            ),
            next_sequence=None,
        )


@pytest.mark.asyncio
async def test_dispatch_and_event_read_use_the_shared_application_boundary() -> None:
    receipt = WorkflowCommandReceipt(
        command_id=UUID("63900000-0000-4000-8000-000000000001"),
        work_item_id=UUID("63900000-0000-4000-8000-000000000009"),
        run_id=UUID("63900000-0000-4000-8000-000000000002"),
        workflow_version_id=UUID("63900000-0000-4000-8000-000000000010"),
        state=WorkflowRunState.RUNNING,
        run_version=2,
        admission="accepted",
    )
    port = AdmissionPort(receipt)
    service = WorkflowRunAdmissionService(port)
    command = WorkflowCommand(
        command_id=receipt.command_id,
        idempotency_key="context-639",
        run_id=receipt.run_id,
        workspace_id=UUID("63900000-0000-4000-8000-000000000003"),
        environment_id=UUID("63900000-0000-4000-8000-000000000004"),
        actor=ActorRef(kind=ActorKind.HUMAN, id=UUID("63900000-0000-4000-8000-000000000005")),
        command_type="add_context",
        command_schema_version=1,
        expected_run_version=1,
        payload=AddContextCommandV1(message="More context"),
        trace_id=UUID("63900000-0000-4000-8000-000000000006"),
        correlation_id=UUID("63900000-0000-4000-8000-000000000007"),
        causation_id=UUID("63900000-0000-4000-8000-000000000008"),
    )

    assert await service.dispatch(command) == receipt
    page = await service.list_events(
        workspace_id=command.workspace_id,
        environment_id=command.environment_id,
        run_id=command.run_id,
        after_sequence=None,
        limit=20,
    )

    assert port.continuations == [command]
    assert page.items[0].summary == "Additional context received."


def _command() -> StartWorkflowCommand:
    return StartWorkflowCommand(
        command_id=UUID("60000000-0000-4000-8000-000000000001"),
        idempotency_key="start-001",
        workspace_id=UUID("60000000-0000-4000-8000-000000000002"),
        environment_id=UUID("60000000-0000-4000-8000-000000000003"),
        actor=ActorRef(
            kind=ActorKind.HUMAN,
            id=UUID("60000000-0000-4000-8000-000000000004"),
        ),
        environment=EnvironmentKind.DEVELOPMENT,
        workflow_key="commercial.requirements",
        workflow_version_id=UUID("60000000-0000-4000-8000-000000000010"),
        workflow_version="1.0.0",
        work_item_key="request:crm-932",
        client_request=ClientRequestV1(message="Prepare proposal requirements"),
        trace_id=UUID("60000000-0000-4000-8000-000000000005"),
        correlation_id=UUID("60000000-0000-4000-8000-000000000006"),
        causation_id=UUID("60000000-0000-4000-8000-000000000007"),
    )


@pytest.mark.asyncio
async def test_start_returns_port_receipt_and_preserves_trusted_command() -> None:
    receipt = WorkflowCommandReceipt(
        command_id=UUID("60000000-0000-4000-8000-000000000001"),
        work_item_id=UUID("60000000-0000-4000-8000-000000000008"),
        run_id=UUID("60000000-0000-4000-8000-000000000009"),
        workflow_version_id=UUID("60000000-0000-4000-8000-000000000010"),
        state=WorkflowRunState.ADMITTED,
        admission="accepted",
    )
    port = AdmissionPort(receipt)
    service = WorkflowRunAdmissionService(port)

    result = await service.start(_command())

    assert result == receipt
    assert port.commands == [_command()]


def test_start_command_rejects_blank_idempotency_and_work_item_keys() -> None:
    with pytest.raises(ValueError):
        StartWorkflowCommand.model_validate(
            _command().model_dump() | {"idempotency_key": " "}
        )


def test_requirements_review_commands_are_only_advertised_for_requirements_runs() -> None:
    command = _command()
    base = WorkflowRun(
        run_id=UUID(int=90),
        command_id=command.command_id,
        work_item_id=UUID(int=91),
        workspace_id=command.workspace_id,
        environment_id=command.environment_id,
        workflow_key="other.workflow",
        workflow_version_id=command.workflow_version_id,
        workflow_version=command.workflow_version,
        state=WorkflowRunState.WAITING,
        run_version=2,
        started_by=command.actor,
        client_request=command.client_request,
        trace_id=command.trace_id,
        correlation_id=command.correlation_id,
        causation_id=command.causation_id,
        created_at=datetime(2026, 10, 29, tzinfo=timezone.utc),
    )

    assert workflow_run_snapshot(base).allowed_commands == ("add_context",)
    requirements = base.model_copy(update={"workflow_key": "commercial.requirements"})
    assert workflow_run_snapshot(requirements).allowed_commands == (
        "add_context",
        "edit_requirement",
        "save_review",
        "request_rework",
    )


@pytest.mark.asyncio
async def test_configured_resolver_returns_published_immutable_version() -> None:
    version = PinnedWorkflowVersion(
        workflow_key="commercial.requirements",
        workflow_version_id=UUID("60000000-0000-4000-8000-000000000010"),
        version="1.0.0",
    )
    resolver = ConfiguredWorkflowVersionResolver((version,))

    selected = await resolver.resolve(
        workspace_id=UUID("60000000-0000-4000-8000-000000000002"),
        environment_id=UUID("60000000-0000-4000-8000-000000000003"),
        workflow_key="commercial.requirements",
    )

    assert selected == version


@pytest.mark.asyncio
async def test_configured_resolver_fails_closed_for_unpublished_workflow() -> None:
    resolver = ConfiguredWorkflowVersionResolver(())

    with pytest.raises(WorkflowVersionUnavailableError):
        await resolver.resolve(
            workspace_id=UUID("60000000-0000-4000-8000-000000000002"),
            environment_id=UUID("60000000-0000-4000-8000-000000000003"),
            workflow_key="commercial.requirements",
        )
