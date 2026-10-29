from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import httpx

from spine.api.workflow_runs import install_workflow_run_routes
from spine.application.persistence.context import EnvironmentScope
from spine.application.persistence.errors import ConstraintConflictError, OptimisticConflictError
from spine.application.requirements_review import (
    RequirementsArtifactStaleError,
    RequirementsReviewReceipt,
)
from spine.application.workflow_runs import PinnedWorkflowVersion, WorkflowArtifactRef
from spine.domain.common import ActorKind, ActorRef
from spine.domain.workflows import (
    ClientRequestV1,
    WorkflowCommandReceipt,
    WorkflowEvent,
    WorkflowEventPage,
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
        self.continuation = None
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
        self.events = [
            WorkflowEvent(
                event_id=UUID("63900000-0000-0000-0000-000000000010"),
                run_id=RUN_ID,
                sequence=1,
                event_type="workflow.run-started",
                event_schema_version=1,
                run_version=1,
                summary="Workflow run started.",
                correlation_id=COMMAND_ID,
                causation_id=COMMAND_ID,
                occurred_at=NOW,
            )
        ]

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

    async def dispatch(self, *, context, command, clarification_successor=None):
        scope = context.scope
        if (
            command.run_id != RUN_ID
            or scope.workspace_id != self.run.workspace_id
            or scope.environment_id != self.run.environment_id
        ):
            return None
        self.continuation = command
        next_state = (
            WorkflowRunState.RUNNING
            if command.command_type == "answer_clarification"
            else WorkflowRunState.CANCELLED
            if command.command_type == "cancel"
            else self.run.state
        )
        self.run = self.run.model_copy(update={
            "run_version": self.run.run_version + 1,
            "state": next_state,
        })
        self.clarification_successor = clarification_successor
        self.events.append(
            WorkflowEvent(
                event_id=UUID("63900000-0000-0000-0000-000000000011"),
                run_id=RUN_ID,
                sequence=self.run.run_version,
                event_type=(
                    "workflow.clarification-answered"
                    if command.command_type == "answer_clarification"
                    else "workflow.run-cancelled"
                    if command.command_type == "cancel"
                    else "workflow.context-added"
                ),
                event_schema_version=1,
                run_version=self.run.run_version,
                summary=(
                    "Clarification answered."
                    if command.command_type == "answer_clarification"
                    else "Workflow run cancelled."
                    if command.command_type == "cancel"
                    else "Additional context received."
                ),
                correlation_id=command.correlation_id,
                causation_id=command.command_id,
                occurred_at=NOW,
            )
        )
        return WorkflowCommandReceipt(
            command_id=command.command_id,
            work_item_id=WORK_ITEM_ID,
            run_id=RUN_ID,
            workflow_version_id=VERSION_ID,
            state=next_state,
            run_version=self.run.run_version,
            admission="accepted",
        )

    async def list_events(self, *, context, run_id, after_sequence, limit):
        run = await self.get_run(context=context, run_id=run_id)
        if run is None:
            return None
        items = [item for item in self.events if after_sequence is None or item.sequence > after_sequence]
        page = items[:limit]
        return WorkflowEventPage(
            items=tuple(page),
            next_sequence=page[-1].sequence if len(items) > limit else None,
        )


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


def _app(service: FakeWorkflowRunService, *, authorize_other_workspace: bool = False,
         requirements_artifact_service=None, requirements_review_service=None):
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
        cursor_secret=b"workflow-run-api-cursor-secret-639",
        requirements_artifact_service=requirements_artifact_service,
        requirements_review_service=requirements_review_service,
    )
    return application


class FakeRequirementsReviewService:
    def __init__(self, *, stale: bool = False, terminal: bool = False) -> None:
        self.command = None
        self.stale = stale
        self.terminal = terminal

    async def dispatch(self, *, context, command):
        if context.scope.workspace_id != WORKSPACE_ID:
            return None
        self.command = command
        if self.terminal:
            raise ConstraintConflictError("Workflow transition is invalid.")
        base = command.artifact_refs[0]
        current = base.model_copy(update={"artifact_version": 2})
        if self.stale:
            raise RequirementsArtifactStaleError(current)
        return RequirementsReviewReceipt(
            command_receipt=WorkflowCommandReceipt(
                command_id=command.command_id,
                work_item_id=WORK_ITEM_ID,
                run_id=RUN_ID,
                workflow_version_id=VERSION_ID,
                state=WorkflowRunState.WAITING,
                run_version=2,
                admission="accepted",
            ),
            action=command.command_type,
            actor=command.actor,
            reason=command.payload.reason,
            changed_requirement_id=getattr(command.payload, "requirement_id", None),
            base_artifact_ref=base,
            resulting_artifact_ref=current,
        )


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


async def test_add_context_uses_trusted_scope_and_restores_safe_event_feed() -> None:
    service = FakeWorkflowRunService()
    transport = httpx.ASGITransport(app=_app(service))
    continuation_id = UUID("63900000-0000-0000-0000-000000000001")

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        command_response = await client.post(
            f"/api/v1/runs/{RUN_ID}/commands",
            headers={**_headers(), "Idempotency-Key": "workflow-context-639"},
            json={
                "schema_version": 1,
                "command_id": str(continuation_id),
                "command_type": "add_context",
                "command_schema_version": 1,
                "expected_run_version": 1,
                "payload": {"schema_version": 1, "message": "The deadline is December 20."},
                "artifact_refs": [],
                "correlation_id": str(COMMAND_ID),
                "causation_id": str(COMMAND_ID),
            },
        )
        run_response = await client.get(f"/api/v1/runs/{RUN_ID}", headers=_headers())
        events_response = await client.get(
            f"/api/v1/runs/{RUN_ID}/events?limit=20", headers=_headers()
        )

    assert command_response.status_code == 202
    assert command_response.json()["run_version"] == 2
    assert service.continuation.workspace_id == WORKSPACE_ID
    assert service.continuation.environment_id == ENVIRONMENT_ID
    assert service.continuation.actor.id == authorization_entry().canonical_human_identity_id
    assert "message" not in run_response.json()
    assert "accepted_sources" not in run_response.json()
    assert events_response.status_code == 200
    assert [item["event_type"] for item in events_response.json()["items"]] == [
        "workflow.run-started",
        "workflow.context-added",
    ]
    assert "deadline" not in events_response.text.lower()


async def test_answer_clarification_uses_typed_command_and_resumes_same_run() -> None:
    service = FakeWorkflowRunService()
    service.run = service.run.model_copy(
        update={"state": WorkflowRunState.WAITING, "run_version": 2}
    )

    class CandidateService:
        def __init__(self) -> None:
            self.command = None
            self.successor = object()

        async def prepare_clarification_answer(self, *, context, command, requester):
            assert context.scope == EnvironmentScope(WORKSPACE_ID, ENVIRONMENT_ID)
            assert requester.subject_id == authorization_entry().canonical_human_identity_id
            self.command = command
            return self.successor

    candidates = CandidateService()
    transport = httpx.ASGITransport(
        app=_app(service, requirements_artifact_service=candidates)
    )
    clarification_id = UUID("64300000-0000-4000-8000-000000000001")
    requirement_id = UUID("64300000-0000-4000-8000-000000000002")
    artifact_id = UUID("64300000-0000-4000-8000-000000000003")

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/runs/{RUN_ID}/commands",
            headers={**_headers(), "Idempotency-Key": "clarification-answer-643"},
            json={
                "schema_version": 1,
                "command_id": "64300000-0000-4000-8000-000000000004",
                "command_type": "answer_clarification",
                "command_schema_version": 1,
                "expected_run_version": 2,
                "payload": {
                    "schema_version": 1,
                    "clarification_id": str(clarification_id),
                    "requirement_id": str(requirement_id),
                    "answer": "The delivery deadline is December 20.",
                },
                "artifact_refs": [{
                    "artifact_id": str(artifact_id),
                    "schema_name": "requirements",
                    "schema_version": "requirements:v1",
                    "artifact_version": 1,
                }],
                "correlation_id": str(RUN_ID),
                "causation_id": str(clarification_id),
            },
        )

    assert response.status_code == 202, response.text
    assert response.json()["run_id"] == str(RUN_ID)
    assert response.json()["state"] == "running"
    assert response.json()["run_version"] == 3
    assert candidates.command.payload.answer.endswith("December 20.")
    assert service.clarification_successor is candidates.successor


async def test_stale_clarification_ref_returns_safe_conflict() -> None:
    class StaleService(FakeWorkflowRunService):
        async def dispatch(self, *, context, command, clarification_successor=None):
            if clarification_successor is None:
                raise ConstraintConflictError("not an exact replay")
            return await super().dispatch(
                context=context,
                command=command,
                clarification_successor=clarification_successor,
            )

    service = StaleService()
    service.run = service.run.model_copy(
        update={"state": WorkflowRunState.WAITING, "run_version": 2}
    )

    class CandidateService:
        async def prepare_clarification_answer(self, **kwargs):
            del kwargs
            raise OptimisticConflictError("protected stale artifact")

    transport = httpx.ASGITransport(
        app=_app(service, requirements_artifact_service=CandidateService())
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/runs/{RUN_ID}/commands",
            headers={**_headers(), "Idempotency-Key": "stale-clarification-643"},
            json={
                "command_id": "64300000-0000-4000-8000-000000000014",
                "command_type": "answer_clarification",
                "command_schema_version": 1,
                "expected_run_version": 2,
                "payload": {
                    "clarification_id": "64300000-0000-4000-8000-000000000011",
                    "requirement_id": "64300000-0000-4000-8000-000000000012",
                    "answer": "Protected answer text",
                },
                "artifact_refs": [{
                    "artifact_id": "64300000-0000-4000-8000-000000000013",
                    "schema_name": "requirements",
                    "schema_version": "requirements:v1",
                    "artifact_version": 1,
                }],
                "correlation_id": str(RUN_ID),
                "causation_id": str(COMMAND_ID),
            },
        )

    assert response.status_code == 409
    assert response.json()["code"] == "workflow.artifact_stale"
    assert "protected answer" not in response.text.lower()


async def test_exact_clarification_replay_returns_original_receipt() -> None:
    class ReplayService(FakeWorkflowRunService):
        async def dispatch(self, *, context, command, clarification_successor=None):
            del context, clarification_successor
            return WorkflowCommandReceipt(
                command_id=command.command_id,
                work_item_id=WORK_ITEM_ID,
                run_id=RUN_ID,
                workflow_version_id=VERSION_ID,
                state=WorkflowRunState.RUNNING,
                run_version=3,
                admission="replayed",
            )

    class CandidateService:
        async def prepare_clarification_answer(self, **kwargs):
            del kwargs
            raise OptimisticConflictError("the original artifact is no longer latest")

    service = ReplayService()
    service.run = service.run.model_copy(
        update={"state": WorkflowRunState.RUNNING, "run_version": 3}
    )
    transport = httpx.ASGITransport(
        app=_app(service, requirements_artifact_service=CandidateService())
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/runs/{RUN_ID}/commands",
            headers={**_headers(), "Idempotency-Key": "replay-clarification-643"},
            json={
                "command_id": "64300000-0000-4000-8000-000000000044",
                "command_type": "answer_clarification",
                "command_schema_version": 1,
                "expected_run_version": 2,
                "payload": {
                    "clarification_id": "64300000-0000-4000-8000-000000000041",
                    "requirement_id": "64300000-0000-4000-8000-000000000042",
                    "answer": "The original exact answer.",
                },
                "artifact_refs": [{
                    "artifact_id": "64300000-0000-4000-8000-000000000043",
                    "schema_name": "requirements",
                    "schema_version": "requirements:v1",
                    "artifact_version": 1,
                }],
                "correlation_id": str(RUN_ID),
                "causation_id": str(COMMAND_ID),
            },
        )

    assert response.status_code == 202
    assert response.json()["admission"] == "replayed"
    assert response.json()["run_version"] == 3


async def test_clarification_conflict_is_not_exposed_as_server_error() -> None:
    service = FakeWorkflowRunService()

    class CandidateService:
        async def prepare_clarification_answer(self, **kwargs):
            del kwargs
            raise ConstraintConflictError("protected conflicting payload")

    transport = httpx.ASGITransport(
        app=_app(service, requirements_artifact_service=CandidateService())
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/runs/{RUN_ID}/commands",
            headers={**_headers(), "Idempotency-Key": "conflicting-clarification-643"},
            json={
                "command_id": "64300000-0000-4000-8000-000000000024",
                "command_type": "answer_clarification",
                "command_schema_version": 1,
                "expected_run_version": 1,
                "payload": {
                    "clarification_id": "64300000-0000-4000-8000-000000000021",
                    "requirement_id": "64300000-0000-4000-8000-000000000022",
                    "answer": "Protected conflicting answer",
                },
                "artifact_refs": [{
                    "artifact_id": "64300000-0000-4000-8000-000000000023",
                    "schema_name": "requirements",
                    "schema_version": "requirements:v1",
                    "artifact_version": 1,
                }],
                "correlation_id": str(RUN_ID),
                "causation_id": str(COMMAND_ID),
            },
        )

    assert response.status_code == 409
    assert response.json()["code"] == "workflow.clarification_conflict"
    assert "protected conflicting" not in response.text.lower()


async def test_cancel_command_returns_terminal_receipt() -> None:
    service = FakeWorkflowRunService()
    service.run = service.run.model_copy(
        update={"state": WorkflowRunState.WAITING, "run_version": 2}
    )
    transport = httpx.ASGITransport(app=_app(service))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/runs/{RUN_ID}/commands",
            headers={**_headers(), "Idempotency-Key": "cancel-643"},
            json={
                "command_id": "64300000-0000-4000-8000-000000000031",
                "command_type": "cancel",
                "command_schema_version": 1,
                "expected_run_version": 2,
                "payload": {"schema_version": 1},
                "artifact_refs": [],
                "correlation_id": str(RUN_ID),
                "causation_id": str(COMMAND_ID),
            },
        )

    assert response.status_code == 202
    assert response.json()["state"] == "cancelled"
    assert response.json()["run_id"] == str(RUN_ID)


async def test_event_cursor_is_stable_and_bound_to_run_scope() -> None:
    service = FakeWorkflowRunService()
    service.events.append(
        WorkflowEvent(
            event_id=UUID("63900000-0000-0000-0000-000000000011"),
            run_id=RUN_ID,
            sequence=2,
            event_type="workflow.context-added",
            event_schema_version=1,
            run_version=2,
            summary="Additional context received.",
            correlation_id=COMMAND_ID,
            causation_id=COMMAND_ID,
            occurred_at=NOW,
        )
    )
    transport = httpx.ASGITransport(app=_app(service))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get(f"/api/v1/runs/{RUN_ID}/events?limit=1", headers=_headers())
        cursor = first.json()["next_cursor"]
        second = await client.get(
            f"/api/v1/runs/{RUN_ID}/events?limit=1&cursor={cursor}", headers=_headers()
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["items"][0]["sequence"] == 1
    assert second.json()["items"][0]["sequence"] == 2


async def test_stale_command_returns_current_safe_snapshot() -> None:
    class StaleService(FakeWorkflowRunService):
        async def dispatch(self, *, context, command):
            del context, command
            self.run = self.run.model_copy(update={"run_version": 3})
            raise OptimisticConflictError("protected concurrent command")

    service = StaleService()
    transport = httpx.ASGITransport(app=_app(service))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/runs/{RUN_ID}/commands",
            headers={**_headers(), "Idempotency-Key": "stale-context-639"},
            json={
                "command_id": "63900000-0000-0000-0000-000000000001",
                "command_type": "add_context",
                "command_schema_version": 1,
                "expected_run_version": 1,
                "payload": {"message": "Protected stale content"},
                "correlation_id": str(COMMAND_ID),
                "causation_id": str(COMMAND_ID),
            },
        )

    assert response.status_code == 409
    assert response.json()["code"] == "workflow.run_stale"
    assert response.json()["current"]["run_version"] == 3
    assert "protected stale content" not in response.text.lower()


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

    assert response.status_code == 202, response.text
    assert response.json() == {
        "schema_version": 1,
        "command_id": str(COMMAND_ID),
        "work_item_id": str(WORK_ITEM_ID),
        "run_id": str(RUN_ID),
        "workflow_version_id": str(VERSION_ID),
        "state": "admitted",
        "run_version": 1,
        "admission": "accepted",
    }
    assert service.command.workspace_id == WORKSPACE_ID
    assert service.command.environment_id == ENVIRONMENT_ID
    assert service.command.workflow_version_id == VERSION_ID


async def test_run_start_persists_one_deterministic_requirements_candidate() -> None:
    service = FakeWorkflowRunService()

    class CandidateService:
        def __init__(self) -> None:
            self.calls = []

        async def produce_first_candidate(self, *, context, run_id):
            self.calls.append((context.scope, run_id))

    candidate_service = CandidateService()
    transport = httpx.ASGITransport(app=_app(service, requirements_artifact_service=candidate_service))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/runs", headers=_headers(),
            json={"workflow_key": "commercial.requirements", "work_item_key": "proposal-640",
                  "message": "Prepare a proposal"},
        )

    assert response.status_code == 202
    assert candidate_service.calls == [(
        EnvironmentScope(workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID), RUN_ID
    )]


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


async def test_run_command_hides_runs_outside_authorized_workspace() -> None:
    service = FakeWorkflowRunService()
    transport = httpx.ASGITransport(app=_app(service, authorize_other_workspace=True))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/runs/{RUN_ID}/commands",
            headers={**_headers(workspace_id=OTHER_WORKSPACE_ID), "Idempotency-Key": "foreign-639"},
            json={
                "command_id": "63900000-0000-0000-0000-000000000001",
                "command_type": "add_context",
                "command_schema_version": 1,
                "expected_run_version": 1,
                "payload": {"message": "Do not disclose this"},
                "correlation_id": str(COMMAND_ID),
                "causation_id": str(COMMAND_ID),
            },
        )

    assert response.status_code == 404
    assert response.json() == {"code": "workflow.run_not_found"}
    assert "do not disclose" not in response.text.lower()


async def test_exact_requirement_edit_returns_human_review_receipt() -> None:
    service = FakeWorkflowRunService()
    review = FakeRequirementsReviewService()
    transport = httpx.ASGITransport(
        app=_app(service, requirements_review_service=review)
    )
    artifact_id = UUID("64400000-0000-4000-8000-000000000001")
    requirement_id = UUID("64400000-0000-4000-8000-000000000002")

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/runs/{RUN_ID}/commands",
            headers={**_headers(), "Idempotency-Key": "edit-644"},
            json={
                "schema_version": 1,
                "command_id": "64400000-0000-4000-8000-000000000003",
                "command_type": "edit_requirement",
                "command_schema_version": 1,
                "expected_run_version": 1,
                "payload": {
                    "schema_version": 1,
                    "requirement_id": str(requirement_id),
                    "statement": "Deliver on Friday by 12:00",
                    "reason": "Manager clarified the deadline.",
                },
                "artifact_refs": [{
                    "artifact_id": str(artifact_id),
                    "schema_name": "requirements",
                    "schema_version": "requirements:v1",
                    "artifact_version": 1,
                }],
                "correlation_id": str(COMMAND_ID),
                "causation_id": str(COMMAND_ID),
            },
        )

    assert response.status_code == 202
    assert response.json()["action"] == "edit_requirement"
    assert response.json()["reason"] == "Manager clarified the deadline."
    assert response.json()["base_artifact_ref"]["artifact_version"] == 1
    assert response.json()["resulting_artifact_ref"]["artifact_version"] == 2
    assert review.command.expected_run_version == 1
    assert review.command.artifact_refs[0].artifact_id == artifact_id


async def test_stale_requirement_edit_returns_current_ref_without_protected_content() -> None:
    service = FakeWorkflowRunService()
    review = FakeRequirementsReviewService(stale=True)
    transport = httpx.ASGITransport(
        app=_app(service, requirements_review_service=review)
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/runs/{RUN_ID}/commands",
            headers={**_headers(), "Idempotency-Key": "stale-644"},
            json={
                "schema_version": 1,
                "command_id": "64400000-0000-4000-8000-000000000004",
                "command_type": "request_rework",
                "command_schema_version": 1,
                "expected_run_version": 1,
                "payload": {
                    "schema_version": 1,
                    "reason": "Protected customer correction",
                },
                "artifact_refs": [{
                    "artifact_id": "64400000-0000-4000-8000-000000000001",
                    "schema_name": "requirements",
                    "schema_version": "requirements:v1",
                    "artifact_version": 1,
                }],
                "correlation_id": str(COMMAND_ID),
                "causation_id": str(COMMAND_ID),
            },
        )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "requirements.artifact_stale"
    assert response.json()["current_artifact_ref"]["artifact_version"] == 2
    assert "protected customer correction" not in response.text.lower()


async def test_cancelled_run_rejects_requirements_review_without_creating_a_result() -> None:
    service = FakeWorkflowRunService()
    review = FakeRequirementsReviewService(terminal=True)
    transport = httpx.ASGITransport(
        app=_app(service, requirements_review_service=review)
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/runs/{RUN_ID}/commands",
            headers={**_headers(), "Idempotency-Key": "cancelled-644"},
            json={
                "schema_version": 1,
                "command_id": "64400000-0000-4000-8000-000000000005",
                "command_type": "save_review",
                "command_schema_version": 1,
                "expected_run_version": 1,
                "payload": {"schema_version": 1, "reason": "Review after cancellation"},
                "artifact_refs": [{
                    "artifact_id": "64400000-0000-4000-8000-000000000001",
                    "schema_name": "requirements",
                    "schema_version": "requirements:v1",
                    "artifact_version": 1,
                }],
                "correlation_id": str(COMMAND_ID),
                "causation_id": str(COMMAND_ID),
            },
        )

    assert response.status_code == 409
    assert response.json() == {"code": "workflow.run_terminal"}
    assert "review after cancellation" not in response.text.lower()
