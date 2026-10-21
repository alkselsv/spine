from __future__ import annotations

from uuid import UUID

import pytest

from spine.domain.common import ActorKind, ActorRef, EnvironmentKind
from spine.application.workflows.ask_admission_dispatch import AskAdmissionCommandData
from spine.workers.qa_runtime import (
    QAWorkerSettings,
    _CurrentAuthorizationGeneration,
    _qa_worker_context,
)
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.application.workflows.ask_admission_dispatch import AskAdmissionWorkflowInput


def test_qa_worker_settings_match_durable_ask_queue() -> None:
    settings = QAWorkerSettings(service_principal_id=UUID(int=50801))

    assert settings.temporal_address == "temporal:7233"
    assert settings.temporal_namespace == "default"
    assert settings.temporal_task_queue == "spine-r1-qa"


def test_qa_worker_context_is_scoped_to_workflow_input() -> None:
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=UUID(int=50802),
        secret=b"issue-508-qa-worker-context-secret",
    )
    workflow_input = AskAdmissionWorkflowInput(
        command_id=UUID(int=50803),
        workspace_id=UUID(int=50804),
        environment_id=UUID(int=50805),
        session_id=UUID(int=50806),
        message_id=UUID(int=50807),
        run_id=UUID(int=50808),
        trace_id=UUID(int=50809),
    )

    context = _qa_worker_context(boundary, UUID(int=50810), workflow_input)

    assert context.scope.workspace_id == workflow_input.workspace_id
    assert context.scope.environment_id == workflow_input.environment_id
    assert context.trace_id == workflow_input.trace_id
    assert context.service_principal_id == UUID(int=50810)
    assert context.operation.value == "answer_question_run"


@pytest.mark.asyncio
async def test_current_authorization_generation_reads_exact_worker_scope() -> None:
    workflow_input = AskAdmissionWorkflowInput(
        command_id=UUID(int=50803),
        workspace_id=UUID(int=50804),
        environment_id=UUID(int=50805),
        session_id=UUID(int=50806),
        message_id=UUID(int=50807),
        run_id=UUID(int=50808),
        trace_id=UUID(int=50809),
    )
    command = AskAdmissionCommandData(
        command_id=workflow_input.command_id,
        workspace_id=workflow_input.workspace_id,
        environment_id=workflow_input.environment_id,
        session_id=workflow_input.session_id,
        message_id=workflow_input.message_id,
        run_id=workflow_input.run_id,
        question="synthetic question",
        subject=ActorRef(kind=ActorKind.HUMAN, id=UUID(int=50811)),
        environment=EnvironmentKind.PRODUCTION,
        idempotency_key="ask-508-current-generation",
    )
    statements = []

    class Result:
        def __init__(self, value=None) -> None:
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        def begin(self):
            return self

        async def execute(self, statement, parameters):
            statements.append((str(statement), parameters))
            return Result(4 if len(statements) == 2 else None)

    generation = await _CurrentAuthorizationGeneration(Session)(
        workflow_input, command
    )

    assert generation == 4
    assert all(
        parameters == {
            "workspace_id": str(workflow_input.workspace_id),
            "environment_id": str(workflow_input.environment_id),
        }
        for _, parameters in statements
    )
