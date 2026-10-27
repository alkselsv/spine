"""Application contracts for governed external connections."""

from spine.application.connections.google import (
    GoogleConnectionCommand,
    GoogleConnectionDeniedError,
    GoogleConnectionRepository,
    GoogleProviderAuthorizationError,
    GoogleProviderQuotaError,
    GoogleProviderPolicyError,
    GoogleProviderReadError,
    GoogleReadAdmission,
    GoogleReadCommand,
)

__all__ = [
    "GoogleConnectionCommand",
    "GoogleConnectionDeniedError",
    "GoogleConnectionRepository",
    "GoogleProviderAuthorizationError",
    "GoogleProviderQuotaError",
    "GoogleProviderPolicyError",
    "GoogleProviderReadError",
    "GoogleReadAdmission",
    "GoogleReadCommand",
]
