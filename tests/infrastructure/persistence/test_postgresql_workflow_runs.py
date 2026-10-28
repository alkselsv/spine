from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.application.persistence.outbox import OutboxEventRegistry
from spine.application.persistence.errors import IdempotencyConflictError, OptimisticConflictError
from spine.application.persistence.idempotency import (
    IdempotencyKey,
    IdempotencyReplay,
    OpaqueResultReference,
)
from spine.application.persistence.workflow_runs import register_workflow_run_outbox_event
from spine.application.workflow_runs import AddContextCommandV1, StartWorkflowCommand, WorkflowCommand
from spine.domain.common import ActorKind, ActorRef, EnvironmentKind
from spine.domain.workspaces import Environment
from spine.domain.workflows import ClientRequestV1
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.persistence.postgresql_workflow_runs import (
    PostgreSQLWorkflowRunAdmissionRepository,
)


WORKSPACE_ID = UUID("63000000-0000-4000-8000-000000000001")
ENVIRONMENT_ID = UUID("63000000-0000-4000-8000-000000000002")
ACTOR_ID = UUID("63000000-0000-4000-8000-000000000003")
TRACE_ID = UUID("63000000-0000-4000-8000-000000000004")
COMMAND_ID = UUID("63000000-0000-4000-8000-000000000005")
WORK_ITEM_ID = UUID("63000000-0000-4000-8000-000000000006")
RUN_ID = UUID("63000000-0000-4000-8000-000000000007")
VERSION_ID = UUID("63000000-0000-4000-8000-000000000008")
NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)


class FakeResult:
    def __init__(self, row=None, *, rowcount: int = 1) -> None:
        self.row = row
        self.rowcount = rowcount

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row


class FakeUow:
    def __init__(self, *, operation: str = "workflow.run.start") -> None:
        self.context = TrustedContextBoundary.for_testing(
            issuer_id=UUID("63000000-0000-4000-8000-000000000009"),
            secret=b"workflow-run-test-secret-630012345678901234567890",
        ).interactive(
            scope=EnvironmentScope(WORKSPACE_ID, ENVIRONMENT_ID),
            acting_subject_id=ACTOR_ID,
            purpose=PersistencePurpose("workflow_execution"),
            operation=PersistenceOperation(operation),
            trace_id=TRACE_ID,
        )
        self._outbox_events = OutboxEventRegistry()
        register_workflow_run_outbox_event(self._outbox_events)
        self.statements = []
        self.replay_row = None
        self.claim_result = None
        self.claim_error = None
        self.intents = []
        self.completed = []
        self.idempotency = SimpleNamespace(
            claim=self._claim,
            complete=self._complete,
        )
        self.outbox = SimpleNamespace(append=self._append)
        self.environments = SimpleNamespace(
            resolve=self._resolve_environment,
        )

    def _guard_active(self):
        return None

    def _context_snapshot_active(self):
        return self.context

    async def _claim(self, **kwargs):
        self.digest = kwargs["digest"]
        if self.claim_error is not None:
            raise self.claim_error
        if self.claim_result is not None:
            return self.claim_result
        return SimpleNamespace(receipt_id=COMMAND_ID)

    async def _complete(self, claim, result):
        self.completed.append((claim, result))

    async def _resolve_environment(self, environment_id):
        assert environment_id == ENVIRONMENT_ID
        return Environment(
            id=ENVIRONMENT_ID,
            workspace_id=WORKSPACE_ID,
            kind=EnvironmentKind.PRODUCTION,
            display_name="Production",
        )

    async def _append(self, intent):
        self.intents.append(intent)

    async def _execute(self, statement):
        self.statements.append(statement)
        return FakeResult(self.replay_row)

    async def _fail(self, error):
        raise error


def _command(message: str = "Prepare a proposal") -> StartWorkflowCommand:
    return StartWorkflowCommand(
        command_id=COMMAND_ID,
        idempotency_key="start-630",
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        actor=ActorRef(kind=ActorKind.HUMAN, id=ACTOR_ID),
        environment=EnvironmentKind.PRODUCTION,
        workflow_key="commercial.requirements",
        workflow_version_id=VERSION_ID,
        workflow_version="1.0.0",
        work_item_key="proposal:crm-630",
        client_request=ClientRequestV1(message=message),
        trace_id=TRACE_ID,
        correlation_id=UUID("63000000-0000-4000-8000-000000000010"),
        causation_id=UUID("63000000-0000-4000-8000-000000000011"),
    )


def _continuation(*, expected_run_version: int = 1) -> WorkflowCommand:
    return WorkflowCommand(
        command_id=UUID("63900000-0000-4000-8000-000000000001"),
        idempotency_key="context-639",
        run_id=RUN_ID,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        actor=ActorRef(kind=ActorKind.HUMAN, id=ACTOR_ID),
        command_type="add_context",
        command_schema_version=1,
        expected_run_version=expected_run_version,
        payload=AddContextCommandV1(message="The delivery deadline is December 20."),
        trace_id=TRACE_ID,
        correlation_id=UUID("63900000-0000-4000-8000-000000000002"),
        causation_id=UUID("63900000-0000-4000-8000-000000000003"),
    )


def _run_row(*, state: str = "running", run_version: int = 1) -> dict[str, object]:
    return {
        "run_id": RUN_ID,
        "command_id": COMMAND_ID,
        "work_item_id": WORK_ITEM_ID,
        "workspace_id": WORKSPACE_ID,
        "environment_id": ENVIRONMENT_ID,
        "workflow_key": "commercial.requirements",
        "workflow_version_id": VERSION_ID,
        "workflow_version": "1.0.0",
        "state": state,
        "run_version": run_version,
        "triggered_by": {"kind": "human", "id": str(ACTOR_ID)},
        "input": {"schema_version": 1, "message": "Prepare a proposal", "accepted_sources": []},
        "trace_id": TRACE_ID,
        "correlation_id": UUID("63000000-0000-4000-8000-000000000010"),
        "causation_id": UUID("63000000-0000-4000-8000-000000000011"),
        "created_at": NOW,
    }


@pytest.mark.asyncio
async def test_add_context_updates_run_and_persists_safe_event_and_receipt() -> None:
    uow = FakeUow(operation="workflow.run.command")
    uow.replay_row = _run_row()
    repository = PostgreSQLWorkflowRunAdmissionRepository(uow, clock=lambda: NOW)

    receipt = await repository.dispatch(_continuation())

    writes = [statement.table.name for statement in uow.statements if hasattr(statement, "table")]
    assert writes == ["workflow_runs", "workflow_command_receipts", "workflow_run_events"]
    assert receipt.run_id == RUN_ID
    assert receipt.run_version == 2
    assert receipt.admission == "accepted"
    receipt_insert = next(
        statement for statement in uow.statements
        if getattr(getattr(statement, "table", None), "name", None) == "workflow_command_receipts"
    )
    event_insert = next(
        statement for statement in uow.statements
        if getattr(getattr(statement, "table", None), "name", None) == "workflow_run_events"
    )
    assert receipt_insert.compile().params["command_payload"]["message"] == "The delivery deadline is December 20."
    assert "delivery deadline" not in str(event_insert.compile().params).lower()


@pytest.mark.asyncio
async def test_add_context_rejects_stale_run_without_writes() -> None:
    uow = FakeUow(operation="workflow.run.command")
    uow.replay_row = _run_row(run_version=2)
    repository = PostgreSQLWorkflowRunAdmissionRepository(uow, clock=lambda: NOW)

    with pytest.raises(OptimisticConflictError):
        await repository.dispatch(_continuation(expected_run_version=1))

    assert not [statement for statement in uow.statements if hasattr(statement, "table")]


@pytest.mark.asyncio
async def test_add_context_replay_returns_original_receipt_without_second_effect() -> None:
    command = _continuation()
    uow = FakeUow(operation="workflow.run.command")
    uow.claim_result = IdempotencyReplay(
        kind="replay",
        receipt_id=command.command_id,
        operation=PersistenceOperation("workflow.run.command"),
        operation_schema_version=1,
        key=IdempotencyKey(command.idempotency_key),
        result=OpaqueResultReference("workflow_command_receipt", command.command_id, 1),
    )
    uow.replay_row = {
        "command_id": command.command_id,
        "work_item_id": WORK_ITEM_ID,
        "run_id": RUN_ID,
        "workflow_version_id": VERSION_ID,
        "run_state": "running",
        "resulting_run_version": 2,
    }
    repository = PostgreSQLWorkflowRunAdmissionRepository(uow, clock=lambda: NOW)

    receipt = await repository.dispatch(command)

    assert receipt.admission == "replayed"
    assert receipt.run_version == 2
    assert not [statement for statement in uow.statements if hasattr(statement, "table")]
    assert uow.completed == []


@pytest.mark.asyncio
async def test_add_context_payload_conflict_has_no_second_effect() -> None:
    uow = FakeUow(operation="workflow.run.command")
    uow.claim_error = IdempotencyConflictError("protected conflict")
    repository = PostgreSQLWorkflowRunAdmissionRepository(uow, clock=lambda: NOW)

    with pytest.raises(IdempotencyConflictError):
        await repository.dispatch(_continuation())

    assert uow.statements == []
    assert uow.completed == []


@pytest.mark.asyncio
async def test_admission_writes_pinned_run_and_content_safe_dispatch_atomically() -> None:
    uow = FakeUow()
    repository = PostgreSQLWorkflowRunAdmissionRepository(
        uow,
        work_item_id_factory=lambda: WORK_ITEM_ID,
        run_id_factory=lambda: RUN_ID,
        clock=lambda: NOW,
    )

    receipt = await repository.admit(_command())

    assert [statement.table.name for statement in uow.statements if hasattr(statement, "table")] == [
        "work_items",
        "workflow_runs",
        "workflow_run_events",
    ]
    assert receipt.run_id == RUN_ID
    assert receipt.workflow_version_id == VERSION_ID
    assert len(uow.intents) == 1
    assert "Prepare a proposal" not in uow.intents[0].payload_json()
    event_insert = next(
        statement for statement in uow.statements
        if getattr(getattr(statement, "table", None), "name", None) == "workflow_run_events"
    )
    assert "Prepare a proposal" not in str(event_insert.compile().params)
    assert len(uow.completed) == 1


@pytest.mark.asyncio
async def test_changed_message_changes_idempotency_digest() -> None:
    digests = []
    for message in ("Prepare a proposal", "Prepare a revised proposal"):
        uow = FakeUow()
        repository = PostgreSQLWorkflowRunAdmissionRepository(
            uow,
            work_item_id_factory=lambda: WORK_ITEM_ID,
            run_id_factory=lambda: RUN_ID,
            clock=lambda: NOW,
        )
        await repository.admit(_command(message))
        digests.append(uow.digest.value)

    assert digests[0] != digests[1]


@pytest.mark.asyncio
async def test_same_idempotency_key_replays_original_run_without_new_effects() -> None:
    uow = FakeUow()
    uow.claim_result = IdempotencyReplay(
        kind="replay",
        receipt_id=COMMAND_ID,
        operation=PersistenceOperation("workflow.run.start"),
        operation_schema_version=1,
        key=IdempotencyKey("start-630"),
        result=OpaqueResultReference("workflow_run_admission", RUN_ID, 1),
    )
    uow.replay_row = {
        "run_id": RUN_ID,
        "command_id": COMMAND_ID,
        "work_item_id": WORK_ITEM_ID,
        "workspace_id": WORKSPACE_ID,
        "environment_id": ENVIRONMENT_ID,
        "workflow_key": "commercial.requirements",
        "workflow_version_id": VERSION_ID,
        "workflow_version": "1.0.0",
        "state": "admitted",
        "run_version": 1,
        "triggered_by": {"kind": "human", "id": str(ACTOR_ID)},
        "input": {"schema_version": 1, "message": "Prepare a proposal", "accepted_sources": []},
        "trace_id": TRACE_ID,
        "correlation_id": UUID("63000000-0000-4000-8000-000000000010"),
        "causation_id": UUID("63000000-0000-4000-8000-000000000011"),
        "created_at": NOW,
    }
    repository = PostgreSQLWorkflowRunAdmissionRepository(uow)

    receipt = await repository.admit(_command())

    assert receipt.admission == "replayed"
    assert receipt.command_id == COMMAND_ID
    assert receipt.run_id == RUN_ID
    assert uow.intents == []
    assert uow.completed == []
    assert len(uow.statements) == 1


@pytest.mark.asyncio
async def test_changed_idempotency_payload_conflicts_before_creating_run() -> None:
    uow = FakeUow()
    uow.claim_error = IdempotencyConflictError("conflict")
    repository = PostgreSQLWorkflowRunAdmissionRepository(uow)

    with pytest.raises(IdempotencyConflictError):
        await repository.admit(_command("Changed proposal"))

    assert uow.statements == []
    assert uow.intents == []
