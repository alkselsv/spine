from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.persistence.ask_admission import (
    AskAdmissionCommand,
    QA_ASK_ADMISSION_OPERATION,
)
from spine.application.persistence.command_digest import digest_command
from spine.application.persistence.context import PersistenceOperation
from spine.application.persistence.errors import (
    IdempotencyConflictError,
    InvalidPersistenceContextError,
)
from spine.application.persistence.idempotency import IdempotencyKey
from spine.domain.capabilities.answer_question_runs import AnswerQuestionRunState
from spine.domain.common import ActorKind, ActorRef, EnvironmentKind
from spine.domain.qa_sessions import QAMessageState, QASession
from spine.domain.workspaces import Environment, Workspace

from .adapter import PersistenceAdapter
from .ids import synthetic_uuid


async def exercise_ask_admission_contract(adapter: PersistenceAdapter) -> None:
    assert adapter.ingestion_context is not None
    workspace_id = synthetic_uuid(42610)
    environment_id = synthetic_uuid(42611)
    session_id = synthetic_uuid(42612)
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)

    async with adapter.uow_factory(adapter.workspace_context(workspace_id)) as uow:
        await uow.workspaces.add(
            Workspace(
                id=workspace_id,
                slug="ask-admission-contract",
                display_name="Ask admission contract",
            )
        )
        await uow.environments.add(
            Environment(
                id=environment_id,
                workspace_id=workspace_id,
                kind=EnvironmentKind.PRODUCTION,
                display_name="Production",
            )
        )
        await uow.commit()

    session_context = adapter.ingestion_context(
        workspace_id, environment_id, "qa_sessions"
    )
    subject_id = session_context.service_principal_id
    assert subject_id is not None
    session = QASession(
        session_id=session_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        subject_id=subject_id,
        created_at=now,
        updated_at=now,
        version=1,
        status="active",
    )
    async with adapter.uow_factory(session_context) as uow:
        await uow.qa_sessions.create_session(
            session,
            command_id=synthetic_uuid(42613),
            idempotency_key=IdempotencyKey("qa-session-contract-426"),
            digest=digest_command(
                operation=PersistenceOperation("qa_sessions"),
                operation_schema_version=1,
                payload={"session_id": session_id, "subject_id": subject_id},
            ),
        )
        await uow.commit()

    context = adapter.ingestion_context(
        workspace_id, environment_id, QA_ASK_ADMISSION_OPERATION.value
    )
    command = AskAdmissionCommand.create(
        workspace_id=workspace_id,
        environment_id=environment_id,
        session_id=session_id,
        subject=ActorRef(kind=ActorKind.SERVICE, id=subject_id),
        environment=EnvironmentKind.PRODUCTION,
        question="What survives admission?",
        idempotency_key=IdempotencyKey("qa-admission-contract-426"),
    )
    async with adapter.uow_factory(context) as uow:
        first = await uow.ask_admissions.admit(command)
        await uow.commit()

    async with adapter.uow_factory(context) as uow:
        replay = await uow.ask_admissions.admit(command)
        run = await uow.answer_question_runs.resolve_run(replay.run_id)
        message = await uow.qa_sessions.resolve_message(
            workspace_id=workspace_id,
            environment_id=environment_id,
            session_id=session_id,
            message_id=replay.message_id,
        )
        await uow.commit()

    assert first.replay is False
    assert replay.replay is True
    assert replay.command_id == first.command_id
    assert replay.session_id == first.session_id
    assert replay.message_id == first.message_id
    assert replay.run_id == first.run_id
    assert replay.dispatch_event_id == first.dispatch_event_id
    assert run.run_id == first.run_id
    assert run.state is AnswerQuestionRunState.ADMITTED
    assert message is not None
    assert message.run_id == first.run_id
    assert message.state is QAMessageState.PENDING
    assert message.question == command.question

    changed = AskAdmissionCommand.create(
        workspace_id=workspace_id,
        environment_id=environment_id,
        session_id=session_id,
        subject=command.subject,
        environment=command.environment,
        question="A changed question",
        idempotency_key=command.idempotency_key,
    )
    async with adapter.uow_factory(context) as uow:
        with pytest.raises(IdempotencyConflictError):
            await uow.ask_admissions.admit(changed)

    unauthorized = replace(
        command,
        subject=ActorRef(kind=ActorKind.SERVICE, id=synthetic_uuid(914)),
    )
    async with adapter.uow_factory(context) as uow:
        with pytest.raises(InvalidPersistenceContextError):
            await uow.ask_admissions.admit(unauthorized)

    foreign_context = adapter.ingestion_context(
        synthetic_uuid(42614), synthetic_uuid(42615), QA_ASK_ADMISSION_OPERATION.value
    )
    with pytest.raises(InvalidPersistenceContextError):
        async with adapter.uow_factory(foreign_context) as uow:
            await uow.ask_admissions.admit(command)


@pytest.mark.asyncio
async def test_ask_admission_contract(persistence_adapter: PersistenceAdapter) -> None:
    await exercise_ask_admission_contract(persistence_adapter)
