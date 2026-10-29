"""Connections, sync cursors, and ingestion-run domain."""

from spine.domain.connections.google import (
    GoogleConnection,
    GoogleConnectionApproval,
    GoogleConnectionStatus,
    OpaqueGoogleReference,
)
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapability,
    ProviderCapabilityKind,
    ProviderCapabilityStatus,
    ProviderConnection,
    ProviderConnectionStatus,
    ProviderKind,
    google_v1_connection_to_v2,
)

__all__ = [
    "GoogleConnection",
    "GoogleConnectionApproval",
    "GoogleConnectionStatus",
    "OpaqueGoogleReference",
    "OpaqueProviderReference",
    "ProviderCapability",
    "ProviderCapabilityKind",
    "ProviderCapabilityStatus",
    "ProviderConnection",
    "ProviderConnectionStatus",
    "ProviderKind",
    "google_v1_connection_to_v2",
]
