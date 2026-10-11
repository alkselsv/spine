"""Run-scoped cooperative cancellation for local agent execution."""

from __future__ import annotations

from asyncio import CancelledError
from dataclasses import dataclass
from threading import Event, Lock
from uuid import UUID


class CooperativeCancellationError(CancelledError):
    """The current local agent run observed a cooperative cancellation."""


@dataclass(frozen=True, slots=True)
class CancellationScope:
    """Identity of the one run that a cancellation handle may affect."""

    workspace_id: UUID
    environment_id: UUID
    step_run_id: UUID
    agent_run_id: UUID

    def __post_init__(self) -> None:
        if any(
            identifier.int == 0
            for identifier in (
                self.workspace_id,
                self.environment_id,
                self.step_run_id,
                self.agent_run_id,
            )
        ):
            raise ValueError("cancellation scope identifiers must be non-zero")


class _CancellationState:
    __slots__ = ("accepting_requests", "cancelled", "lock")

    def __init__(self) -> None:
        self.accepting_requests = True
        self.cancelled = Event()
        self.lock = Lock()


class CooperativeCancellationToken:
    """Observation-only cancellation handle shared with untrusted execution.

    The token is deliberately in-memory and non-durable. It exposes no method
    that can request or disable cancellation.
    """

    __slots__ = ("_state",)

    def __init__(self, state: _CancellationState) -> None:
        self._state = state

    @property
    def is_cancelled(self) -> bool:
        return self._state.cancelled.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise CooperativeCancellationError


class CooperativeCancellationController:
    """Trusted control façade paired with one observation-only token."""

    __slots__ = ("_scope", "_state", "_token")

    def __init__(self, scope: CancellationScope) -> None:
        if not isinstance(scope, CancellationScope):
            raise TypeError("cooperative cancellation scope is invalid")
        self._scope = scope
        self._state = _CancellationState()
        self._token = CooperativeCancellationToken(self._state)

    @property
    def scope(self) -> CancellationScope:
        return self._scope

    @property
    def token(self) -> CooperativeCancellationToken:
        return self._token

    def request_cancel(self, scope: CancellationScope) -> bool:
        """Request cancellation, returning true only for the first request."""

        if scope != self._scope:
            raise ValueError("cancellation scope is invalid")
        with self._state.lock:
            if (
                not self._state.accepting_requests
                or self._state.cancelled.is_set()
            ):
                return False
            self._state.cancelled.set()
            return True

    def seal(self, scope: CancellationScope) -> bool:
        """Close the request window and report whether cancellation lost the race.

        The runtime seals immediately before its externally visible commit.
        Requests that lose that atomic boundary are safe no-ops because the
        already-started effect cannot be retracted cooperatively.
        """

        if scope != self._scope:
            raise ValueError("cancellation scope is invalid")
        with self._state.lock:
            self._state.accepting_requests = False
            return not self._state.cancelled.is_set()


__all__ = [
    "CancellationScope",
    "CooperativeCancellationController",
    "CooperativeCancellationError",
    "CooperativeCancellationToken",
]
