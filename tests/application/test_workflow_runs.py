from __future__ import annotations

from uuid import UUID

import pytest

from spine.application.workflow_runs import (
    ConfiguredWorkflowVersionResolver,
    PinnedWorkflowVersion,
    StartWorkflowCommand,
    WorkflowVersionUnavailableError,
    WorkflowRunAdmissionService,
)
from spine.domain.common import ActorKind, ActorRef, EnvironmentKind
from spine.domain.workflows import ClientRequestV1, WorkflowCommandReceipt, WorkflowRunState


class AdmissionPort:
    def __init__(self, receipt: WorkflowCommandReceipt) -> None:
        self.receipt = receipt
        self.commands: list[StartWorkflowCommand] = []

    async def admit(self, command: StartWorkflowCommand) -> WorkflowCommandReceipt:
        self.commands.append(command)
        return self.receipt

    async def get_run(self, *, workspace_id: UUID, environment_id: UUID, run_id: UUID):
        return None


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
