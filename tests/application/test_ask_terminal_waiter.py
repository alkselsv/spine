from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from uuid import UUID

import pytest

from spine.application.ask_terminal_waiter import TerminalAskWaiter
from spine.application.persistence.ask_admission import (
    AskAdmissionLifecycle,
    AskAdmissionResult,
    AskAdmissionStatus,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.application.qa_observation.contracts import (
    ExecutionWaitState,
    SafeWaitUnavailableReason,
)
from spine.application.qa_sessions import QAMessageRead, QASessionRequester
from spine.domain.capabilities.answer_question import AnswerQuestionOutcome
from spine.domain.capabilities import (
    AcceptedAnswerClaimPresentation,
    AcceptedAnswerPresentation,
    AcceptedAnswerRelationshipPresentation,
    AnswerClaimKind,
    ClaimEvidenceRelationshipKind,
)
from spine.domain.qa_sessions import QAMessageDisclosureEligibility, QAMessageState
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from tests.application.qa_observation.factories import execution_identity


NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, duration: float) -> None:
        self.sleeps.append(duration)
        self.now += duration


class _Admission:
    def __init__(self, statuses: list[AskAdmissionStatus]) -> None:
        self.statuses = statuses
        self.calls = 0

    async def status(self, *, context, result):
        del context, result
        self.calls += 1
        return self.statuses[min(self.calls - 1, len(self.statuses) - 1)]


class _Sessions:
    def __init__(self, message: QAMessageRead | None) -> None:
        self.message = message
        self.calls = 0

    async def get_message(self, **kwargs):
        del kwargs
        self.calls += 1
        return self.message


def _context(identity, *, workspace_id=None, environment_id=None):
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=UUID("f1000000-0000-0000-0000-000000000475"),
        secret=b"terminal-ask-waiter-test-secret-475",
    )
    return boundary.interactive(
        scope=EnvironmentScope(
            workspace_id=workspace_id or identity.workspace_id,
            environment_id=environment_id or identity.environment_id,
        ),
        acting_subject_id=identity.requester.id,
        purpose=PersistencePurpose("question_answering"),
        operation=PersistenceOperation("qa.ask-admission-cancellation"),
        trace_id=UUID("f2000000-0000-0000-0000-000000000475"),
    )


def _admission_result(identity) -> AskAdmissionResult:
    return AskAdmissionResult(
        command_id=identity.command_id,
        run_id=identity.run_id,
        session_id=identity.session_id,
        message_id=identity.message_id,
        dispatch_event_id=UUID("90000000-0000-0000-0000-000000000475"),
        replay=False,
    )


def _status(identity, lifecycle, outcome=None, attempt_id=None):
    return AskAdmissionStatus(
        command_id=identity.command_id,
        session_id=identity.session_id,
        message_id=identity.message_id,
        run_id=identity.run_id,
        status=lifecycle,
        outcome=outcome,
        attempt_id=attempt_id,
    )


def _message(identity, outcome, *, eligible=True, answer=None):
    presentation = None
    if answer is not None:
        presentation = AcceptedAnswerPresentation(
            message_id=identity.message_id,
            run_id=identity.run_id,
            attempt_id=identity.attempt_id,
            answer_id=UUID("c0000000-0000-0000-0000-000000000475"),
            answer_text_sha256=sha256(answer.encode("utf-8")).hexdigest(),
            claims=(
                AcceptedAnswerClaimPresentation(
                    claim_id=UUID("d0000000-0000-0000-0000-000000000475"),
                    kind=AnswerClaimKind.FACTUAL,
                    start_offset=0,
                    end_offset=1,
                ),
            ),
            relationships=(
                AcceptedAnswerRelationshipPresentation(
                    relationship_id=UUID("e0000000-0000-0000-0000-000000000475"),
                    claim_id=UUID("d0000000-0000-0000-0000-000000000475"),
                    kind=ClaimEvidenceRelationshipKind.INDEPENDENT,
                    citation_ids=(UUID("a0000000-0000-0000-0000-000000000475"),),
                ),
            ),
            citation_ids=(UUID("a0000000-0000-0000-0000-000000000475"),),
        )
    return QAMessageRead(
        message_id=identity.message_id,
        session_id=identity.session_id,
        workspace_id=identity.workspace_id,
        environment_id=identity.environment_id,
        version=2,
        ordinal=1,
        trace_id=UUID("b0000000-0000-0000-0000-000000000475"),
        run_id=identity.run_id,
        state=QAMessageState.TERMINAL,
        terminal_outcome=outcome,
        cancelled=outcome is AnswerQuestionOutcome.CANCELLED,
        disclosure_eligibility=(
            QAMessageDisclosureEligibility.ELIGIBLE
            if eligible
            else QAMessageDisclosureEligibility.INELIGIBLE
        ),
        question="Synthetic question",
        answer=answer,
        accepted_presentation=presentation,
        created_at=NOW,
        updated_at=NOW,
    )


def _waiter(identity, statuses, message, clock=None):
    clock = clock or _Clock()
    admission = _Admission(statuses)
    sessions = _Sessions(message)
    waiter = TerminalAskWaiter(
        admission=admission,
        sessions=sessions,
        clock=clock,
        sleeper=clock.sleep,
    )
    args = {
        "identity": identity,
        "context": _context(identity),
        "admission_result": _admission_result(identity),
        "requester": QASessionRequester(identity.requester.id),
        "deadline": 10.0,
        "poll_interval": 0.25,
    }
    return waiter, args, admission, sessions, clock


@pytest.mark.asyncio
async def test_waiter_returns_exact_current_authorized_answer_after_pending_and_running():
    identity = execution_identity()
    answer = "Synthetic persisted answer"
    waiter, args, admission, sessions, clock = _waiter(
        identity,
        [
            _status(identity, AskAdmissionLifecycle.ADMITTED),
            _status(
                identity,
                AskAdmissionLifecycle.RUNNING,
                attempt_id=identity.attempt_id,
            ),
            _status(
                identity,
                AskAdmissionLifecycle.TERMINAL,
                AnswerQuestionOutcome.ANSWERED,
                identity.attempt_id,
            ),
        ],
        _message(identity, AnswerQuestionOutcome.ANSWERED, answer=answer),
    )

    result = await waiter.wait(**args)

    assert result.wait.state is ExecutionWaitState.TERMINAL
    assert result.wait.outcome is AnswerQuestionOutcome.ANSWERED
    assert result.message is not None and result.message.answer == answer
    assert admission.calls == 3
    assert sessions.calls == 1
    assert clock.sleeps == [0.25, 0.25]


@pytest.mark.parametrize(
    "outcome",
    [
        AnswerQuestionOutcome.CLARIFICATION_REQUIRED,
        AnswerQuestionOutcome.ABSTAINED,
        AnswerQuestionOutcome.DENIED,
        AnswerQuestionOutcome.FAILED,
        AnswerQuestionOutcome.CANCELLED,
    ],
)
@pytest.mark.asyncio
async def test_waiter_preserves_non_answer_terminal_outcomes(outcome):
    identity = execution_identity()
    waiter, args, _, _, _ = _waiter(
        identity,
        [
            _status(
                identity,
                AskAdmissionLifecycle.TERMINAL,
                outcome,
                identity.attempt_id,
            )
        ],
        _message(identity, outcome),
    )

    result = await waiter.wait(**args)

    assert result.wait.state is ExecutionWaitState.TERMINAL
    assert result.wait.outcome is outcome
    assert result.message is not None


@pytest.mark.asyncio
async def test_waiter_times_out_without_requesting_cancellation():
    identity = execution_identity()
    clock = _Clock()
    waiter, args, admission, sessions, _ = _waiter(
        identity,
        [
            _status(identity, AskAdmissionLifecycle.ADMITTED),
            _status(
                identity,
                AskAdmissionLifecycle.RUNNING,
                attempt_id=identity.attempt_id,
            ),
        ],
        None,
        clock,
    )
    args["deadline"] = 0.5

    result = await waiter.wait(**args)

    assert result.wait.state is ExecutionWaitState.UNAVAILABLE
    assert result.wait.unavailable_reason is SafeWaitUnavailableReason.DEADLINE_EXCEEDED
    assert admission.calls == 2
    assert sessions.calls == 0
    assert clock.sleeps == [0.25, 0.25]
    assert result.last_observed_state is ExecutionWaitState.RUNNING
    assert not hasattr(admission, "request_cancellation")


@pytest.mark.asyncio
async def test_waiter_rejects_cross_scope_and_requester_before_status_read():
    identity = execution_identity()
    waiter, args, admission, sessions, _ = _waiter(
        identity,
        [_status(identity, AskAdmissionLifecycle.ADMITTED)],
        None,
    )
    args["context"] = _context(
        identity,
        workspace_id=UUID("f0000000-0000-0000-0000-000000000475"),
    )

    result = await waiter.wait(**args)

    assert result.wait.unavailable_reason is SafeWaitUnavailableReason.CORRELATION_MISMATCH
    assert admission.calls == 0
    assert sessions.calls == 0


@pytest.mark.asyncio
async def test_waiter_rejects_requester_substitution_before_status_read():
    identity = execution_identity()
    waiter, args, admission, sessions, _ = _waiter(
        identity,
        [_status(identity, AskAdmissionLifecycle.ADMITTED)],
        None,
    )
    args["requester"] = QASessionRequester(
        UUID("f0000000-0000-0000-0000-000000000475")
    )

    result = await waiter.wait(**args)

    assert result.wait.unavailable_reason is SafeWaitUnavailableReason.CORRELATION_MISMATCH
    assert admission.calls == 0
    assert sessions.calls == 0


@pytest.mark.asyncio
async def test_waiter_fails_closed_when_running_attempt_cannot_be_correlated():
    identity = execution_identity()
    waiter, args, admission, sessions, _ = _waiter(
        identity,
        [_status(identity, AskAdmissionLifecycle.RUNNING)],
        None,
    )

    result = await waiter.wait(**args)

    assert result.wait.unavailable_reason is SafeWaitUnavailableReason.CORRELATION_MISMATCH
    assert admission.calls == 1
    assert sessions.calls == 0


@pytest.mark.asyncio
async def test_waiter_fails_closed_for_terminal_attempt_mismatch():
    identity = execution_identity()
    waiter, args, _, sessions, _ = _waiter(
        identity,
        [
            _status(
                identity,
                AskAdmissionLifecycle.TERMINAL,
                AnswerQuestionOutcome.ANSWERED,
                UUID("f0000000-0000-0000-0000-000000000475"),
            )
        ],
        _message(identity, AnswerQuestionOutcome.ANSWERED, answer="Protected"),
    )

    result = await waiter.wait(**args)

    assert result.wait.state is ExecutionWaitState.UNAVAILABLE
    assert result.wait.unavailable_reason is SafeWaitUnavailableReason.CORRELATION_MISMATCH
    assert result.message is None
    assert sessions.calls == 0


@pytest.mark.asyncio
async def test_waiter_fails_closed_when_canonical_terminal_message_is_missing():
    identity = execution_identity()
    waiter, args, _, sessions, _ = _waiter(
        identity,
        [
            _status(
                identity,
                AskAdmissionLifecycle.TERMINAL,
                AnswerQuestionOutcome.ABSTAINED,
                identity.attempt_id,
            )
        ],
        None,
    )

    result = await waiter.wait(**args)

    assert result.wait.unavailable_reason is SafeWaitUnavailableReason.INCOMPLETE_TERMINAL_STATE
    assert result.message is None
    assert sessions.calls == 1


@pytest.mark.asyncio
async def test_waiter_rejects_message_linked_to_another_run():
    identity = execution_identity()
    message = replace(
        _message(identity, AnswerQuestionOutcome.ABSTAINED),
        run_id=UUID("f0000000-0000-0000-0000-000000000475"),
    )
    waiter, args, _, _, _ = _waiter(
        identity,
        [
            _status(
                identity,
                AskAdmissionLifecycle.TERMINAL,
                AnswerQuestionOutcome.ABSTAINED,
                identity.attempt_id,
            )
        ],
        message,
    )

    result = await waiter.wait(**args)

    assert result.wait.unavailable_reason is SafeWaitUnavailableReason.CORRELATION_MISMATCH
    assert result.message is None


@pytest.mark.asyncio
async def test_waiter_maps_canonical_status_read_failure_to_safe_unavailable():
    identity = execution_identity()

    class BrokenAdmission:
        async def status(self, **kwargs):
            del kwargs
            raise RuntimeError("protected persistence diagnostic")

    waiter = TerminalAskWaiter(
        admission=BrokenAdmission(),
        sessions=_Sessions(None),
        clock=lambda: 0.0,
        sleeper=lambda _duration: None,
    )
    result = await waiter.wait(
        identity=identity,
        context=_context(identity),
        admission_result=_admission_result(identity),
        requester=QASessionRequester(identity.requester.id),
        deadline=1.0,
    )

    assert result.wait.state is ExecutionWaitState.UNAVAILABLE
    assert result.wait.unavailable_reason is SafeWaitUnavailableReason.READBACK_UNAVAILABLE
    assert "protected" not in str(result)


@pytest.mark.asyncio
async def test_waiter_does_not_return_answer_after_disclosure_revocation():
    identity = execution_identity()
    waiter, args, _, _, _ = _waiter(
        identity,
        [
            _status(
                identity,
                AskAdmissionLifecycle.TERMINAL,
                AnswerQuestionOutcome.ANSWERED,
                identity.attempt_id,
            )
        ],
        _message(
            identity,
            AnswerQuestionOutcome.ANSWERED,
            eligible=False,
            answer="Protected answer",
        ),
    )

    result = await waiter.wait(**args)

    assert result.wait.state is ExecutionWaitState.UNAVAILABLE
    assert result.wait.unavailable_reason is SafeWaitUnavailableReason.AUTHORIZATION_CHANGED
    assert result.message is None


@pytest.mark.asyncio
async def test_waiter_rejects_persisted_answer_with_mismatched_presentation_digest():
    identity = execution_identity()
    message = _message(
        identity,
        AnswerQuestionOutcome.ANSWERED,
        answer="Protected answer",
    )
    message = replace(
        message,
        accepted_presentation=message.accepted_presentation.model_copy(
            update={"answer_text_sha256": "f" * 64}
        ),
    )
    waiter, args, _, _, _ = _waiter(
        identity,
        [
            _status(
                identity,
                AskAdmissionLifecycle.TERMINAL,
                AnswerQuestionOutcome.ANSWERED,
                identity.attempt_id,
            )
        ],
        message,
    )

    result = await waiter.wait(**args)

    assert result.wait.unavailable_reason is SafeWaitUnavailableReason.AUTHORIZATION_CHANGED
    assert result.message is None


@pytest.mark.asyncio
async def test_waiter_bounds_a_stalled_canonical_status_read():
    identity = execution_identity()

    class StalledAdmission:
        async def status(self, **kwargs):
            del kwargs
            await asyncio.Event().wait()

    waiter = TerminalAskWaiter(
        admission=StalledAdmission(),
        sessions=_Sessions(None),
        clock=lambda: 0.0,
        max_wait_seconds=0.01,
    )
    result = await waiter.wait(
        identity=identity,
        context=_context(identity),
        admission_result=_admission_result(identity),
        requester=QASessionRequester(identity.requester.id),
        deadline=10.0,
    )

    assert result.wait.unavailable_reason is SafeWaitUnavailableReason.DEADLINE_EXCEEDED


@pytest.mark.asyncio
async def test_waiter_bounds_a_stalled_authorized_message_read():
    identity = execution_identity()

    class StalledSessions:
        async def get_message(self, **kwargs):
            del kwargs
            await asyncio.Event().wait()

    waiter = TerminalAskWaiter(
        admission=_Admission(
            [
                _status(
                    identity,
                    AskAdmissionLifecycle.TERMINAL,
                    AnswerQuestionOutcome.ABSTAINED,
                    identity.attempt_id,
                )
            ]
        ),
        sessions=StalledSessions(),
        clock=lambda: 0.0,
        max_wait_seconds=0.01,
    )

    result = await waiter.wait(
        identity=identity,
        context=_context(identity),
        admission_result=_admission_result(identity),
        requester=QASessionRequester(identity.requester.id),
        deadline=10.0,
    )

    assert result.wait.unavailable_reason is SafeWaitUnavailableReason.DEADLINE_EXCEEDED
    assert result.last_observed_state is ExecutionWaitState.TERMINAL
    assert result.message is None
