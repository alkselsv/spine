"""Production authentication adapters and their process-owned lifecycle."""

from spine.infrastructure.auth.composition import (
    BoundRouteAuthorization,
    RequestAuthorizationComposer,
)
from spine.infrastructure.auth.oidc import (
    OIDCAuthenticationRuntime,
    OIDCReadiness,
    OIDCReadinessCode,
    OIDCReadinessStatus,
)
from spine.infrastructure.auth.settings import (
    InteractiveClientQualification,
    OIDCAuthenticationSettings,
    OIDCReadinessPolicy,
)
from spine.infrastructure.auth.trusted_context import (
    AuthorizedRequest,
    TrustedRequestContextBoundary,
)

__all__ = [
    "AuthorizedRequest",
    "BoundRouteAuthorization",
    "InteractiveClientQualification",
    "OIDCAuthenticationRuntime",
    "OIDCAuthenticationSettings",
    "OIDCReadiness",
    "OIDCReadinessCode",
    "OIDCReadinessPolicy",
    "OIDCReadinessStatus",
    "RequestAuthorizationComposer",
    "TrustedRequestContextBoundary",
]
