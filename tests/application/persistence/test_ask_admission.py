from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.persistence.ask_admission import (
    AskAdmissionCommand,
    QA_ASK_ADMISSION_OPERATION,
)
from spine.application.persistence.command_digest import digest_command
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    WorkspaceScope,
)
from spine.application.persistence.errors import (
    AnswerQuestionRunNotFoundError,
    ConstraintConflictError,
    IdempotencyConflictError,
)
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.outbox import OutboxEventRegistry
from spine.domain.common import ActorKind, ActorRef, EnvironmentKind
from spine.domain.qa_sessions import QASession
from spine.domain.workspaces import Environment, Workspace
from spine.infrastructure.persistence.contexts import (
    TrustedContextBoundary,
    create_initial_workspace_bootstrap_authority,
)
from spine.infrastructure.persistence.in_memory import InMemoryPersistence


WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000426")
ENVIRONMENT_ID = UUID("20000000-0000-0000-0000-000000000426")
SUBJECT_ID = UUID("30000000-0000-0000-0000-000000000426")
SESSION_ID = UUID("40000000-0000-0000-0000-000000000426")
RUN_ID = UUID("50000000-0000-0000-0000-000000000426")
MESSAGE_ID = UUID("60000000-0000-0000-0000-000000000426")
TRACE_ID = UUID("70000000-0000-0000-0000-000000000426")
ISSUER_ID = UUID("80000000-0000-0000-0000-000000000426")
NOW = datetime(2026, 10, 17, tzinfo=timezone.utc)


def _boundary() -> TrustedContextBoundary:
    return TrustedContextBoundary.for_testing(
        issuer_id=ISSUER_ID,
        secret=b"ask-admission-test-secret-426-0123456789",
    )


def _context(boundary: TrustedContextBoundary, *, operation: PersistenceOperation):
    return boundary.interactive(
        scope=EnvironmentScope(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
        ),
        acting_subject_id=SUBJECT_ID,
        purpose=PersistencePurpose("question_answering"),
        operation=operation,
        trace_id=TRACE_ID,
    )


def _command(question: str = "Cafe\u0301?") -> AskAdmissionCommand:
    return AskAdmissionCommand.create(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        session_id=SESSION_ID,
        subject=ActorRef(kind=ActorKind.HUMAN, id=SUBJECT_ID),
        environment=EnvironmentKind.PRODUCTION,
        question=question,
        idempotency_key=IdempotencyKey("ask-426"),
    )


async def _persistence() -> tuple[InMemoryPersistence, TrustedContextBoundary]:
    boundary = _boundary()
    bootstrap = create_initial_workspace_bootstrap_authority()
    persistence = InMemoryPersistence(
        context_verifier=boundary,
        outbox_events=OutboxEventRegistry(),
        bootstrap_authority=bootstrap,
        ask_admission_run_id_factory=lambda: RUN_ID,
        ask_admission_message_id_factory=lambda: MESSAGE_ID,
        ask_admission_clock=lambda: NOW,
    )
    await persistence.initial_workspace_bootstrap.create_initial_workspace(
        bootstrap,
        Workspace(id=WORKSPACE_ID, slug="ask-426", display_name="Ask 426"),
    )
    setup_context = boundary.interactive(
        scope=WorkspaceScope(workspace_id=WORKSPACE_ID),
        acting_subject_id=SUBJECT_ID,
        purpose=PersistencePurpose("workspace_setup"),
        operation=PersistenceOperation("environment_setup"),
        trace_id=TRACE_ID,
    )
    async with persistence.uow_factory(setup_context) as uow:
        await uow.environments.add(
            Environment(
                id=ENVIRONMENT_ID,
                workspace_id=WORKSPACE_ID,
                kind=EnvironmentKind.PRODUCTION,
                display_name="Production",
            )
        )
        await uow.commit()
    session_context = _context(
        boundary, operation=PersistenceOperation("qa_sessions")
    )
    session = QASession(
        session_id=SESSION_ID,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        subject_id=SUBJECT_ID,
        created_at=NOW,
        updated_at=NOW,
        version=1,
        status="active",
    )
    async with persistence.uow_factory(session_context) as uow:
        await uow.qa_sessions.create_session(
            session,
            command_id=UUID("90000000-0000-0000-0000-000000000426"),
            idempotency_key=IdempotencyKey("session-426"),
            digest=digest_command(
                operation=PersistenceOperation("qa_sessions"),
                operation_schema_version=1,
                payload={"subject_id": SUBJECT_ID},
            ),
        )
        await uow.commit()
    return persistence, boundary


@pytest.mark.asyncio
async def test_ask_admission_replay_returns_stable_identities_and_preserves_question() -> None:
    persistence, boundary = await _persistence()
    context = _context(boundary, operation=QA_ASK_ADMISSION_OPERATION)
    command = _command()

    async with persistence.uow_factory(context) as uow:
        first = await uow.ask_admissions.admit(command)
        await uow.commit()
    async with persistence.uow_factory(context) as uow:
        replay = await uow.ask_admissions.admit(command)
        run = await uow.answer_question_runs.resolve_run(replay.run_id)
        link = await uow.answer_question_runs.resolve_message_link(replay.run_id)
        message = await uow.qa_sessions.resolve_message(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            session_id=SESSION_ID,
            message_id=first.message_id,
        )
        await uow.commit()

    assert first.replay is False
    assert replay.replay is True
    assert replay.command_id == first.command_id
    assert replay.session_id == first.session_id
    assert replay.message_id == first.message_id
    assert replay.run_id == first.run_id
    assert replay.dispatch_event_id == first.dispatch_event_id
    assert run.command_digest == command.digest.value
    assert link is not None
    assert message is not None
    assert message.question == "Cafe\u0301?"


@pytest.mark.asyncio
async def test_ask_admission_rollback_removes_receipt_run_message_and_intent() -> None:
    persistence, boundary = await _persistence()
    context = _context(boundary, operation=QA_ASK_ADMISSION_OPERATION)
    command = _command()

    async with persistence.uow_factory(context) as uow:
        await uow.ask_admissions.admit(command)

    async with persistence.uow_factory(context) as uow:
        with pytest.raises(AnswerQuestionRunNotFoundError):
            await uow.answer_question_runs.resolve_run(RUN_ID)

    async with persistence.uow_factory(context) as uow:
        assert (
            await uow.qa_sessions.resolve_message(
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
                session_id=SESSION_ID,
                message_id=MESSAGE_ID,
            )
            is None
        )

    async with persistence.uow_factory(context) as uow:
        retry = await uow.ask_admissions.admit(command)
        await uow.commit()

    assert retry.replay is False


@pytest.mark.asyncio
async def test_ask_admission_changed_payload_conflicts_without_aliasing() -> None:
    persistence, boundary = await _persistence()
    context = _context(boundary, operation=QA_ASK_ADMISSION_OPERATION)
    async with persistence.uow_factory(context) as uow:
        await uow.ask_admissions.admit(_command())
        await uow.commit()

    async with persistence.uow_factory(context) as uow:
        with pytest.raises(IdempotencyConflictError):
            await uow.ask_admissions.admit(_command("Different question"))


@pytest.mark.asyncio
async def test_ask_admission_missing_clarification_target_is_rejected() -> None:
    persistence, boundary = await _persistence()
    context = _context(boundary, operation=QA_ASK_ADMISSION_OPERATION)
    command = AskAdmissionCommand.create(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        session_id=SESSION_ID,
        subject=ActorRef(kind=ActorKind.HUMAN, id=SUBJECT_ID),
        environment=EnvironmentKind.PRODUCTION,
        question="Follow up?",
        clarification_of_message_id=UUID("a0000000-0000-0000-0000-000000000426"),
        idempotency_key=IdempotencyKey("ask-426-clarification"),
    )

    async with persistence.uow_factory(context) as uow:
        with pytest.raises(ConstraintConflictError):
            await uow.ask_admissions.admit(command)


def test_ask_admission_digest_normalizes_identity_but_not_stored_source_text() -> None:
    first = _command("Cafe\u0301?")
    equivalent = _command("Caf\u00e9?")

    assert first.digest == equivalent.digest
    assert first.question == "Cafe\u0301?"
    assert first.digest.operation == QA_ASK_ADMISSION_OPERATION
