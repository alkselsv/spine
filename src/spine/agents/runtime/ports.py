"""Framework-independent ports consumed by local agent handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

from spine.application.diagnostics import DiagnosticContext, DiagnosticSink
from spine.auth.contracts import (
    AuthorizedRequestContext,
    AuthorizationResolution,
    RequestedAuthorizationScope,
)
from spine.domain.common import EnvironmentKind

if TYPE_CHECKING:
    from spine.agents.runtime.cancellation import CancellationScope
    from spine.agents.runtime.contracts import AgentExecutionContext


@runtime_checkable
class CancellationToken(Protocol):
    """A cooperative, run-scoped cancellation observation port."""

    @property
    def is_cancelled(self) -> bool:
        """Whether cancellation has been requested for the current run."""

    def raise_if_cancelled(self) -> None:
        """Raise the caller's cancellation outcome when cancellation is observed."""


@runtime_checkable
class CancellationController(Protocol):
    """Trusted control side of a scoped cooperative cancellation handle."""

    @property
    def scope(self) -> CancellationScope:
        """Exact run identity controlled by this handle."""

    @property
    def token(self) -> CancellationToken:
        """Observation-only token exposed to the handler context."""

    def request_cancel(self, scope: CancellationScope) -> bool:
        """Request cancellation and report whether this was the first request."""

    def seal(self, scope: CancellationScope) -> bool:
        """Close cancellation before commit and report whether it may proceed."""


# Compatibility name for the first runtime contract.  Runtime diagnostics now
# consume the Issue #4 port directly instead of maintaining a parallel schema.
EventSink = DiagnosticSink


@runtime_checkable
class AuthorizationContextView(Protocol):
    """Minimal data view supplied by Issue #69/#108 authorization boundaries.

    Implementations are issued and verified outside the local contract layer.
    Structural compatibility and deterministic fakes make this a testable
    port; satisfying the protocol alone is not proof of authorization.
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


# Existing consumers use this approved port name. Keep it as an alias while
# making the non-authoritative view semantics explicit above.
TrustedExecutionIdentity = AuthorizationContextView


class TrustedAuthorizationContextVerifier(Protocol):
    """Issue #72 integration seam for canonical context provenance.

    ``AuthorizationResolution`` is validated directory data.  It is not proof
    that the sealed ``AuthorizedRequestContext`` was issued by the trusted
    composition root.  The verifier is deliberately required at the runtime
    construction boundary and is not implemented in this ticket.
    """

    def verify(
        self,
        context: AuthorizedRequestContext[DiagnosticContext],
        resolution: AuthorizationResolution,
    ) -> None:
        """Raise when the context was not issued and verified by the root."""


class CanonicalTrustedAuthorizationContextVerifier:
    """Composition-owned verifier base for production execution admission.

    The implementation is supplied by the infrastructure authorization
    boundary. Structural and nominal test verifiers may support plain
    construction tests, but a verifier alone cannot issue an admission
    accepted by the protected execution gate.
    """

    def verify(
        self,
        context: AuthorizedRequestContext[DiagnosticContext],
        resolution: AuthorizationResolution,
    ) -> None:
        raise NotImplementedError


@runtime_checkable
class TrustedExecutionAdmission(Protocol):
    """Current authorization admission required immediately before execution.

    An implementation is supplied by trusted composition and must revalidate
    the exact authorization context, lifecycle, scope and pinned execution
    selection. Structural compatibility alone is not production authority.
    """

    def verify(self, context: AgentExecutionContext) -> None:
        """Raise unless this exact context is currently admitted."""


class ExecutionScopeValidator(Protocol):
    """Authoritative Workspace/Environment UUID-to-kind and generation check."""

    def validate(
        self,
        *,
        scope: RequestedAuthorizationScope,
        environment: EnvironmentKind,
        authorization_generation: int,
    ) -> None:
        """Raise for an unknown, mismatched, or stale execution scope."""


__all__ = [
    "AuthorizationContextView",
    "CancellationController",
    "CancellationToken",
    "CanonicalTrustedAuthorizationContextVerifier",
    "EventSink",
    "ExecutionScopeValidator",
    "TrustedAuthorizationContextVerifier",
    "TrustedExecutionAdmission",
    "TrustedExecutionIdentity",
]
