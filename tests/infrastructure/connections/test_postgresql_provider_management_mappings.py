from spine.infrastructure.connections.postgresql_provider_management import (
    provider_connection_approvals,
    provider_connection_versions,
)
from spine.infrastructure.persistence.postgresql_mappings import metadata


def test_provider_connection_tables_are_owned_by_connection_adapter() -> None:
    assert provider_connection_approvals.metadata is not metadata
    assert provider_connection_versions.metadata is provider_connection_approvals.metadata
    assert "spine.provider_connection_versions" not in metadata.tables
    assert {
        "provider_kind",
        "capabilities",
        "health",
        "diagnostic_codes",
        "receipt_id",
    }.issubset(provider_connection_versions.c.keys())
