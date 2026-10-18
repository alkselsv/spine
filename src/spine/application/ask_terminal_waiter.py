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
    ExecutionWaitState,
    SafeWaitUnavailableReason,
    TerminalWaitResult,
)
from spine.application.qa_sessions import QAMessageRead, QASessionRequester, QASessionService
from spine.domain.capabilities.answer_question import AnswerQuestionOutcome
from spine.domain.qa_sessions import QAMessageDisclosureEligibility, QAMessageState

MAX_TERMINAL_ASK_WAIT_SECONDS = 30.0
MAX_TERMINAL_ASK_POLL_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class TerminalAskWaitResult:
    """Wait disposition plus the current-authorized message, when terminal."""

    wait: TerminalWaitResult
    message: QAMessageRead | None = None


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
        """Poll canonical status until terminal or the bounded monotonic deadline."""

        if not isfinite(poll_interval) or not (
            0 < poll_interval <= MAX_TERMINAL_ASK_POLL_INTERVAL_SECONDS
        ):
            raise ValueError("poll interval must be between zero and five seconds")
        started_at = self.clock()
        if not isfinite(deadline) or not isfinite(started_at):
            raise ValueError("deadline and monotonic clock must be finite")
        if deadline <= started_at:
            return self._unavailable(identity, SafeWaitUnavailableReason.DEADLINE_EXCEEDED)
        bounded_deadline = min(deadline, started_at + self.max_wait_seconds)

        while True:
            if not self._request_matches(identity, context, admission_result, requester):
                return self._unavailable(identity, SafeWaitUnavailableReason.CORRELATION_MISMATCH)
            try:
                status = await self.admission.status(context=context, result=admission_result)
            except Exception:
                return self._unavailable(identity, SafeWaitUnavailableReason.READBACK_UNAVAILABLE)

            if not self._status_matches(identity, status):
                return self._unavailable(identity, SafeWaitUnavailableReason.CORRELATION_MISMATCH)
            if status.status is AskAdmissionLifecycle.TERMINAL:
                return await self._read_terminal_message(
                    identity=identity,
                    context=context,
                    requester=requester,
                    status=status,
                )

            now = self.clock()
            if not isfinite(now):
                return self._unavailable(
                    identity, SafeWaitUnavailableReason.READBACK_UNAVAILABLE
                )
            remaining = bounded_deadline - now
            if remaining <= 0:
                return self._unavailable(identity, SafeWaitUnavailableReason.DEADLINE_EXCEEDED)
            try:
                await self.sleeper(min(poll_interval, remaining))
            except Exception:
                return self._unavailable(identity, SafeWaitUnavailableReason.READBACK_UNAVAILABLE)

    async def _read_terminal_message(
        self,
        *,
        identity: AdmittedExecutionIdentity,
        context: TrustedPersistenceContext,
        requester: QASessionRequester,
        status: AskAdmissionStatus,
    ) -> TerminalAskWaitResult:
        if status.outcome is None or status.attempt_id is None:
            return self._unavailable(
                identity, SafeWaitUnavailableReason.INCOMPLETE_TERMINAL_STATE
            )
        if status.attempt_id != identity.attempt_id:
            return self._unavailable(
                identity, SafeWaitUnavailableReason.CORRELATION_MISMATCH
            )
        try:
            message = await self.sessions.get_message(
                context=context,
                requester=requester,
                session_id=identity.session_id,
                message_id=identity.message_id,
            )
        except Exception:
            return self._unavailable(identity, SafeWaitUnavailableReason.READBACK_UNAVAILABLE)
        if message is None:
            return self._unavailable(
                identity, SafeWaitUnavailableReason.INCOMPLETE_TERMINAL_STATE
            )
        if not isinstance(message, QAMessageRead):
            return self._unavailable(identity, SafeWaitUnavailableReason.READBACK_UNAVAILABLE)
        if message.state is not QAMessageState.TERMINAL:
            return self._unavailable(
                identity, SafeWaitUnavailableReason.INCOMPLETE_TERMINAL_STATE
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
                identity, SafeWaitUnavailableReason.CORRELATION_MISMATCH
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
                    identity, SafeWaitUnavailableReason.AUTHORIZATION_CHANGED
                )
        elif message.answer is not None or message.accepted_presentation is not None:
            return self._unavailable(
                identity, SafeWaitUnavailableReason.CORRELATION_MISMATCH
            )

        return TerminalAskWaitResult(
            wait=TerminalWaitResult(
                identity=identity,
                state=ExecutionWaitState.TERMINAL,
                outcome=status.outcome,
            ),
            message=message,
        )

    @staticmethod
    def _request_matches(
        identity: AdmittedExecutionIdentity,
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
        return status.attempt_id in (None, identity.attempt_id)

    @staticmethod
    def _unavailable(
        identity: AdmittedExecutionIdentity,
        reason: SafeWaitUnavailableReason,
    ) -> TerminalAskWaitResult:
        return TerminalAskWaitResult(
            wait=TerminalWaitResult(
                identity=identity,
                state=ExecutionWaitState.UNAVAILABLE,
                unavailable_reason=reason,
            )
        )


__all__ = ["TerminalAskWaitResult", "TerminalAskWaiter"]
