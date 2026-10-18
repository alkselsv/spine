"""Bounded read-only observation of one canonical Ask terminal result."""

from __future__ import annotations

import asyncio
from hashlib import sha256
from math import isfinite
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from spine.application.persistence.ask_admission import (
    AskAdmissionApplication,
    AskAdmissionLifecycle,
    AskAdmissionResult,
    AskAdmissionStatus,
)
from spine.application.persistence.context import EnvironmentScope, TrustedPersistenceContext
from spine.application.qa_observation.contracts import (
    AdmittedExecutionIdentity,
    AdmittedRunIdentity,
    ExecutionWaitState,
    SafeWaitUnavailableReason,
    TerminalWaitResult,
    TerminalRunWaitResult,
)
from spine.application.qa_sessions import QAMessageRead, QASessionRequester, QASessionService
from spine.domain.capabilities.answer_question import AnswerQuestionOutcome
from spine.domain.qa_sessions import QAMessageDisclosureEligibility, QAMessageState

MAX_TERMINAL_ASK_WAIT_SECONDS = 30.0
MAX_TERMINAL_ASK_POLL_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class TerminalAskWaitResult:
    """Wait disposition, last canonical lifecycle, and authorized message."""

    wait: TerminalWaitResult | TerminalRunWaitResult
    message: QAMessageRead | None = None
    last_observed_state: ExecutionWaitState | None = None


@dataclass(frozen=True, slots=True)
class TerminalAskWaiter:
    """Observe Ask status and its authorized persisted message without mutation."""

    admission: AskAdmissionApplication
    sessions: QASessionService
    clock: Callable[[], float] = time.monotonic
    sleeper: Callable[[float], Awaitable[object]] = asyncio.sleep
    max_wait_seconds: float = MAX_TERMINAL_ASK_WAIT_SECONDS

    def __post_init__(self) -> None:
        if not isfinite(self.max_wait_seconds) or not (
            0 < self.max_wait_seconds <= MAX_TERMINAL_ASK_WAIT_SECONDS
        ):
            raise ValueError("terminal Ask wait bound must be between zero and thirty seconds")

    async def wait(
        self,
        *,
        identity: AdmittedExecutionIdentity,
        context: TrustedPersistenceContext,
        admission_result: AskAdmissionResult,
        requester: QASessionRequester,
        deadline: float,
        poll_interval: float = 0.05,
    ) -> TerminalAskWaitResult:
        """Wait for one expected canonical attempt and read its persisted message."""

        return await self._wait(
            identity=identity,
            context=context,
            admission_result=admission_result,
            requester=requester,
            deadline=deadline,
            poll_interval=poll_interval,
        )

    async def wait_for_run(
        self,
        *,
        identity: AdmittedRunIdentity,
        context: TrustedPersistenceContext,
        admission_result: AskAdmissionResult,
        requester: QASessionRequester,
        deadline: float,
        poll_interval: float = 0.05,
    ) -> TerminalAskWaitResult:
        """Wait by admitted run identity, resolving the attempt from canonical status."""

        return await self._wait(
            identity=identity,
            context=context,
            admission_result=admission_result,
            requester=requester,
            deadline=deadline,
            poll_interval=poll_interval,
        )

    async def _wait(
        self,
        *,
        identity: AdmittedExecutionIdentity | AdmittedRunIdentity,
        context: TrustedPersistenceContext,
        admission_result: AskAdmissionResult,
        requester: QASessionRequester,
        deadline: float,
        poll_interval: float,
    ) -> TerminalAskWaitResult:
        if not isfinite(poll_interval) or not (
            0 < poll_interval <= MAX_TERMINAL_ASK_POLL_INTERVAL_SECONDS
        ):
            raise ValueError("poll interval must be between zero and five seconds")
        started_at = self.clock()
        if not isfinite(deadline) or not isfinite(started_at):
            raise ValueError("deadline and monotonic clock must be finite")
        if deadline <= started_at:
            return self._identity_unavailable(
                identity, SafeWaitUnavailableReason.DEADLINE_EXCEEDED
            )
        bounded_deadline = min(deadline, started_at + self.max_wait_seconds)
        last_observed_state: ExecutionWaitState | None = None

        while True:
            is_run_identity = isinstance(identity, AdmittedRunIdentity)
            if not self._request_matches(identity, context, admission_result, requester):
                return self._identity_unavailable(
                    identity, SafeWaitUnavailableReason.CORRELATION_MISMATCH
                )
            read_timeout = self._remaining(bounded_deadline)
            if read_timeout <= 0:
                return self._identity_unavailable(
                    identity,
                    SafeWaitUnavailableReason.DEADLINE_EXCEEDED,
                    last_observed_state=last_observed_state,
                )
            try:
                status = await asyncio.wait_for(
                    self.admission.status(context=context, result=admission_result),
                    timeout=read_timeout,
                )
            except asyncio.TimeoutError:
                return self._identity_unavailable(
                    identity,
                    SafeWaitUnavailableReason.DEADLINE_EXCEEDED,
                    last_observed_state=last_observed_state,
                )
            except Exception:
                return self._identity_unavailable(
                    identity,
                    SafeWaitUnavailableReason.READBACK_UNAVAILABLE,
                    last_observed_state=last_observed_state,
                )
            status_matches = (
                self._status_matches_run(identity, status)
                if is_run_identity
                else self._status_matches(identity, status)
            )
            if not status_matches:
                return self._identity_unavailable(
                    identity, SafeWaitUnavailableReason.CORRELATION_MISMATCH
                )
            if status.status is AskAdmissionLifecycle.TERMINAL:
                if not is_run_identity:
                    return await self._read_terminal_message(
                        identity=identity,
                        context=context,
                        requester=requester,
                        status=status,
                        deadline=bounded_deadline,
                    )
                if status.attempt_id is None or status.attempt_number is None:
                    return self._identity_unavailable(
                        identity,
                        SafeWaitUnavailableReason.INCOMPLETE_TERMINAL_STATE,
                        last_observed_state=ExecutionWaitState.TERMINAL,
                    )
                execution_identity = identity.with_attempt(
                    attempt_id=status.attempt_id,
                    attempt_number=status.attempt_number,
                )
                terminal = await self._read_terminal_message(
                    identity=execution_identity,
                    context=context,
                    requester=requester,
                    status=status,
                    deadline=bounded_deadline,
                )
                if terminal.wait.state is not ExecutionWaitState.TERMINAL:
                    return self._identity_unavailable(
                        identity,
                        terminal.wait.unavailable_reason
                        or SafeWaitUnavailableReason.READBACK_UNAVAILABLE,
                        last_observed_state=terminal.last_observed_state,
                    )
                return TerminalAskWaitResult(
                    wait=TerminalRunWaitResult(
                        identity=identity,
                        state=ExecutionWaitState.TERMINAL,
                        outcome=terminal.wait.outcome,
                        execution_identity=execution_identity,
                    ),
                    message=terminal.message,
                    last_observed_state=terminal.last_observed_state,
                )
            last_observed_state = (
                ExecutionWaitState.RUNNING
                if status.status is AskAdmissionLifecycle.RUNNING
                else ExecutionWaitState.PENDING
            )
            now = self.clock()
            if not isfinite(now):
                return self._identity_unavailable(
                    identity,
                    SafeWaitUnavailableReason.READBACK_UNAVAILABLE,
                    last_observed_state=last_observed_state,
                )
            remaining = bounded_deadline - now
            if remaining <= 0:
                return self._identity_unavailable(
                    identity,
                    SafeWaitUnavailableReason.DEADLINE_EXCEEDED,
                    last_observed_state=last_observed_state,
                )
            try:
                await self.sleeper(min(poll_interval, remaining))
            except Exception:
                return self._identity_unavailable(
                    identity,
                    SafeWaitUnavailableReason.READBACK_UNAVAILABLE,
                    last_observed_state=last_observed_state,
                )

    async def _read_terminal_message(
        self,
        *,
        identity: AdmittedExecutionIdentity,
        context: TrustedPersistenceContext,
        requester: QASessionRequester,
        status: AskAdmissionStatus,
        deadline: float,
    ) -> TerminalAskWaitResult:
        terminal_state = ExecutionWaitState.TERMINAL
        if (
            status.outcome is None
            or status.attempt_id is None
            or status.attempt_number is None
        ):
            return self._unavailable(
                identity,
                SafeWaitUnavailableReason.INCOMPLETE_TERMINAL_STATE,
                last_observed_state=terminal_state,
            )
        if (
            status.attempt_id != identity.attempt_id
            or status.attempt_number != identity.attempt_number
        ):
            return self._unavailable(
                identity,
                SafeWaitUnavailableReason.CORRELATION_MISMATCH,
                last_observed_state=terminal_state,
            )
        try:
            message = await asyncio.wait_for(
                self.sessions.get_message(
                    context=context,
                    requester=requester,
                    session_id=identity.session_id,
                    message_id=identity.message_id,
                ),
                timeout=self._remaining(deadline),
            )
        except asyncio.TimeoutError:
            return self._unavailable(
                identity,
                SafeWaitUnavailableReason.DEADLINE_EXCEEDED,
                last_observed_state=terminal_state,
            )
        except Exception:
            return self._unavailable(
                identity,
                SafeWaitUnavailableReason.READBACK_UNAVAILABLE,
                last_observed_state=terminal_state,
            )
        if message is None:
            return self._unavailable(
                identity,
                SafeWaitUnavailableReason.INCOMPLETE_TERMINAL_STATE,
                last_observed_state=terminal_state,
            )
        if not isinstance(message, QAMessageRead):
            return self._unavailable(
                identity,
                SafeWaitUnavailableReason.READBACK_UNAVAILABLE,
                last_observed_state=terminal_state,
            )
        if message.state is not QAMessageState.TERMINAL:
            return self._unavailable(
                identity,
                SafeWaitUnavailableReason.INCOMPLETE_TERMINAL_STATE,
                last_observed_state=terminal_state,
            )
        if (
            message.message_id != identity.message_id
            or message.session_id != identity.session_id
            or message.run_id != identity.run_id
            or message.workspace_id != identity.workspace_id
            or message.environment_id != identity.environment_id
            or message.terminal_outcome is not status.outcome
            or message.cancelled != (status.outcome is AnswerQuestionOutcome.CANCELLED)
        ):
            return self._unavailable(
                identity,
                SafeWaitUnavailableReason.CORRELATION_MISMATCH,
                last_observed_state=terminal_state,
            )

        if status.outcome is AnswerQuestionOutcome.ANSWERED:
            presentation = message.accepted_presentation
            if (
                message.disclosure_eligibility is not QAMessageDisclosureEligibility.ELIGIBLE
                or message.answer is None
                or presentation is None
                or presentation.message_id != identity.message_id
                or presentation.run_id != identity.run_id
                or presentation.attempt_id != identity.attempt_id
                or presentation.answer_text_sha256
                != sha256(message.answer.encode("utf-8")).hexdigest()
                or not presentation.citation_ids
            ):
                return self._unavailable(
                    identity,
                    SafeWaitUnavailableReason.AUTHORIZATION_CHANGED,
                    last_observed_state=terminal_state,
                )
        elif message.answer is not None or message.accepted_presentation is not None:
            return self._unavailable(
                identity,
                SafeWaitUnavailableReason.CORRELATION_MISMATCH,
                last_observed_state=terminal_state,
            )

        return TerminalAskWaitResult(
            wait=TerminalWaitResult(
                identity=identity,
                state=ExecutionWaitState.TERMINAL,
                outcome=status.outcome,
            ),
            message=message,
            last_observed_state=terminal_state,
        )

    @staticmethod
    def _request_matches(
        identity: AdmittedExecutionIdentity | AdmittedRunIdentity,
        context: TrustedPersistenceContext,
        admission_result: AskAdmissionResult,
        requester: QASessionRequester,
    ) -> bool:
        scope = context.scope
        if not isinstance(scope, EnvironmentScope):
            return False
        actor_id = context.acting_subject_id or context.service_principal_id
        return (
            scope.workspace_id == identity.workspace_id
            and scope.environment_id == identity.environment_id
            and actor_id == identity.requester.id
            and requester.subject_id == identity.requester.id
            and admission_result.command_id == identity.command_id
            and admission_result.run_id == identity.run_id
            and admission_result.session_id == identity.session_id
            and admission_result.message_id == identity.message_id
        )

    @staticmethod
    def _status_matches_run(identity: AdmittedRunIdentity, status: AskAdmissionStatus) -> bool:
        return (
            isinstance(status, AskAdmissionStatus)
            and status.command_id == identity.command_id
            and status.run_id == identity.run_id
            and status.session_id == identity.session_id
            and status.message_id == identity.message_id
            and ((status.attempt_id is None) == (status.attempt_number is None))
        )

    @staticmethod
    def _status_matches(
        identity: AdmittedExecutionIdentity, status: AskAdmissionStatus
    ) -> bool:
        if not isinstance(status, AskAdmissionStatus):
            return False
        if (
            status.command_id != identity.command_id
            or status.run_id != identity.run_id
            or status.session_id != identity.session_id
            or status.message_id != identity.message_id
        ):
            return False
        if status.status is AskAdmissionLifecycle.RUNNING:
            return (
                status.attempt_id == identity.attempt_id
                and status.attempt_number == identity.attempt_number
            )
        return (
            status.attempt_id in (None, identity.attempt_id)
            and status.attempt_number in (None, identity.attempt_number)
        )

    def _remaining(self, deadline: float) -> float:
        now = self.clock()
        if not isfinite(now):
            return 0.0
        return max(0.0, deadline - now)

    @staticmethod
    def _unavailable(
        identity: AdmittedExecutionIdentity,
        reason: SafeWaitUnavailableReason,
        *,
        last_observed_state: ExecutionWaitState | None = None,
    ) -> TerminalAskWaitResult:
        return TerminalAskWaitResult(
            wait=TerminalWaitResult(
                identity=identity,
                state=ExecutionWaitState.UNAVAILABLE,
                unavailable_reason=reason,
            ),
            last_observed_state=last_observed_state,
        )

    @staticmethod
    def _run_unavailable(
        identity: AdmittedRunIdentity,
        reason: SafeWaitUnavailableReason,
        *,
        last_observed_state: ExecutionWaitState | None = None,
    ) -> TerminalAskWaitResult:
        return TerminalAskWaitResult(
            wait=TerminalRunWaitResult(
                identity=identity,
                state=ExecutionWaitState.UNAVAILABLE,
                unavailable_reason=reason,
            ),
            last_observed_state=last_observed_state,
        )

    @classmethod
    def _identity_unavailable(
        cls,
        identity: AdmittedExecutionIdentity | AdmittedRunIdentity,
        reason: SafeWaitUnavailableReason,
        *,
        last_observed_state: ExecutionWaitState | None = None,
    ) -> TerminalAskWaitResult:
        if isinstance(identity, AdmittedRunIdentity):
            return cls._run_unavailable(
                identity, reason, last_observed_state=last_observed_state
            )
        return cls._unavailable(
            identity, reason, last_observed_state=last_observed_state
        )


__all__ = ["TerminalAskWaitResult", "TerminalAskWaiter"]
