"""Framework-independent ports consumed by local agent handlers."""

from __future__ import annotations

from typing import Generic, Protocol, TypeVar, runtime_checkable
from uuid import UUID

EventT = TypeVar("EventT")


@runtime_checkable
class CancellationToken(Protocol):
    """A cooperative, run-scoped cancellation observation port."""

    @property
    def is_cancelled(self) -> bool:
        """Whether cancellation has been requested for the current run."""

    def raise_if_cancelled(self) -> None:
        """Raise the caller's cancellation outcome when cancellation is observed."""


@runtime_checkable
class EventSink(Protocol, Generic[EventT]):
    """Port for safe execution observations owned by the diagnostics boundary."""

    async def emit(self, event: EventT) -> None:
        """Accept one already-validated, disclosure-safe execution event."""


@runtime_checkable
class TrustedExecutionIdentity(Protocol):
    """Minimal structural view of Issue #69's trusted authorization context.

    Implementations are issued and verified by the authorization boundary. The
    agent runtime only consumes this view; it cannot construct authority from
    invocation payloads or diagnostic identifiers.
    """

    @property
    def workspace_id(self) -> UUID:
        """Trusted Workspace scope."""

    @property
    def environment_id(self) -> UUID:
        """Trusted Environment scope."""

    @property
    def acting_subject_id(self) -> UUID | None:
        """Trusted human/actor identity, when the request is interactive."""

    @property
    def service_principal_id(self) -> UUID | None:
        """Trusted service identity, when the request is worker-owned."""

    @property
    def authorization_generation(self) -> int:
        """Authorization-directory generation used for stale-context checks."""
