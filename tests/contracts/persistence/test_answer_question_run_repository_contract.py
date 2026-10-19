import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from spine.application.diagnostics import DiagnosticContext
from spine.domain.agents import AgentResult, AgentRunStatus
from spine.application.persistence.answer_question_runs import (
    AnswerQuestionAttemptAllocation,
    AnswerQuestionRunCommand,
)
from spine.application.persistence.errors import (
    AnswerQuestionAttemptImmutableError,
    AnswerQuestionRunConflictError,
    AnswerQuestionRunNotFoundError,
    ContextBundleNotFoundError,
    IdempotencyConflictError,
    PersistenceUnavailableError,
)
from spine.application.persistence.idempotency import IdempotencyKey
from spine.domain.capabilities.answer_question import AnswerQuestionOutcome
from spine.domain.capabilities.answer_question_runs import (
    AnswerQuestionAttempt,
    AnswerQuestionAttemptEvent,
    AnswerQuestionAttemptEventKind,
    AnswerQuestionAttemptState,
    AnswerQuestionRun,
    AnswerQuestionUsage,
    AgentResultRef,
    SafeAttemptReason,
)
from spine.domain.common import ActorKind, ActorRef, EnvironmentKind
from spine.domain.context_bundles import ContextBundleRef
from spine.domain.workspaces import Environment, Workspace
from tests.domain.capabilities.test_answer_question_runs import _pin

from spine.infrastructure.persistence.answer_question_audit import (
    answer_question_run_audit_event_id,
)

from .adapter import AuditPersistenceAdapter, PersistenceAdapter
from .ids import synthetic_uuid


def run_record(workspace_id: UUID, environment_id: UUID, **overrides: object) -> AnswerQuestionRun:
    values: dict[str, object] = {
        "run_id": synthetic_uuid(4101),
        "workspace_id": workspace_id,
        "environment_id": environment_id,
        "environment": EnvironmentKind.PRODUCTION,
        "subject": ActorRef(kind=ActorKind.HUMAN, id=synthetic_uuid(4102)),
        "idempotency_key": "answer-question-command-1",
        "command_digest": "0" * 64,
        "input_artifact_ids": (synthetic_uuid(4103),),
        "diagnostic_context": DiagnosticContext(
            trace_id=synthetic_uuid(4104),
            correlation_id=synthetic_uuid(4105),
            causation_id=synthetic_uuid(4106),
        ),
        "created_at": datetime(2026, 10, 15, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return AnswerQuestionRun.start(**values)  # type: ignore[arg-type]


def run_command(run: AnswerQuestionRun) -> AnswerQuestionRunCommand:
    return AnswerQuestionRunCommand.create(
        run=run,
        idempotency_key=IdempotencyKey(run.idempotency_key),
    )


async def persist_scope(
    adapter: PersistenceAdapter, workspace_id: UUID, environment_id: UUID
) -> None:
    async with adapter.uow_factory(adapter.workspace_context(workspace_id)) as uow:
        await uow.workspaces.add(
            Workspace(
                id=workspace_id,
                slug=f"qa-{workspace_id.hex[-8:]}",
                display_name="Q&A contract",
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


def qa_context(adapter: PersistenceAdapter, workspace_id: UUID, environment_id: UUID):
    assert adapter.ingestion_context is not None
    return adapter.ingestion_context(workspace_id, environment_id, "answer_question_run")


@pytest.mark.asyncio
async def test_answer_question_run_create_and_replay_are_canonical_and_idempotent(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(4110)
    environment_id = synthetic_uuid(4111)
    await persist_scope(persistence_adapter, workspace_id, environment_id)
    context = qa_context(persistence_adapter, workspace_id, environment_id)
    command = run_command(
        run_record(
            workspace_id,
            environment_id,
            diagnostic_context=DiagnosticContext(trace_id=context.trace_id),
        )
    )

    async with persistence_adapter.uow_factory(context) as uow:
        created = await uow.answer_question_runs.create_run(command)
        await uow.commit()
    async with persistence_adapter.uow_factory(context) as uow:
        replay = await uow.answer_question_runs.create_run(command)
        await uow.commit()

    assert created.run == command.run
    assert created.replay is False
    assert replay.run == command.run
    assert replay.replay is True
    assert replay.result_reference == created.result_reference


@pytest.mark.asyncio
async def test_answer_question_run_scoped_resolution_cannot_cross_workspace(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(4112)
    environment_id = synthetic_uuid(4113)
    foreign_workspace_id = synthetic_uuid(4114)
    foreign_environment_id = synthetic_uuid(4117)
    await persist_scope(persistence_adapter, workspace_id, environment_id)
    await persist_scope(persistence_adapter, foreign_workspace_id, foreign_environment_id)
    command = run_command(run_record(workspace_id, environment_id))
    context = qa_context(persistence_adapter, workspace_id, environment_id)
    foreign_context = qa_context(
        persistence_adapter, foreign_workspace_id, foreign_environment_id
    )

    async with persistence_adapter.uow_factory(context) as uow:
        await uow.answer_question_runs.create_run(command)
        await uow.commit()

    async with persistence_adapter.uow_factory(context) as uow:
        resolved = await uow.answer_question_runs.resolve_run_in_scope(
            workspace_id=workspace_id,
            environment_id=environment_id,
            run_id=command.run.run_id,
        )
    assert resolved == command.run

    with pytest.raises(AnswerQuestionRunNotFoundError):
        async with persistence_adapter.uow_factory(foreign_context) as uow:
            await uow.answer_question_runs.resolve_run_in_scope(
                workspace_id=foreign_workspace_id,
                environment_id=foreign_environment_id,
                run_id=command.run.run_id,
            )


@pytest.mark.asyncio
async def test_answer_question_run_create_commits_required_audit_atomically(
    persistence_adapter: PersistenceAdapter,
) -> None:
    assert isinstance(persistence_adapter, AuditPersistenceAdapter)
    workspace_id = synthetic_uuid(4115)
    environment_id = synthetic_uuid(4116)
    await persist_scope(persistence_adapter, workspace_id, environment_id)
    command = run_command(run_record(workspace_id, environment_id))
    context = qa_context(persistence_adapter, workspace_id, environment_id)
    audit_id = answer_question_run_audit_event_id(
        workspace_id,
        environment_id,
        command.run.run_id,
        "answer_question.run.started",
    )

    persistence_adapter.fail_next_audit_append()
    with pytest.raises(PersistenceUnavailableError):
        async with persistence_adapter.uow_factory(context) as uow:
            await uow.answer_question_runs.create_run(command)

    assert await persistence_adapter.read_audit_event(context, audit_id) is None
    with pytest.raises(AnswerQuestionRunNotFoundError):
        async with persistence_adapter.uow_factory(context) as uow:
            await uow.answer_question_runs.resolve_run(command.run.run_id)

    async with persistence_adapter.uow_factory(context) as uow:
        await uow.answer_question_runs.create_run(command)
        await uow.commit()

    audit = await persistence_adapter.read_audit_event(context, audit_id)
    assert audit is not None
    assert audit.target is not None
    assert audit.target.object_id == command.run.run_id
    assert audit.payload_json() == {"transition": "answer_question.run.started"}


@pytest.mark.asyncio
async def test_answer_question_run_conflicting_command_key_fails_closed(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(4120)
    environment_id = synthetic_uuid(4121)
    await persist_scope(persistence_adapter, workspace_id, environment_id)
    original = run_record(workspace_id, environment_id)
    changed = run_record(
        workspace_id,
        environment_id,
        run_id=synthetic_uuid(4122),
        input_artifact_ids=(synthetic_uuid(4123),),
    )
    context = qa_context(persistence_adapter, workspace_id, environment_id)

    async with persistence_adapter.uow_factory(context) as uow:
        await uow.answer_question_runs.create_run(run_command(original))
        await uow.commit()

    with pytest.raises(IdempotencyConflictError):
        async with persistence_adapter.uow_factory(context) as uow:
            await uow.answer_question_runs.create_run(run_command(changed))


@pytest.mark.asyncio
async def test_answer_question_attempt_allocation_resumes_and_orders_logical_attempts(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(4124)
    environment_id = synthetic_uuid(4125)
    await persist_scope(persistence_adapter, workspace_id, environment_id)
    command = run_command(run_record(workspace_id, environment_id))
    context = qa_context(persistence_adapter, workspace_id, environment_id)
    first_request = AnswerQuestionAttemptAllocation(
        run_id=command.run.run_id,
        logical_execution_key="durable-ask:logical-1",
        execution_pin=_pin(workspace_id, environment_id),
        input_artifact_ids=command.run.input_artifact_ids,
    )

    async with persistence_adapter.uow_factory(context) as uow:
        await uow.answer_question_runs.create_run(command)
        first = await uow.answer_question_runs.allocate_or_resume_attempt(first_request)
        await uow.commit()
    async with persistence_adapter.uow_factory(context) as uow:
        replay = await uow.answer_question_runs.allocate_or_resume_attempt(first_request)
        await uow.commit()

    assert first.replay is False
    assert replay.replay is True
    assert replay.attempt == first.attempt
    assert first.attempt.attempt_number == 1
    assert first.attempt.logical_execution_key == "durable-ask:logical-1"

    with pytest.raises(AnswerQuestionRunConflictError):
        async with persistence_adapter.uow_factory(context) as uow:
            await uow.answer_question_runs.allocate_or_resume_attempt(
                AnswerQuestionAttemptAllocation(
                    run_id=command.run.run_id,
                    logical_execution_key="durable-ask:logical-1",
                    execution_pin=_pin(workspace_id, environment_id),
                    input_artifact_ids=(synthetic_uuid(4126),),
                )
            )

    failed = first.attempt.complete(
        state=AnswerQuestionAttemptState.FAILED,
        outcome=AnswerQuestionOutcome.FAILED,
        reason=SafeAttemptReason.PROVIDER_UNAVAILABLE,
        usage=AnswerQuestionUsage(),
        terminal_event=AnswerQuestionAttemptEvent(
            event_id=synthetic_uuid(4127),
            workspace_id=workspace_id,
            environment_id=environment_id,
            environment=EnvironmentKind.PRODUCTION,
            kind=AnswerQuestionAttemptEventKind.FAILED,
            reason=SafeAttemptReason.PROVIDER_UNAVAILABLE,
            occurred_at=first.attempt.started_at + timedelta(seconds=1),
        ),
    )
    async with persistence_adapter.uow_factory(context) as uow:
        await uow.answer_question_runs.complete_attempt(failed)
        second = await uow.answer_question_runs.allocate_or_resume_attempt(
            AnswerQuestionAttemptAllocation(
                run_id=command.run.run_id,
                logical_execution_key="durable-ask:logical-2",
                execution_pin=_pin(workspace_id, environment_id),
                input_artifact_ids=command.run.input_artifact_ids,
            )
        )
        await uow.commit()

    assert second.attempt.attempt_number == 2
    assert second.attempt.attempt_id != first.attempt.attempt_id


@pytest.mark.asyncio
async def test_answer_question_attempt_allocation_rejects_cancellation_winner(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(4128)
    environment_id = synthetic_uuid(4129)
    await persist_scope(persistence_adapter, workspace_id, environment_id)
    context = qa_context(persistence_adapter, workspace_id, environment_id)
    command = run_command(
        run_record(
            workspace_id,
            environment_id,
            diagnostic_context=DiagnosticContext(trace_id=context.trace_id),
        )
    )
    async with persistence_adapter.uow_factory(context) as uow:
        await uow.answer_question_runs.create_run(command)
        await uow.answer_question_runs.request_cancellation(
            run_id=command.run.run_id,
            requested_at=command.run.created_at + timedelta(seconds=1),
        )
        await uow.commit()

    with pytest.raises(AnswerQuestionRunConflictError):
        async with persistence_adapter.uow_factory(context) as uow:
            await uow.answer_question_runs.allocate_or_resume_attempt(
                AnswerQuestionAttemptAllocation(
                    run_id=command.run.run_id,
                    logical_execution_key="durable-ask:cancelled",
                    execution_pin=_pin(workspace_id, environment_id),
                    input_artifact_ids=command.run.input_artifact_ids,
                )
            )


@pytest.mark.asyncio
async def test_answer_question_attempt_terminal_record_is_immutable_and_replayable(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(4130)
    environment_id = synthetic_uuid(4131)
    await persist_scope(persistence_adapter, workspace_id, environment_id)
    command = run_command(run_record(workspace_id, environment_id))
    context = qa_context(persistence_adapter, workspace_id, environment_id)
    attempt = AnswerQuestionAttempt.start(
        attempt_id=synthetic_uuid(4132),
        run=command.run,
        attempt_number=1,
        execution_pin=_pin(workspace_id, environment_id),
        input_artifact_ids=command.run.input_artifact_ids,
        diagnostic_context=command.run.diagnostic_context,
        started_at=datetime(2026, 10, 15, tzinfo=timezone.utc),
    )
    failure_event = AnswerQuestionAttemptEvent(
        event_id=synthetic_uuid(4133),
        workspace_id=workspace_id,
        environment_id=environment_id,
        environment=EnvironmentKind.PRODUCTION,
        kind=AnswerQuestionAttemptEventKind.FAILED,
        reason=SafeAttemptReason.PROVIDER_UNAVAILABLE,
        occurred_at=datetime(2026, 10, 15, 0, 1, tzinfo=timezone.utc),
    )
    terminal = attempt.complete(
        state=AnswerQuestionAttemptState.FAILED,
        outcome=AnswerQuestionOutcome.FAILED,
        reason=SafeAttemptReason.PROVIDER_UNAVAILABLE,
        usage=AnswerQuestionUsage(model_invocations=1, input_tokens=10),
        terminal_event=failure_event,
    )

    async with persistence_adapter.uow_factory(context) as uow:
        await uow.answer_question_runs.create_run(command)
        await uow.answer_question_runs.start_attempt(attempt)
        await uow.commit()

    tampered_lineage = terminal.model_copy(
        update={
            "subject": ActorRef(
                kind=ActorKind.HUMAN,
                id=synthetic_uuid(4134),
            )
        }
    )
    with pytest.raises(AnswerQuestionAttemptImmutableError):
        async with persistence_adapter.uow_factory(context) as uow:
            await uow.answer_question_runs.complete_attempt(tampered_lineage)

    async with persistence_adapter.uow_factory(context) as uow:
        first = await uow.answer_question_runs.complete_attempt(terminal)
        await uow.commit()

    async with persistence_adapter.uow_factory(context) as uow:
        replay = await uow.answer_question_runs.complete_attempt(terminal)
        await uow.commit()

    assert first == terminal
    assert replay == terminal

    conflicting = terminal.model_copy(
        update={
            "reason": SafeAttemptReason.RETRY_EXHAUSTED,
            "events": (
                terminal.events[0],
                failure_event.model_copy(update={"reason": SafeAttemptReason.RETRY_EXHAUSTED}),
            ),
        }
    )
    with pytest.raises(AnswerQuestionAttemptImmutableError):
        async with persistence_adapter.uow_factory(context) as uow:
            await uow.answer_question_runs.complete_attempt(conflicting)


@pytest.mark.asyncio
async def test_answer_question_attempt_rejects_unresolvable_context_bundle_reference(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(4135)
    environment_id = synthetic_uuid(4136)
    await persist_scope(persistence_adapter, workspace_id, environment_id)
    command = run_command(run_record(workspace_id, environment_id))
    context = qa_context(persistence_adapter, workspace_id, environment_id)
    attempt = AnswerQuestionAttempt.start(
        attempt_id=synthetic_uuid(4137),
        run=command.run,
        attempt_number=1,
        execution_pin=_pin(workspace_id, environment_id),
        input_artifact_ids=command.run.input_artifact_ids,
        diagnostic_context=command.run.diagnostic_context,
        started_at=datetime(2026, 10, 15, tzinfo=timezone.utc),
    )
    completed = attempt.complete(
        state=AnswerQuestionAttemptState.SUCCEEDED,
        outcome=AnswerQuestionOutcome.ANSWERED,
        context_bundle_ref=ContextBundleRef(
            bundle_id=synthetic_uuid(4138),
            bundle_version=1,
            bundle_digest="d" * 64,
        ),
        output_artifact_ids=(synthetic_uuid(4139),),
        agent_result_ref=AgentResultRef(result_id=synthetic_uuid(4140)),
        agent_result=AgentResult(
            status=AgentRunStatus.SUCCEEDED,
            output_artifact_ids=(synthetic_uuid(4139),),
        ),
        usage=AnswerQuestionUsage(model_invocations=1),
        terminal_event=AnswerQuestionAttemptEvent(
            event_id=synthetic_uuid(4141),
            workspace_id=workspace_id,
            environment_id=environment_id,
            environment=EnvironmentKind.PRODUCTION,
            kind=AnswerQuestionAttemptEventKind.COMPLETED,
            occurred_at=datetime(2026, 10, 15, 0, 1, tzinfo=timezone.utc),
        ),
    )

    async with persistence_adapter.uow_factory(context) as uow:
        await uow.answer_question_runs.create_run(command)
        await uow.answer_question_runs.start_attempt(attempt)
        await uow.commit()

    with pytest.raises(ContextBundleNotFoundError):
        async with persistence_adapter.uow_factory(context) as uow:
            await uow.answer_question_runs.complete_attempt(completed)

    async with persistence_adapter.uow_factory(context) as uow:
        assert await uow.answer_question_runs.resolve_attempt(attempt.attempt_id) == attempt


@pytest.mark.asyncio
async def test_answer_question_attempt_concurrent_completion_has_one_terminal_winner(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(4145)
    environment_id = synthetic_uuid(4146)
    await persist_scope(persistence_adapter, workspace_id, environment_id)
    command = run_command(run_record(workspace_id, environment_id))
    context = qa_context(persistence_adapter, workspace_id, environment_id)
    attempt = AnswerQuestionAttempt.start(
        attempt_id=synthetic_uuid(4147),
        run=command.run,
        attempt_number=1,
        execution_pin=_pin(workspace_id, environment_id),
        input_artifact_ids=command.run.input_artifact_ids,
        diagnostic_context=command.run.diagnostic_context,
        started_at=datetime(2026, 10, 15, tzinfo=timezone.utc),
    )

    def terminal(
        *,
        state: AnswerQuestionAttemptState,
        outcome: AnswerQuestionOutcome,
        reason: SafeAttemptReason,
        event_id: UUID,
    ) -> AnswerQuestionAttempt:
        return attempt.complete(
            state=state,
            outcome=outcome,
            reason=reason,
            usage=AnswerQuestionUsage(),
            terminal_event=AnswerQuestionAttemptEvent(
                event_id=event_id,
                workspace_id=workspace_id,
                environment_id=environment_id,
                environment=EnvironmentKind.PRODUCTION,
                kind=(
                    AnswerQuestionAttemptEventKind.FAILED
                    if state is AnswerQuestionAttemptState.FAILED
                    else AnswerQuestionAttemptEventKind.CANCELLED
                ),
                occurred_at=datetime(2026, 10, 15, 0, 1, tzinfo=timezone.utc),
                reason=reason,
            ),
        )

    winner = terminal(
        state=AnswerQuestionAttemptState.FAILED,
        outcome=AnswerQuestionOutcome.FAILED,
        reason=SafeAttemptReason.PROVIDER_UNAVAILABLE,
        event_id=synthetic_uuid(4148),
    )
    loser = terminal(
        state=AnswerQuestionAttemptState.CANCELLED,
        outcome=AnswerQuestionOutcome.CANCELLED,
        reason=SafeAttemptReason.CANCELLED_BY_REQUEST,
        event_id=synthetic_uuid(4149),
    )
    async with persistence_adapter.uow_factory(context) as uow:
        await uow.answer_question_runs.create_run(command)
        await uow.answer_question_runs.start_attempt(attempt)
        await uow.commit()

    first_prepared = asyncio.Event()
    second_started = asyncio.Event()
    first_committed = asyncio.Event()

    async def first_writer() -> None:
        async with persistence_adapter.uow_factory(context) as uow:
            await uow.answer_question_runs.complete_attempt(winner)
            first_prepared.set()
            await second_started.wait()
            await uow.commit()
            first_committed.set()

    async def second_writer() -> None:
        await first_prepared.wait()
        with pytest.raises(AnswerQuestionAttemptImmutableError):
            async with persistence_adapter.uow_factory(context) as uow:
                second_started.set()
                await uow.answer_question_runs.complete_attempt(loser)
                await first_committed.wait()
                await uow.commit()

    await asyncio.gather(first_writer(), second_writer())

    async with persistence_adapter.uow_factory(context) as uow:
        assert await uow.answer_question_runs.resolve_attempt(attempt.attempt_id) == winner


@pytest.mark.asyncio
async def test_answer_question_run_resolution_does_not_cross_workspace_or_environment(
    persistence_adapter: PersistenceAdapter,
) -> None:
    owner_workspace = synthetic_uuid(4140)
    owner_environment = synthetic_uuid(4141)
    other_workspace = synthetic_uuid(4142)
    other_environment = synthetic_uuid(4143)
    sibling_environment = synthetic_uuid(4144)
    await persist_scope(persistence_adapter, owner_workspace, owner_environment)
    await persist_scope(persistence_adapter, other_workspace, other_environment)
    async with persistence_adapter.uow_factory(
        persistence_adapter.workspace_context(owner_workspace)
    ) as uow:
        await uow.environments.add(
            Environment(
                id=sibling_environment,
                workspace_id=owner_workspace,
                kind=EnvironmentKind.STAGING,
                display_name="Sibling",
            )
        )
        await uow.commit()
    command = run_command(run_record(owner_workspace, owner_environment))

    async with persistence_adapter.uow_factory(
        qa_context(persistence_adapter, owner_workspace, owner_environment)
    ) as uow:
        await uow.answer_question_runs.create_run(command)
        await uow.commit()

    with pytest.raises(AnswerQuestionRunNotFoundError) as raised:
        async with persistence_adapter.uow_factory(
            qa_context(persistence_adapter, other_workspace, other_environment)
        ) as uow:
            await uow.answer_question_runs.resolve_run(command.run.run_id)

    assert str(owner_workspace) not in str(raised.value)
    assert str(command.run.run_id) not in str(raised.value)

    with pytest.raises(AnswerQuestionRunNotFoundError):
        async with persistence_adapter.uow_factory(
            qa_context(persistence_adapter, owner_workspace, sibling_environment)
        ) as uow:
            await uow.answer_question_runs.resolve_run(command.run.run_id)


@pytest.mark.asyncio
async def test_answer_question_partial_failure_retry_and_cancellation_are_replayable(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(4150)
    environment_id = synthetic_uuid(4151)
    await persist_scope(persistence_adapter, workspace_id, environment_id)
    command = run_command(run_record(workspace_id, environment_id))
    context = qa_context(persistence_adapter, workspace_id, environment_id)
    started_at = datetime(2026, 10, 15, tzinfo=timezone.utc)

    first = AnswerQuestionAttempt.start(
        attempt_id=synthetic_uuid(4152),
        run=command.run,
        attempt_number=1,
        execution_pin=_pin(workspace_id, environment_id),
        input_artifact_ids=command.run.input_artifact_ids,
        diagnostic_context=command.run.diagnostic_context,
        started_at=started_at,
    )
    failed = first.complete(
        state=AnswerQuestionAttemptState.FAILED,
        outcome=AnswerQuestionOutcome.FAILED,
        reason=SafeAttemptReason.PROVIDER_UNAVAILABLE,
        usage=AnswerQuestionUsage(model_invocations=1, input_tokens=8),
        terminal_event=AnswerQuestionAttemptEvent(
            event_id=synthetic_uuid(4153),
            workspace_id=workspace_id,
            environment_id=environment_id,
            environment=EnvironmentKind.PRODUCTION,
            kind=AnswerQuestionAttemptEventKind.FAILED,
            reason=SafeAttemptReason.PROVIDER_UNAVAILABLE,
            occurred_at=datetime(2026, 10, 15, 0, 1, tzinfo=timezone.utc),
        ),
    )
    retry = AnswerQuestionAttempt.start(
        attempt_id=synthetic_uuid(4154),
        run=command.run,
        attempt_number=2,
        execution_pin=_pin(workspace_id, environment_id),
        input_artifact_ids=command.run.input_artifact_ids,
        diagnostic_context=command.run.diagnostic_context,
        started_at=datetime(2026, 10, 15, 0, 2, tzinfo=timezone.utc),
    )
    cancelled = retry.complete(
        state=AnswerQuestionAttemptState.CANCELLED,
        outcome=AnswerQuestionOutcome.CANCELLED,
        reason=SafeAttemptReason.CANCELLED_BY_REQUEST,
        usage=AnswerQuestionUsage(),
        terminal_event=AnswerQuestionAttemptEvent(
            event_id=synthetic_uuid(4155),
            workspace_id=workspace_id,
            environment_id=environment_id,
            environment=EnvironmentKind.PRODUCTION,
            kind=AnswerQuestionAttemptEventKind.CANCELLED,
            reason=SafeAttemptReason.CANCELLED_BY_REQUEST,
            occurred_at=datetime(2026, 10, 15, 0, 3, tzinfo=timezone.utc),
        ),
    )
    completed_run = command.run.complete(
        outcome=AnswerQuestionOutcome.CANCELLED,
        final_attempt_id=cancelled.attempt_id,
        completed_at=datetime(2026, 10, 15, 0, 3, tzinfo=timezone.utc),
    )

    async with persistence_adapter.uow_factory(context) as uow:
        await uow.answer_question_runs.create_run(command)
        await uow.answer_question_runs.start_attempt(first)
        await uow.answer_question_runs.complete_attempt(failed)
        await uow.answer_question_runs.start_attempt(retry)
        await uow.answer_question_runs.complete_attempt(cancelled)
        await uow.commit()

    with pytest.raises(AnswerQuestionRunConflictError):
        async with persistence_adapter.uow_factory(context) as uow:
            await uow.answer_question_runs.complete_run(
                completed_run.model_copy(update={"outcome": AnswerQuestionOutcome.FAILED})
            )

    async with persistence_adapter.uow_factory(context) as uow:
        first_completion = await uow.answer_question_runs.complete_run(completed_run)
        replay = await uow.answer_question_runs.complete_run(completed_run)
        cancellation_loser = await uow.answer_question_runs.complete_run(
            completed_run.model_copy(
                update={"outcome": AnswerQuestionOutcome.FAILED}
            )
        )
        await uow.commit()

    async with persistence_adapter.uow_factory(context) as uow:
        attempts = await uow.answer_question_runs.list_attempts(command.run.run_id)
        resolved = await uow.answer_question_runs.resolve_run(command.run.run_id)

    assert attempts == (failed, cancelled)
    assert first_completion == completed_run
    assert replay == completed_run
    assert cancellation_loser == completed_run
    assert resolved == completed_run


async def exercise_answer_question_run_completion_and_cancellation_cas(
    persistence_adapter: PersistenceAdapter,
    first_winner: str,
    wait_until_second_writer_blocked: Callable[[], Awaitable[None]],
) -> None:
    id_offset = 0 if first_winner == "completion" else 10
    workspace_id = synthetic_uuid(4170 + id_offset)
    environment_id = synthetic_uuid(4171 + id_offset)
    await persist_scope(persistence_adapter, workspace_id, environment_id)
    context = qa_context(persistence_adapter, workspace_id, environment_id)
    command = run_command(
        run_record(
            workspace_id,
            environment_id,
            run_id=synthetic_uuid(4178 + id_offset),
            diagnostic_context=DiagnosticContext(
                trace_id=context.trace_id,
                correlation_id=synthetic_uuid(4176 + id_offset),
                causation_id=synthetic_uuid(4177 + id_offset),
            ),
        )
    )
    started_at = datetime(2026, 10, 15, tzinfo=timezone.utc)

    completion_attempt_running = AnswerQuestionAttempt.start(
        attempt_id=synthetic_uuid(4172 + id_offset),
        run=command.run,
        attempt_number=1,
        execution_pin=_pin(workspace_id, environment_id),
        input_artifact_ids=command.run.input_artifact_ids,
        diagnostic_context=command.run.diagnostic_context,
        started_at=started_at,
    )
    completion_attempt = completion_attempt_running.complete(
        state=AnswerQuestionAttemptState.SUCCEEDED,
        outcome=AnswerQuestionOutcome.ABSTAINED,
        usage=AnswerQuestionUsage(),
        terminal_event=AnswerQuestionAttemptEvent(
            event_id=synthetic_uuid(4173 + id_offset),
            workspace_id=workspace_id,
            environment_id=environment_id,
            environment=EnvironmentKind.PRODUCTION,
            kind=AnswerQuestionAttemptEventKind.COMPLETED,
            occurred_at=started_at + timedelta(minutes=1),
        ),
    )
    cancellation_attempt_running = AnswerQuestionAttempt.start(
        attempt_id=synthetic_uuid(4174 + id_offset),
        run=command.run,
        attempt_number=2,
        execution_pin=_pin(workspace_id, environment_id),
        input_artifact_ids=command.run.input_artifact_ids,
        diagnostic_context=command.run.diagnostic_context,
        started_at=started_at + timedelta(minutes=2),
    )
    cancellation_attempt = cancellation_attempt_running.complete(
        state=AnswerQuestionAttemptState.CANCELLED,
        outcome=AnswerQuestionOutcome.CANCELLED,
        reason=SafeAttemptReason.CANCELLED_BY_REQUEST,
        usage=AnswerQuestionUsage(),
        terminal_event=AnswerQuestionAttemptEvent(
            event_id=synthetic_uuid(4175 + id_offset),
            workspace_id=workspace_id,
            environment_id=environment_id,
            environment=EnvironmentKind.PRODUCTION,
            kind=AnswerQuestionAttemptEventKind.CANCELLED,
            occurred_at=started_at + timedelta(minutes=3),
            reason=SafeAttemptReason.CANCELLED_BY_REQUEST,
        ),
    )

    async with persistence_adapter.uow_factory(context) as uow:
        await uow.answer_question_runs.create_run(command)
        await uow.answer_question_runs.start_attempt(completion_attempt_running)
        await uow.answer_question_runs.start_attempt(cancellation_attempt_running)
        await uow.commit()

    async with persistence_adapter.uow_factory(context) as uow:
        await uow.answer_question_runs.complete_attempt(completion_attempt)
        await uow.answer_question_runs.complete_attempt(cancellation_attempt)
        await uow.commit()

    cancellation_requested_at = started_at + timedelta(minutes=4)
    async with persistence_adapter.uow_factory(context) as uow:
        requested = await uow.answer_question_runs.request_cancellation(
            run_id=command.run.run_id,
            requested_at=cancellation_requested_at,
        )
        await uow.commit()

    completion = requested.complete(
        outcome=AnswerQuestionOutcome.ABSTAINED,
        final_attempt_id=completion_attempt.attempt_id,
        completed_at=completion_attempt.completed_at,
    )
    cancellation = requested.complete(
        outcome=AnswerQuestionOutcome.CANCELLED,
        final_attempt_id=cancellation_attempt.attempt_id,
        completed_at=cancellation_attempt.completed_at,
    )
    first_candidate, second_candidate = (
        (completion, cancellation)
        if first_winner == "completion"
        else (cancellation, completion)
    )
    first_prepared = asyncio.Event()
    second_entered = asyncio.Event()
    allow_first_commit = asyncio.Event()

    async def first_writer() -> AnswerQuestionRun:
        async with persistence_adapter.uow_factory(context) as uow:
            result = await uow.answer_question_runs.complete_run(first_candidate)
            first_prepared.set()
            await allow_first_commit.wait()
            await uow.commit()
            return result

    async def second_writer() -> AnswerQuestionRun:
        await first_prepared.wait()
        async with persistence_adapter.uow_factory(context) as uow:
            second_entered.set()
            result = await uow.answer_question_runs.complete_run(second_candidate)
            await uow.commit()
            return result

    first_task = asyncio.create_task(first_writer())
    second_task = asyncio.create_task(second_writer())
    try:
        await asyncio.wait_for(second_entered.wait(), timeout=10)
        await wait_until_second_writer_blocked()
    except BaseException:
        allow_first_commit.set()
        await asyncio.gather(first_task, second_task, return_exceptions=True)
        raise
    allow_first_commit.set()
    first_result, second_result = await asyncio.wait_for(
        asyncio.gather(first_task, second_task), timeout=10
    )
    expected = completion if first_winner == "completion" else cancellation

    async with persistence_adapter.uow_factory(context) as uow:
        persisted = await uow.answer_question_runs.resolve_run(command.run.run_id)

    assert first_result == expected
    assert second_result == expected
    assert persisted == expected
