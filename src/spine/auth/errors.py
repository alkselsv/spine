"""Stable disclosure-safe failures exposed by the auth seam."""

from __future__ import annotations

from enum import Enum


class AuthFailureCategory(str, Enum):
    """Stable machine-readable categories owned by the auth seam."""

    AUTHENTICATION_INVALID = "authentication.invalid"
    AUTHENTICATION_UNAVAILABLE = "authentication.unavailable"
    AUTHORIZATION_DENIED = "authorization.denied"
    AUTHORIZATION_UNAVAILABLE = "authorization.unavailable"


class AuthFailureRetryability(str, Enum):
    """Retryability values compatible with the future Structured Error seam."""

    NEVER = "never"
    AFTER_DELAY = "after_delay"


class AuthError(Exception):
    """Base class for failures exposed by authentication and authorization."""

    category: AuthFailureCategory
    retryability: AuthFailureRetryability


class InvalidAuthenticationError(AuthError):
    """Presented credentials do not establish an authenticated alias."""

    category = AuthFailureCategory.AUTHENTICATION_INVALID
    retryability = AuthFailureRetryability.NEVER

    def __init__(self) -> None:
        super().__init__("Authentication is invalid.")


class AuthenticationUnavailableError(AuthError):
    """The configured authentication authority cannot currently decide."""

    category = AuthFailureCategory.AUTHENTICATION_UNAVAILABLE
    retryability = AuthFailureRetryability.AFTER_DELAY

    def __init__(self) -> None:
        super().__init__("Authentication is unavailable.")


class AuthorizationDeniedError(AuthError):
    """Current authority does not allow the requested protected operation."""

    category = AuthFailureCategory.AUTHORIZATION_DENIED
    retryability = AuthFailureRetryability.NEVER

    def __init__(self) -> None:
        super().__init__("Authorization is denied.")


class AuthorizationUnavailableError(AuthError):
    """Current authorization state cannot be resolved safely."""

    category = AuthFailureCategory.AUTHORIZATION_UNAVAILABLE
    retryability = AuthFailureRetryability.AFTER_DELAY

    def __init__(self) -> None:
        super().__init__("Authorization is unavailable.")


__all__ = [
    "AuthError",
    "AuthFailureCategory",
    "AuthFailureRetryability",
    "AuthenticationUnavailableError",
    "AuthorizationDeniedError",
    "AuthorizationUnavailableError",
    "InvalidAuthenticationError",
]
