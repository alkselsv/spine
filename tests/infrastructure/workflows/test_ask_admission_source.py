from __future__ import annotations

from datetime import datetime, timezone
from typing import cast
from uuid import UUID

import pytest

from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import AnswerQuestionRunNotFoundError
from spine.application.workflows.ask_admission_dispatch import (
    AskAdmissionWorkflowInput,
)
from spine.domain.capabilities.answer_question_runs import (
    AnswerQuestionDiagnosticContext,
    AnswerQuestionRun,
)
from spine.domain.common import ActorKind, ActorRef, EnvironmentKind
from spine.infrastructure.workflows.ask_admission import (
    PersistenceAskAdmissionCommandSource,
)

WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000428")
ENVIRONMENT_ID = UUID("20000000-0000-0000-0000-000000000428")
SUBJECT_ID = UUID("30000000-0000-0000-0000-000000000428")
SESSION_ID = UUID("40000000-0000-0000-0000-000000000428")
MESSAGE_ID = UUID("50000000-0000-0000-0000-000000000428")
RUN_ID = UUID("60000000-0000-0000-0000-000000000428")
COMMAND_ID = UUID("70000000-0000-0000-0000-000000000428")
TRACE_ID = UUID("80000000-0000-0000-0000-000000000428")
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
        authorization_generation=1,
    )


class RunRepository:
    async def resolve_run_in_scope(self, **kwargs):
        assert kwargs["workspace_id"] == WORKSPACE_ID
        assert kwargs["environment_id"] == ENVIRONMENT_ID
        assert kwargs["run_id"] == RUN_ID
        return AnswerQuestionRun.admit(
            run_id=RUN_ID,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            environment=EnvironmentKind.PRODUCTION,
            subject=ActorRef(kind=ActorKind.HUMAN, id=SUBJECT_ID),
            idempotency_key="ask-428",
            command_digest="a" * 64,
            input_artifact_ids=(),
            diagnostic_context=AnswerQuestionDiagnosticContext(trace_id=TRACE_ID),
            created_at=NOW,
        )

    async def resolve_message_link(self, run_id):
        assert run_id == RUN_ID
        return type("Link", (), {"session_id": SESSION_ID, "message_id": MESSAGE_ID})()


class SessionRepository:
    async def resolve_message(self, **kwargs):
        assert kwargs["workspace_id"] == WORKSPACE_ID
        assert kwargs["environment_id"] == ENVIRONMENT_ID
        assert kwargs["session_id"] == SESSION_ID
        assert kwargs["message_id"] == MESSAGE_ID
        return type(
            "Message",
            (),
            {
                "run_id": RUN_ID,
                "workspace_id": WORKSPACE_ID,
                "environment_id": ENVIRONMENT_ID,
                "session_id": SESSION_ID,
                "message_id": MESSAGE_ID,
                "question": "protected question",
            },
        )()


class Uow:
    answer_question_runs = RunRepository()
    qa_sessions = SessionRepository()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


def context_factory(value: AskAdmissionWorkflowInput) -> TrustedPersistenceContext:
    del value
    return cast(TrustedPersistenceContext, object())


@pytest.mark.asyncio
async def test_persistence_source_loads_exact_scoped_lineage_without_outbox_content() -> (
    None
):
    seen: list[AskAdmissionWorkflowInput] = []

    def trusted_context(value: AskAdmissionWorkflowInput) -> TrustedPersistenceContext:
        seen.append(value)
        return cast(TrustedPersistenceContext, object())

    source = PersistenceAskAdmissionCommandSource(
        uow_factory=lambda context: Uow(), context_factory=trusted_context
    )

    command = await source.load_exact(workflow_input())

    assert command is not None
    assert command.question == "protected question"
    assert seen == [workflow_input()]
