"""Pure policy for short, explicitly eligible local Agent retries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import Any

from spine.application.diagnostics import Retryability, StructuredError


_MAXIMUM_LOCAL_ATTEMPTS = 5
_MAXIMUM_BACKOFF = timedelta(seconds=30)
_MAXIMUM_TOTAL_BACKOFF = timedelta(seconds=60)
_POLICY_KEY = "runtime_retry_policy"


class AgentRetryPolicyError(ValueError):
    """A pinned Agent Version contains an invalid local retry policy."""


class AgentRetryReason(str, Enum):
    """Closed transient categories eligible for explicit local policy."""

    PROVIDER_UNAVAILABLE = "provider.unavailable"
    RATE_LIMITED = "provider.rate_limited"
    TOOL_UNAVAILABLE = "tool.unavailable"
    TRANSIENT_INTERNAL = "internal.transient"


_ERROR_REASON = {
    "spine.provider.unavailable": AgentRetryReason.PROVIDER_UNAVAILABLE,
    "spine.provider.rate_limited": AgentRetryReason.RATE_LIMITED,
    "spine.tool.unavailable": AgentRetryReason.TOOL_UNAVAILABLE,
    "spine.internal.unexpected": AgentRetryReason.TRANSIENT_INTERNAL,
}


@dataclass(frozen=True, slots=True)
class AgentRetryPolicy:
    """One bounded schedule admitted from immutable Agent Version metadata."""

    maximum_attempts: int = 1
    backoff: tuple[timedelta, ...] = ()
    eligible_reasons: frozenset[AgentRetryReason] = frozenset()

    @classmethod
    def from_runtime_config(cls, runtime_config: Mapping[str, Any]) -> AgentRetryPolicy:
        raw = runtime_config.get(_POLICY_KEY)
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping) or set(raw) != {
            "maximum_attempts",
            "backoff_seconds",
            "eligible_reasons",
        }:
            raise AgentRetryPolicyError("agent retry policy is invalid")

        maximum_attempts = raw["maximum_attempts"]
        if (
            isinstance(maximum_attempts, bool)
            or not isinstance(maximum_attempts, int)
            or not 1 <= maximum_attempts <= _MAXIMUM_LOCAL_ATTEMPTS
        ):
            raise AgentRetryPolicyError("agent retry policy is invalid")

        raw_backoff = raw["backoff_seconds"]
        if not isinstance(raw_backoff, Sequence) or isinstance(raw_backoff, (str, bytes)):
            raise AgentRetryPolicyError("agent retry policy is invalid")
        if len(raw_backoff) != maximum_attempts - 1:
            raise AgentRetryPolicyError("agent retry policy is invalid")
        backoff: list[timedelta] = []
        for seconds in raw_backoff:
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
                raise AgentRetryPolicyError("agent retry policy is invalid")
            delay = timedelta(seconds=float(seconds))
            if delay < timedelta(0) or delay > _MAXIMUM_BACKOFF:
                raise AgentRetryPolicyError("agent retry policy is invalid")
            backoff.append(delay)
        if sum(backoff, timedelta(0)) > _MAXIMUM_TOTAL_BACKOFF:
            raise AgentRetryPolicyError("agent retry policy is invalid")

        raw_reasons = raw["eligible_reasons"]
        if not isinstance(raw_reasons, Sequence) or isinstance(raw_reasons, (str, bytes)):
            raise AgentRetryPolicyError("agent retry policy is invalid")
        try:
            reasons = tuple(AgentRetryReason(reason) for reason in raw_reasons)
        except (TypeError, ValueError):
            raise AgentRetryPolicyError("agent retry policy is invalid") from None
        if len(set(reasons)) != len(reasons):
            raise AgentRetryPolicyError("agent retry policy is invalid")
        if maximum_attempts > 1 and not reasons:
            raise AgentRetryPolicyError("agent retry policy is invalid")
        if maximum_attempts == 1 and (backoff or reasons):
            raise AgentRetryPolicyError("agent retry policy is invalid")
        return cls(
            maximum_attempts=maximum_attempts,
            backoff=tuple(backoff),
            eligible_reasons=frozenset(reasons),
        )

    def retry_reason(
        self,
        error: StructuredError | None,
    ) -> AgentRetryReason | None:
        if error is None or error.retryability is not Retryability.AFTER_DELAY:
            return None
        reason = _ERROR_REASON.get(error.code)
        if reason not in self.eligible_reasons:
            return None
        return reason

    def delay_before(self, attempt_number: int) -> timedelta:
        if not 2 <= attempt_number <= self.maximum_attempts:
            raise AgentRetryPolicyError("agent retry attempt is outside policy")
        return self.backoff[attempt_number - 2]


__all__ = ["AgentRetryPolicy", "AgentRetryPolicyError", "AgentRetryReason"]
