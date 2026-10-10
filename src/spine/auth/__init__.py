"""Authentication context and workspace-scoped authorization seams."""

from spine.auth.contracts import (
    AccessTokenCredential,
    AuthenticationAlias,
    AuthenticatedAlias,
    AuthorizationResolution,
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizedRequestContext,
    RequestedAuthorizationScope,
    RouteAuthorizationPolicy,
)
from spine.auth.errors import (
    AuthError,
    AuthFailureCategory,
    AuthFailureRetryability,
    AuthenticationUnavailableError,
    AuthorizationDeniedError,
    AuthorizationUnavailableError,
    InvalidAuthenticationError,
)
from spine.auth.in_memory import (
    AuthorizationRecordState,
    InMemoryAuthorizationDirectory,
    InMemoryAuthorizationEntry,
)
from spine.auth.ports import AuthenticationPort, AuthorizationDirectory
from spine.auth.registry import (
    DuplicateRoutePolicyError,
    InvalidRoutePolicyError,
    MissingRoutePolicyError,
    RoutePolicyRegistry,
    RoutePolicyRegistryError,
)

__all__ = [
    "AccessTokenCredential",
    "AuthError",
    "AuthFailureCategory",
    "AuthFailureRetryability",
    "AuthenticationAlias",
    "AuthenticatedAlias",
    "AuthenticationPort",
    "AuthenticationUnavailableError",
    "AuthorizationDeniedError",
    "AuthorizationDirectory",
    "AuthorizationResolution",
    "AuthorizationRole",
    "AuthorizationRecordState",
    "AuthorizationScopeRequirement",
    "AuthorizationUnavailableError",
    "AuthorizedRequestContext",
    "DuplicateRoutePolicyError",
    "InvalidRoutePolicyError",
    "RequestedAuthorizationScope",
    "RouteAuthorizationPolicy",
    "InvalidAuthenticationError",
    "InMemoryAuthorizationDirectory",
    "InMemoryAuthorizationEntry",
    "MissingRoutePolicyError",
    "RoutePolicyRegistry",
    "RoutePolicyRegistryError",
]
