from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import httpx

from spine.api.workflow_runs import install_workflow_run_routes
from spine.application.workflow_runs import PinnedWorkflowVersion
from spine.domain.common import ActorKind, ActorRef
from spine.domain.workflows import (
    ClientRequestV1,
    WorkflowCommandReceipt,
    WorkflowRun,
    WorkflowRunState,
)
from tests.api.test_authorization import (
    ENVIRONMENT_ID,
    OTHER_WORKSPACE_ID,
    WORKSPACE_ID,
    authorized_app,
    authorization_entry,
)
from spine.auth import InMemoryAuthorizationDirectory


NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)
COMMAND_ID = UUID("63800000-0000-0000-0000-000000000001")
WORK_ITEM_ID = UUID("63800000-0000-0000-0000-000000000002")
RUN_ID = UUID("63800000-0000-0000-0000-000000000003")
VERSION_ID = UUID("63800000-0000-0000-0000-000000000004")
ACTOR_ID = UUID("20000000-0000-0000-0000-000000000071")


class FakeWorkflowRunService:
    def __init__(self) -> None:
        self.command = None
        self.run = WorkflowRun(
            command_id=COMMAND_ID,
            run_id=RUN_ID,
            work_item_id=WORK_ITEM_ID,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            started_by=ActorRef(kind=ActorKind.HUMAN, id=ACTOR_ID),
            workflow_key="commercial.requirements",
            workflow_version_id=VERSION_ID,
            workflow_version="1.0.0",
            run_version=1,
            state=WorkflowRunState.ADMITTED,
            client_request=ClientRequestV1(message="Prepare a proposal"),
            trace_id=COMMAND_ID,
            correlation_id=COMMAND_ID,
            causation_id=COMMAND_ID,
            created_at=NOW,
        )

    async def start(self, *, context, command):
        del context
        self.command = command
        return WorkflowCommandReceipt(
            command_id=command.command_id,
            work_item_id=WORK_ITEM_ID,
            run_id=RUN_ID,
            workflow_version_id=command.workflow_version_id,
            state=WorkflowRunState.ADMITTED,
            admission="accepted",
        )

    async def get_run(self, *, context, run_id):
        scope = context.scope
        if (
            run_id != RUN_ID
            or scope.workspace_id != self.run.workspace_id
            or scope.environment_id != self.run.environment_id
        ):
            return None
        return self.run


class FakeWorkflowVersionResolver:
    async def resolve(self, *, workspace_id, environment_id, workflow_key):
        assert workspace_id == WORKSPACE_ID
        assert environment_id == ENVIRONMENT_ID
        assert workflow_key == "commercial.requirements"
        return PinnedWorkflowVersion(
            workflow_key=workflow_key,
            workflow_version_id=VERSION_ID,
            version="1.0.0",
        )


def _app(service: FakeWorkflowRunService, *, authorize_other_workspace: bool = False):
    directory = None
    if authorize_other_workspace:
        other_entry = authorization_entry().model_copy(
            update={
                "workspace_id": OTHER_WORKSPACE_ID,
                "environment_id": UUID("30000000-0000-0000-0000-000000000074"),
                "environment_workspace_id": OTHER_WORKSPACE_ID,
            }
        )
        directory = InMemoryAuthorizationDirectory(
            entries=(authorization_entry(), other_entry)
        )
    application = authorized_app([], directory=directory)
    install_workflow_run_routes(
        application,
        service=service,  # type: ignore[arg-type]
        version_resolver=FakeWorkflowVersionResolver(),
        command_id_source=lambda: COMMAND_ID,
    )
    return application


def _headers(*, workspace_id: UUID = WORKSPACE_ID) -> dict[str, str]:
    return {
        "Authorization": "Bearer opaque-provider-token",
        "X-Workspace-ID": str(workspace_id),
        "X-Environment-ID": str(
            UUID("30000000-0000-0000-0000-000000000074")
            if workspace_id == OTHER_WORKSPACE_ID
            else ENVIRONMENT_ID
        ),
        "Idempotency-Key": "workflow-start-638",
    }


async def test_start_uses_server_scope_and_pins_selected_workflow_version() -> None:
    service = FakeWorkflowRunService()
    transport = httpx.ASGITransport(app=_app(service))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/runs",
            headers=_headers(),
            json={
                "workflow_key": "commercial.requirements",
                "work_item_key": "proposal-638",
                "message": "Prepare a proposal",
            },
        )

    assert response.status_code == 202
    assert response.json() == {
        "schema_version": 1,
        "command_id": str(COMMAND_ID),
        "work_item_id": str(WORK_ITEM_ID),
        "run_id": str(RUN_ID),
        "workflow_version_id": str(VERSION_ID),
        "state": "admitted",
        "admission": "accepted",
    }
    assert service.command.workspace_id == WORKSPACE_ID
    assert service.command.environment_id == ENVIRONMENT_ID
    assert service.command.workflow_version_id == VERSION_ID


async def test_run_read_hides_runs_outside_authorized_workspace() -> None:
    service = FakeWorkflowRunService()
    transport = httpx.ASGITransport(app=_app(service, authorize_other_workspace=True))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/runs/{RUN_ID}",
            headers=_headers(workspace_id=OTHER_WORKSPACE_ID),
        )

    assert response.status_code == 404
    assert response.json() == {"code": "workflow.run_not_found"}
