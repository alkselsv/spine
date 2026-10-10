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
from spine.auth.records import (
    AuthenticationAliasBinding,
    AuthorizationDirectoryRecords,
    AuthorizationGeneration,
    AuthorizationRecordStatus,
    CanonicalHumanIdentity,
    CurrentAuthorizationSnapshot,
    EnvironmentMembership,
    EnvironmentOwnership,
    EnvironmentRoleBinding,
    WorkspaceMembership,
)
from spine.auth.repositories import AuthorizationSnapshotRepository
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
    "AuthenticationAliasBinding",
    "AuthenticatedAlias",
    "AuthenticationPort",
    "AuthenticationUnavailableError",
    "AuthorizationDeniedError",
    "AuthorizationDirectory",
    "AuthorizationDirectoryRecords",
    "AuthorizationGeneration",
    "AuthorizationRecordStatus",
    "AuthorizationResolution",
    "AuthorizationRole",
    "AuthorizationRecordState",
    "AuthorizationScopeRequirement",
    "AuthorizationSnapshotRepository",
    "AuthorizationUnavailableError",
    "AuthorizedRequestContext",
    "CanonicalHumanIdentity",
    "CurrentAuthorizationSnapshot",
    "DuplicateRoutePolicyError",
    "EnvironmentMembership",
    "EnvironmentOwnership",
    "EnvironmentRoleBinding",
    "InvalidRoutePolicyError",
    "RequestedAuthorizationScope",
    "RouteAuthorizationPolicy",
    "InvalidAuthenticationError",
    "InMemoryAuthorizationDirectory",
    "InMemoryAuthorizationEntry",
    "MissingRoutePolicyError",
    "RoutePolicyRegistry",
    "RoutePolicyRegistryError",
    "WorkspaceMembership",
]
