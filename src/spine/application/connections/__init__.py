"""Application contracts for governed external connections."""

from spine.application.connections.google import (
    GoogleConnectionCommand,
    GoogleConnectionDeniedError,
    GoogleConnectionRepository,
    GoogleConnectionSuspensionError,
    GoogleProviderAuthorizationError,
    GoogleProviderNotFoundError,
    GoogleProviderQuotaError,
    GoogleProviderPolicyError,
    GoogleProviderReadError,
    GoogleReadAdmission,
    GoogleReadCommand,
)
from spine.application.connections.provider import (
    ProviderCapabilityUnavailableError,
    ProviderConnectionRepository,
    ProviderObservationReader,
    ProviderReadAdmission,
    ProviderReadCommand,
    ProviderReadDeniedError,
    ProviderReadError,
)

__all__ = [
    "GoogleConnectionCommand",
    "GoogleConnectionDeniedError",
    "GoogleConnectionRepository",
    "GoogleConnectionSuspensionError",
    "GoogleProviderAuthorizationError",
    "GoogleProviderNotFoundError",
    "GoogleProviderQuotaError",
    "GoogleProviderPolicyError",
    "GoogleProviderReadError",
    "GoogleReadAdmission",
    "GoogleReadCommand",
    "ProviderCapabilityUnavailableError",
    "ProviderConnectionRepository",
    "ProviderObservationReader",
    "ProviderReadAdmission",
    "ProviderReadCommand",
    "ProviderReadDeniedError",
    "ProviderReadError",
]
