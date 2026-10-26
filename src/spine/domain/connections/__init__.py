"""Connections, sync cursors, and ingestion-run domain."""

from spine.domain.connections.google import (
    GoogleConnection,
    GoogleConnectionStatus,
    OpaqueGoogleReference,
)

__all__ = [
    "GoogleConnection",
    "GoogleConnectionStatus",
    "OpaqueGoogleReference",
]
