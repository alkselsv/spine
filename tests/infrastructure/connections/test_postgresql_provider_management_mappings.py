import importlib
import inspect
from types import SimpleNamespace

import pytest

from spine.infrastructure.persistence.postgresql_mappings import metadata
from spine.infrastructure.connections.postgresql_provider_management import (
    provider_connection_approvals,
    provider_connection_versions,
)


def test_provider_connection_tables_are_owned_by_connection_adapter() -> None:
    assert provider_connection_approvals.metadata is not metadata
    assert provider_connection_versions.metadata is provider_connection_approvals.metadata
    assert "spine.provider_connection_versions" not in metadata.tables
    assert {
        "provider_kind",
        "capabilities",
        "health",
        "diagnostic_codes",
        "permitted_operations",
        "receipt_id",
        "valid_until",
        "approved_manifest_digest",
    }.issubset(provider_connection_versions.c.keys())
    assert {"valid_until", "approved_manifest_digest"}.issubset(
        provider_connection_approvals.c.keys()
    )


def test_yandex_binary_read_migration_exposes_only_exact_control_capability() -> None:
    migration = importlib.import_module(
        "migrations.versions.20270113_112_yandex_binary_read_authority"
    )
    upgrade = inspect.getsource(migration.upgrade)

    assert migration.down_revision == "20270112_111"
    assert "SECURITY DEFINER" in upgrade
    assert "session_user <> '{control_name}'" in upgrade
    assert "REVOKE ALL ON FUNCTION {signature} FROM PUBLIC" in upgrade
    assert "GRANT EXECUTE ON FUNCTION {signature} TO {control}" in upgrade
    assert "GRANT SELECT, INSERT" not in upgrade
    assert "p_manifest_digest <> '487360a49" in upgrade
    assert "p_recorded_at > clock_timestamp()" in upgrade
    assert "p_valid_until <= clock_timestamp()" in upgrade
    assert "current_record.version <> 1" in upgrade
    assert "current_record.capabilities <>" in upgrade


def test_yandex_binary_read_migration_requires_bootstrapped_control_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module(
        "migrations.versions.20270113_112_yandex_binary_read_authority"
    )
    monkeypatch.setattr(
        migration,
        "_settings",
        lambda: SimpleNamespace(control_role="spine_control"),
    )
    monkeypatch.setattr(migration, "_role_exists", lambda _role: False)

    with pytest.raises(RuntimeError, match="operator bootstrap"):
        migration.upgrade()
