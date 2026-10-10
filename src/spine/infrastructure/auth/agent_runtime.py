"""Issue #72 bridge for the local agent execution admission port."""

from __future__ import annotations

from spine.agents.runtime.ports import TrustedAuthorizationContextVerifier
from spine.application.diagnostics import DiagnosticContext
from spine.auth.contracts import (
    AuthorizedRequestContext,
    AuthorizationResolution,
)
from spine.infrastructure.auth.trusted_context import (
    AuthorizedRequest,
    TrustedRequestContextBoundary,
)


class RequestAgentAuthorizationVerifier(TrustedAuthorizationContextVerifier):
    """Revalidate one exact active #72 request before agent execution.

    The bridge retains neither credentials nor a serialized proof. It holds the
    request-local #72 objects and delegates lifecycle/provenance validation to
    ``TrustedRequestContextBoundary`` on every call.
    """

    __slots__ = ("_authorized", "_boundary")

    def __init__(
        self,
        *,
        boundary: TrustedRequestContextBoundary,
        authorized: AuthorizedRequest,
    ) -> None:
        if type(boundary) is not TrustedRequestContextBoundary:
            raise TypeError("request boundary must use the canonical implementation")
        if type(authorized) is not AuthorizedRequest:
            raise TypeError("authorized request must use the canonical contract")
        if not isinstance(authorized.request_context, AuthorizedRequestContext):
            raise TypeError("authorized request context is invalid")
        self._boundary = boundary
        self._authorized = authorized

    def verify(
        self,
        context: AuthorizedRequestContext[DiagnosticContext],
        resolution: AuthorizationResolution,
    ) -> None:
        """Raise unless the exact #72 request is active and consistent."""

        try:
            if context is not self._authorized.request_context:
                raise ValueError
            self._boundary.verify(self._authorized.persistence_context)
            request_context = self._authorized.request_context
            if (
                request_context.acting_subject_id != resolution.acting_subject_id
                or request_context.scope != resolution.scope
                or request_context.roles != resolution.roles
                or request_context.authorization_generation
                != resolution.authorization_generation
                or request_context.authentication_configuration_version
                != resolution.authentication_configuration_version
            ):
                raise ValueError
        except Exception:
            raise ValueError("authorization context is invalid") from None


__all__ = ["RequestAgentAuthorizationVerifier"]
