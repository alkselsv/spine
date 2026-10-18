from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.ask_terminal_waiter import TerminalAskWaiter
from spine.application.persistence.ask_admission import (
    AskAdmissionLifecycle,
    AskAdmissionResult,
    AskAdmissionStatus,
)
from spine.application.persistence.context import EnvironmentScope
from spine.application.qa_observation.contracts import (
    ExecutionWaitState,
    SafeWaitUnavailableReason,
)
from spine.application.qa_sessions import QAMessageRead, QASessionRequester
from spine.domain.capabilities.answer_question import AnswerQuestionOutcome
from spine.domain.qa_sessions import QAMessageDisclosureEligibility, QAMessageState
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


def _context(identity):
    return SimpleNamespace(
        scope=EnvironmentScope(
            workspace_id=identity.workspace_id,
            environment_id=identity.environment_id,
        ),
        acting_subject_id=identity.requester.id,
        service_principal_id=None,
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
        presentation = SimpleNamespace(
            message_id=identity.message_id,
            run_id=identity.run_id,
            attempt_id=identity.attempt_id,
            answer_text_sha256=sha256(answer.encode("utf-8")).hexdigest(),
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
            _status(identity, AskAdmissionLifecycle.RUNNING),
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
        [_status(identity, AskAdmissionLifecycle.ADMITTED)],
        None,
        clock,
    )
    args["deadline"] = 0.5

    result = await waiter.wait(**args)

    assert result.wait.state is ExecutionWaitState.UNAVAILABLE
    assert result.wait.unavailable_reason is SafeWaitUnavailableReason.DEADLINE_EXCEEDED
    assert admission.calls == 3
    assert sessions.calls == 0
    assert clock.sleeps == [0.25, 0.25]
    assert not hasattr(admission, "request_cancellation")


@pytest.mark.asyncio
async def test_waiter_rejects_cross_scope_and_requester_before_status_read():
    identity = execution_identity()
    waiter, args, admission, sessions, _ = _waiter(
        identity,
        [_status(identity, AskAdmissionLifecycle.ADMITTED)],
        None,
    )
    args["context"] = SimpleNamespace(
        scope=EnvironmentScope(
            workspace_id=UUID("f0000000-0000-0000-0000-000000000475"),
            environment_id=identity.environment_id,
        ),
        acting_subject_id=identity.requester.id,
        service_principal_id=None,
    )

    result = await waiter.wait(**args)

    assert result.wait.unavailable_reason is SafeWaitUnavailableReason.CORRELATION_MISMATCH
    assert admission.calls == 0
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
