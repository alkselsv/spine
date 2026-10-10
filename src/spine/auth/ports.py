"""Purpose-specific authentication and authorization-directory ports."""

from __future__ import annotations

from typing import Protocol, TypeVar

from spine.auth.contracts import (
    AccessTokenCredential,
    AuthenticatedAlias,
    AuthorizationResolution,
    RequestedAuthorizationScope,
    RouteAuthorizationPolicy,
)


DiagnosticContextT = TypeVar("DiagnosticContextT", contravariant=True)


class AuthenticationPort(Protocol[DiagnosticContextT]):
    """Authenticate one opaque access token without granting Spine authority."""

    async def authenticate(
        self,
        credentials: AccessTokenCredential,
        diagnostic_context: DiagnosticContextT,
    ) -> AuthenticatedAlias: ...


class AuthorizationDirectory(Protocol):
    """Resolve current authority for exactly one protected request."""

    async def resolve_request_authority(
        self,
        authenticated_alias: AuthenticatedAlias,
        requested_scope: RequestedAuthorizationScope,
        route_policy: RouteAuthorizationPolicy,
    ) -> AuthorizationResolution: ...


__all__ = ["AuthenticationPort", "AuthorizationDirectory"]
