from __future__ import annotations

from spine.infrastructure.connections.postgresql_google import (
    google_connection_approvals,
    google_connection_versions,
)
from spine.infrastructure.persistence.postgresql_mappings import metadata


def test_google_connection_mappings_are_owned_by_their_migration() -> None:
    assert google_connection_approvals.metadata is not metadata
    assert google_connection_versions.metadata is not metadata
    assert "spine.google_connection_approvals" not in metadata.tables
    assert "spine.google_connection_versions" not in metadata.tables
