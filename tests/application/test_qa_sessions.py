from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.persistence.context import EnvironmentScope, PersistenceOperation
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.errors import IdempotencyConflictError
from spine.application.diagnostics import DiagnosticContext
from spine.domain.capabilities import (
    AcceptedGroundedAnswer,
    AnswerClaimLocation,
    AnswerContent,
    AnswerDisclosureValidation,
)
from spine.domain.capabilities.answer_disclosure import AnswerDisclosureRecord
from spine.domain.capabilities.answer_question_runs import AnswerQuestionRun
from spine.domain.common import ActorKind, ActorRef, EnvironmentKind
from spine.application.qa_sessions import (
    PersistedQASessionService,
    QAMessagePresentationUnavailable,
    QASessionRequester,
    QASessionVersionConflict,
)
from spine.domain.capabilities import AnswerQuestionOutcome
from spine.infrastructure.persistence.qa_sessions import InMemoryQASessionRepository


WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ENVIRONMENT_ID = UUID("30000000-0000-0000-0000-000000000001")
SUBJECT_ID = UUID("10000000-0000-0000-0000-000000000001")
TRACE_ID = UUID("60000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def accepted_answer() -> AcceptedGroundedAnswer:
    from tests.domain.capabilities.test_answer_question_candidate import candidate_output

    source = candidate_output().answer
    replacements = {
        UUID(int=1): WORKSPACE_ID,
        UUID(int=2): ENVIRONMENT_ID,
        UUID(int=9): UUID("70000000-0000-0000-0000-000000000001"),
        UUID(int=10): UUID("70000000-0000-0000-0000-000000000002"),
    }

    def replace(value):
        if isinstance(value, UUID):
            return replacements.get(value, value)
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, tuple):
            return tuple(replace(item) for item in value)
        if isinstance(value, list):
            return [replace(item) for item in value]
        return value

    answer = AnswerContent.model_validate(replace(source.model_dump(mode="python")))
    text = "Привет 🌍 e\u0301"
    claim = answer.claims[0].model_copy(
        update={
            "text": text,
            "location": AnswerClaimLocation(
                answer_id=answer.answer_id,
                start_offset=0,
                end_offset=len(text),
            ),
        }
    )
    answer = answer.model_copy(update={"text": text, "claims": (claim,)})
    validation = AnswerDisclosureValidation(
        citation_validation_reference=UUID(int=710),
        semantic_support_validation_reference=UUID(int=711),
        evaluation_lineage_reference=UUID(int=712),
        disclosure_eligibility_reference=UUID(int=713),
        context_bundle_id=answer.citations[0].context_bundle.bundle.bundle_id,
        context_bundle_version=answer.citations[0].context_bundle.bundle.bundle_version,
        context_bundle_digest=answer.citations[0].context_bundle.bundle.bundle_digest,
        claim_ids=tuple(claim.claim_id for claim in answer.claims),
        citation_ids=tuple(citation.citation_id for citation in answer.citations),
    )
    return AcceptedGroundedAnswer(
        answer=answer,
        validation=validation,
        accepted_at=NOW,
    )


def completed_run(*, run_id: UUID, workspace_id: UUID, environment_id: UUID, attempt_id: UUID) -> AnswerQuestionRun:
    running = AnswerQuestionRun.start(
        run_id=run_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        environment=EnvironmentKind.PRODUCTION,
        subject=ActorRef(kind=ActorKind.HUMAN, id=SUBJECT_ID),
        idempotency_key="qa-session-test-run",
        command_digest="0" * 64,
        input_artifact_ids=(UUID(int=800),),
        diagnostic_context=DiagnosticContext(
            trace_id=TRACE_ID,
            correlation_id=UUID(int=801),
            causation_id=UUID(int=802),
        ),
        created_at=NOW,
    )
    return running.complete(
        outcome=AnswerQuestionOutcome.ANSWERED,
        final_attempt_id=attempt_id,
        output_artifact_ids=(UUID(int=803),),
        completed_at=NOW,
    )


def context(workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID):
    return SimpleNamespace(
        scope=EnvironmentScope(workspace_id=workspace_id, environment_id=environment_id),
        operation=PersistenceOperation("qa_sessions"),
        trace_id=TRACE_ID,
        validate_shape=lambda: None,
    )


class FakeEligibility:
    def __init__(self, allowed: bool):
        self.allowed = allowed
        self.calls = 0

    async def is_eligible(self, *, message, requester):
        del message, requester
        self.calls += 1
        return self.allowed


def service(*, allowed=True):
    eligibility = FakeEligibility(allowed)
    return PersistedQASessionService(
        repository=InMemoryQASessionRepository(),
        disclosure_eligibility=eligibility,
        clock=lambda: NOW,
    ), eligibility


@pytest.mark.asyncio
async def test_answered_message_exposes_accepted_presentation_only_from_final_attempt_join():
    class RunRepository:
        async def resolve_run_in_scope(self, *, workspace_id, environment_id, run_id):
            assert (workspace_id, environment_id) == (WORKSPACE_ID, ENVIRONMENT_ID)
            return completed_run(
                run_id=run_id,
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
                attempt_id=UUID("70000000-0000-0000-0000-000000000002"),
            )

    class DisclosureRepository:
        async def resolve_for_attempt(self, **kwargs):
            del kwargs
            return None

    qa, _ = service()
    qa = PersistedQASessionService(
        repository=qa.repository,
        disclosure_eligibility=qa.disclosure_eligibility,
        answer_question_runs=RunRepository(),
        answer_disclosures=DisclosureRepository(),
        clock=lambda: NOW,
    )
    requester = QASessionRequester(SUBJECT_ID)
    session = await qa.create_session(
        context=context(), requester=requester, idempotency_key=IdempotencyKey("join-session")
    )
    message = await qa.create_pending_message(
        context=context(), requester=requester, session_id=session.value.session_id,
        question="Protected question", run_id=UUID("70000000-0000-0000-0000-000000000001"),
        idempotency_key=IdempotencyKey("join-message"),
    )
    await qa.complete_message(
        context=context(), session_id=session.value.session_id, message_id=message.value.message_id,
        answer="Protected answer", outcome=AnswerQuestionOutcome.ANSWERED,
        cancelled=False, idempotency_key=IdempotencyKey("join-complete"),
    )

    with pytest.raises(QAMessagePresentationUnavailable):
        await qa.get_message(
            context=context(), requester=requester, session_id=session.value.session_id,
            message_id=message.value.message_id,
        )


@pytest.mark.asyncio
async def test_answered_message_projects_exact_unicode_spans_from_accepted_final_attempt():
    run_id = UUID("70000000-0000-0000-0000-000000000001")
    accepted = accepted_answer()

    class RunRepository:
        async def resolve_run_in_scope(self, *, workspace_id, environment_id, run_id):
            assert (workspace_id, environment_id) == (WORKSPACE_ID, ENVIRONMENT_ID)
            return completed_run(
                run_id=run_id,
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
                attempt_id=accepted.answer.attempt_id,
            )

    class DisclosureRepository:
        async def resolve_for_attempt(self, **kwargs):
            assert kwargs["run_id"] == run_id
            assert kwargs["attempt_id"] == accepted.answer.attempt_id
            return AnswerDisclosureRecord(
                request_id=UUID(int=720),
                request_digest="a" * 64,
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
                run_id=run_id,
                attempt_id=accepted.answer.attempt_id,
                outcome=AnswerQuestionOutcome.ANSWERED,
                validation=accepted.validation,
                accepted_answer=accepted,
                created_at=NOW,
            )

    qa, eligibility = service()
    qa = PersistedQASessionService(
        repository=qa.repository,
        disclosure_eligibility=qa.disclosure_eligibility,
        answer_question_runs=RunRepository(),
        answer_disclosures=DisclosureRepository(),
        clock=lambda: NOW,
    )
    requester = QASessionRequester(SUBJECT_ID)
    session = await qa.create_session(
        context=context(), requester=requester, idempotency_key=IdempotencyKey("unicode-session")
    )
    message = await qa.create_pending_message(
        context=context(), requester=requester, session_id=session.value.session_id,
        question="Protected question", run_id=run_id,
        idempotency_key=IdempotencyKey("unicode-message"),
    )
    await qa.complete_message(
        context=context(), session_id=session.value.session_id, message_id=message.value.message_id,
        answer=accepted.answer.text, outcome=AnswerQuestionOutcome.ANSWERED,
        cancelled=False, idempotency_key=IdempotencyKey("unicode-complete"),
    )

    read = await qa.get_message(
        context=context(), requester=requester, session_id=session.value.session_id,
        message_id=message.value.message_id,
    )

    assert read is not None
    assert read.answer == "Привет 🌍 e\u0301"
    assert read.accepted_presentation is not None
    assert read.accepted_presentation.claims[0].end_offset == len("Привет 🌍 e\u0301")
    assert read.accepted_presentation.answer_id == accepted.answer.answer_id
    assert eligibility.calls == 2


@pytest.mark.asyncio
async def test_session_create_replays_one_identity_and_changed_payload_conflicts():
    qa, _ = service()
    requester = QASessionRequester(SUBJECT_ID)
    key = IdempotencyKey("create-session")

    first = await qa.create_session(context=context(), requester=requester, idempotency_key=key)
    replay = await qa.create_session(context=context(), requester=requester, idempotency_key=key)

    assert first.value.session_id == replay.value.session_id
    assert replay.replay is True
    with pytest.raises(IdempotencyConflictError):
        await qa.create_session(
            context=context(), requester=QASessionRequester(UUID("10000000-0000-0000-0000-000000000002")), idempotency_key=key
        )


@pytest.mark.asyncio
async def test_message_content_is_hidden_when_current_disclosure_is_revoked():
    qa, eligibility = service(allowed=True)
    requester = QASessionRequester(SUBJECT_ID)
    session = await qa.create_session(
        context=context(), requester=requester, idempotency_key=IdempotencyKey("session")
    )
    message = await qa.create_pending_message(
        context=context(), requester=requester, session_id=session.value.session_id,
        question="Protected question", run_id=None, idempotency_key=IdempotencyKey("message"),
    )
    await qa.complete_message(
        context=context(), session_id=session.value.session_id, message_id=message.value.message_id,
        answer="Protected answer", outcome=AnswerQuestionOutcome.ANSWERED,
        cancelled=False, idempotency_key=IdempotencyKey("complete"),
    )
    eligibility.allowed = False

    read = await qa.get_message(
        context=context(), requester=requester, session_id=session.value.session_id,
        message_id=message.value.message_id,
    )

    assert read is not None
    assert read.question is None and read.answer is None
    assert read.disclosure_eligibility.value == "ineligible"
    assert eligibility.calls == 1


@pytest.mark.asyncio
async def test_restore_rejects_stale_version_without_changing_session():
    qa, _ = service()
    requester = QASessionRequester(SUBJECT_ID)
    created = await qa.create_session(
        context=context(), requester=requester, idempotency_key=IdempotencyKey("session")
    )
    repository = qa.repository
    await repository.archive_for_testing(created.value.session_id, WORKSPACE_ID, ENVIRONMENT_ID)

    restored = await qa.restore_session(
        context=context(), session_id=created.value.session_id, expected_version=2,
        idempotency_key=IdempotencyKey("restore"),
    )
    assert restored.value.status.value == "active"
    with pytest.raises(QASessionVersionConflict):
        await qa.restore_session(
            context=context(), session_id=created.value.session_id, expected_version=1,
            idempotency_key=IdempotencyKey("restore-again"),
        )


@pytest.mark.asyncio
async def test_message_run_link_and_cancellation_are_typed_terminal_state_transitions():
    qa, _ = service()
    requester = QASessionRequester(SUBJECT_ID)
    session = await qa.create_session(
        context=context(), requester=requester, idempotency_key=IdempotencyKey("session-transition")
    )
    message = await qa.create_pending_message(
        context=context(), requester=requester, session_id=session.value.session_id,
        question="Will this be cancelled?", run_id=None,
        idempotency_key=IdempotencyKey("message-transition"),
    )
    run_id = UUID("70000000-0000-0000-0000-000000000001")
    linked = await qa.link_message_to_run(
        context=context(), session_id=session.value.session_id,
        message_id=message.value.message_id, run_id=run_id,
        idempotency_key=IdempotencyKey("link-transition"),
    )
    cancelled = await qa.complete_message(
        context=context(), session_id=session.value.session_id,
        message_id=message.value.message_id, answer=None,
        outcome=AnswerQuestionOutcome.CANCELLED, cancelled=True,
        idempotency_key=IdempotencyKey("cancel-transition"),
    )

    assert linked.value.run_id == run_id
    assert linked.value.state.value == "running"
    assert cancelled.value.state.value == "terminal"
    assert cancelled.value.terminal_outcome is AnswerQuestionOutcome.CANCELLED
    assert cancelled.value.cancelled is True
    with pytest.raises(QASessionVersionConflict):
        await qa.link_message_to_run(
            context=context(), session_id=session.value.session_id,
            message_id=message.value.message_id, run_id=run_id,
            idempotency_key=IdempotencyKey("link-after-cancel"),
        )


@pytest.mark.asyncio
async def test_sessions_and_messages_are_isolated_between_workspace_environments():
    qa, _ = service()
    requester = QASessionRequester(SUBJECT_ID)
    other_workspace = UUID("20000000-0000-0000-0000-000000000002")
    other_environment = UUID("30000000-0000-0000-0000-000000000002")

    first = await qa.create_session(
        context=context(), requester=requester, idempotency_key=IdempotencyKey("scope-a")
    )
    await qa.create_pending_message(
        context=context(), requester=requester, session_id=first.value.session_id,
        question="Workspace A secret", run_id=None,
        idempotency_key=IdempotencyKey("scope-a-message"),
    )
    second = await qa.create_session(
        context=context(other_workspace, other_environment), requester=requester,
        idempotency_key=IdempotencyKey("scope-b"),
    )

    first_scope = await qa.list_sessions(context=context(), limit=10, after=None)
    second_scope = await qa.list_sessions(
        context=context(other_workspace, other_environment), limit=10, after=None
    )
    foreign_message = await qa.list_messages(
        context=context(other_workspace, other_environment), requester=requester,
        session_id=first.value.session_id, limit=10, after=None,
    )

    assert tuple(item.session_id for item in first_scope.items) == (first.value.session_id,)
    assert tuple(item.session_id for item in second_scope.items) == (second.value.session_id,)
    assert foreign_message is None
