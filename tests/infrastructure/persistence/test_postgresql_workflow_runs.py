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
from spine.application.workflow_runs import (
    AddContextCommandV1,
    AnswerClarificationCommandV1,
    CancelWorkflowCommandV1,
    StartWorkflowCommand,
    WorkflowArtifactRef,
    WorkflowCommand,
)
from spine.domain.common import ActorKind, ActorRef, EnvironmentKind
from spine.domain.workspaces import Environment
from spine.domain.workflows import (
    Clarification,
    ClarificationAnswer,
    ClientRequestV1,
    Requirement,
    RequirementState,
    RequirementsArtifact,
)
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
        self.persisted_artifacts = []
        self.idempotency = SimpleNamespace(
            claim=self._claim,
            complete=self._complete,
        )
        self.outbox = SimpleNamespace(append=self._append)
        self.environments = SimpleNamespace(
            resolve=self._resolve_environment,
        )
        self.requirements_artifacts = SimpleNamespace(
            persist_candidate=self._persist_artifact,
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

    async def _persist_artifact(self, artifact):
        self.persisted_artifacts.append(artifact)
        return artifact

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


def _clarification_artifacts() -> tuple[RequirementsArtifact, RequirementsArtifact]:
    requirement = Requirement(
        requirement_id=UUID("64300000-0000-4000-8000-000000000010"),
        category="unresolved_information",
        statement="Delivery date is not confirmed.",
        missing_markers=("delivery_date",),
        state=RequirementState.PARTIAL,
    )
    clarification = Clarification(
        clarification_id=UUID("64300000-0000-4000-8000-000000000011"),
        run_id=RUN_ID,
        requirement_id=requirement.requirement_id,
        originating_artifact_id=UUID("64300000-0000-4000-8000-000000000012"),
        originating_artifact_version=1,
        question="Please provide the missing information required to continue.",
        reason="missing_information",
        asked_by=ActorRef(kind=ActorKind.AGENT, id=UUID("64300000-0000-4000-8000-000000000013")),
        asked_event_id=UUID("64300000-0000-4000-8000-000000000014"),
        asked_at=NOW,
        correlation_id=UUID("64300000-0000-4000-8000-000000000015"),
    )
    first = RequirementsArtifact(
        artifact_id=clarification.originating_artifact_id,
        artifact_version=1,
        production_identity="requirements-extraction-v1",
        production_digest="a" * 64,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        work_item_id=WORK_ITEM_ID,
        run_id=RUN_ID,
        requirements=(requirement,),
        clarifications=(clarification,),
        state=RequirementState.PARTIAL,
    )
    answered = clarification.model_copy(update={
        "answer": ClarificationAnswer(
            text="December 20.",
            answered_by=ActorRef(kind=ActorKind.HUMAN, id=ACTOR_ID),
            answered_event_id=UUID("64300000-0000-4000-8000-000000000016"),
            answered_at=NOW,
        ),
        "resulting_artifact_version": 2,
    })
    successor = RequirementsArtifact(
        **{
            **first.model_dump(mode="python"),
            "artifact_version": 2,
            "predecessor_version": 1,
            "production_identity": "clarification-answer:v1:643",
            "production_digest": "b" * 64,
            "requirements": (requirement.model_copy(update={
                "missing_markers": (), "state": RequirementState.CANDIDATE,
            }),),
            "clarifications": (answered,),
            "state": RequirementState.CANDIDATE,
        }
    )
    return first, successor


def _answer_clarification() -> WorkflowCommand:
    first, _ = _clarification_artifacts()
    clarification = first.clarifications[0]
    return WorkflowCommand(
        command_id=UUID("64300000-0000-4000-8000-000000000001"),
        idempotency_key="answer-643",
        run_id=RUN_ID,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        actor=ActorRef(kind=ActorKind.HUMAN, id=ACTOR_ID),
        command_type="answer_clarification",
        command_schema_version=1,
        expected_run_version=2,
        payload=AnswerClarificationCommandV1(
            clarification_id=clarification.clarification_id,
            requirement_id=clarification.requirement_id,
            answer="December 20.",
        ),
        artifact_refs=(WorkflowArtifactRef(
            artifact_id=first.artifact_id,
            schema_name="requirements",
            schema_version=first.schema_version,
            artifact_version=1,
        ),),
        trace_id=TRACE_ID,
        correlation_id=UUID("64300000-0000-4000-8000-000000000002"),
        causation_id=UUID("64300000-0000-4000-8000-000000000003"),
    )


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
async def test_open_clarification_enters_one_durable_wait_with_safe_event() -> None:
    first, _ = _clarification_artifacts()
    uow = FakeUow(operation="workflow.run.command")
    uow.replay_row = _run_row(state="admitted", run_version=1)
    repository = PostgreSQLWorkflowRunAdmissionRepository(uow, clock=lambda: NOW)

    waiting = await repository.wait_for_clarification(first)

    assert waiting.state.value == "waiting"
    assert waiting.run_version == 2
    event_insert = next(
        statement for statement in uow.statements
        if getattr(getattr(statement, "table", None), "name", None) == "workflow_run_events"
    )
    assert event_insert.compile().params["event_type"] == "workflow.clarification-requested"
    assert "Delivery date" not in str(event_insert.compile().params)


@pytest.mark.asyncio
async def test_answer_clarification_resumes_same_run_and_persists_one_successor() -> None:
    _, successor = _clarification_artifacts()
    uow = FakeUow(operation="workflow.run.command")
    uow.replay_row = _run_row(state="waiting", run_version=2)
    repository = PostgreSQLWorkflowRunAdmissionRepository(uow, clock=lambda: NOW)

    receipt = await repository.dispatch(
        _answer_clarification(), clarification_successor=successor
    )

    assert receipt.run_id == RUN_ID
    assert receipt.state.value == "running"
    assert receipt.run_version == 3
    assert uow.persisted_artifacts == [successor]
    event_insert = next(
        statement for statement in uow.statements
        if getattr(getattr(statement, "table", None), "name", None) == "workflow_run_events"
    )
    assert event_insert.compile().params["event_type"] == "workflow.clarification-answered"
    assert "December 20" not in str(event_insert.compile().params)


@pytest.mark.asyncio
async def test_answer_clarification_keeps_same_run_waiting_for_next_question() -> None:
    _, successor = _clarification_artifacts()
    next_requirement = Requirement(
        requirement_id=UUID("64300000-0000-4000-8000-000000000020"),
        category="constraint",
        statement="Budget is ambiguous.",
        ambiguous_markers=("budget",),
        state=RequirementState.PARTIAL,
    )
    next_clarification = Clarification(
        clarification_id=UUID("64300000-0000-4000-8000-000000000021"),
        run_id=RUN_ID,
        requirement_id=next_requirement.requirement_id,
        originating_artifact_id=successor.artifact_id,
        originating_artifact_version=2,
        question="Please resolve the ambiguous information required to continue.",
        reason="ambiguous_information",
        asked_by=ActorRef(
            kind=ActorKind.AGENT,
            id=UUID("64300000-0000-4000-8000-000000000022"),
        ),
        asked_event_id=UUID("64300000-0000-4000-8000-000000000023"),
        asked_at=NOW,
        correlation_id=UUID("64300000-0000-4000-8000-000000000024"),
    )
    successor = successor.model_copy(
        update={
            "requirements": (*successor.requirements, next_requirement),
            "clarifications": (*successor.clarifications, next_clarification),
            "state": RequirementState.PARTIAL,
        }
    )
    uow = FakeUow(operation="workflow.run.command")
    uow.replay_row = _run_row(state="waiting", run_version=2)
    repository = PostgreSQLWorkflowRunAdmissionRepository(uow, clock=lambda: NOW)

    receipt = await repository.dispatch(
        _answer_clarification(), clarification_successor=successor
    )

    assert receipt.state.value == "waiting"
    assert uow.intents[-1].payload.next_clarification_id == next_clarification.clarification_id


@pytest.mark.asyncio
async def test_answer_clarification_replay_returns_receipt_without_second_successor() -> None:
    command = _answer_clarification()
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
        "resulting_run_version": 3,
    }
    repository = PostgreSQLWorkflowRunAdmissionRepository(uow, clock=lambda: NOW)

    receipt = await repository.dispatch(command)

    assert receipt.admission == "replayed"
    assert receipt.run_version == 3
    assert uow.persisted_artifacts == []
    assert uow.intents == []


@pytest.mark.asyncio
async def test_cancel_while_waiting_is_terminal_on_the_same_run() -> None:
    uow = FakeUow(operation="workflow.run.command")
    uow.replay_row = _run_row(state="waiting", run_version=2)
    repository = PostgreSQLWorkflowRunAdmissionRepository(uow, clock=lambda: NOW)
    command = WorkflowCommand(
        command_id=UUID("64300000-0000-4000-8000-000000000020"),
        idempotency_key="cancel-643",
        run_id=RUN_ID,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        actor=ActorRef(kind=ActorKind.HUMAN, id=ACTOR_ID),
        command_type="cancel",
        command_schema_version=1,
        expected_run_version=2,
        payload=CancelWorkflowCommandV1(),
        trace_id=TRACE_ID,
        correlation_id=UUID("64300000-0000-4000-8000-000000000021"),
        causation_id=UUID("64300000-0000-4000-8000-000000000022"),
    )

    receipt = await repository.dispatch(command)

    assert receipt.run_id == RUN_ID
    assert receipt.state.value == "cancelled"
    assert receipt.run_version == 3
    assert len(uow.intents) == 1
    assert uow.intents[0].event_type == "workflow.run-cancelled"


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
async def test_start_retry_ignores_attempt_specific_correlation_metadata() -> None:
    digests = []
    for correlation_id, causation_id in (
        (
            UUID("63000000-0000-4000-8000-000000000010"),
            UUID("63000000-0000-4000-8000-000000000011"),
        ),
        (
            UUID("63000000-0000-4000-8000-000000000012"),
            UUID("63000000-0000-4000-8000-000000000013"),
        ),
    ):
        uow = FakeUow()
        repository = PostgreSQLWorkflowRunAdmissionRepository(uow)
        await repository.admit(
            _command().model_copy(
                update={
                    "correlation_id": correlation_id,
                    "causation_id": causation_id,
                }
            )
        )
        digests.append(uow.digest.value)

    assert digests[0] == digests[1]


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
