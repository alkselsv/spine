from __future__ import annotations

import importlib
import inspect

from spine.infrastructure.persistence.postgresql_mappings import (
    bitrix24_disk_content_policy_versions,
    bitrix24_disk_root_policy_versions,
    current_bitrix24_disk_content_policies,
    current_bitrix24_disk_root_policies,
)


def test_bitrix24_disk_policy_migration_is_additive_and_tenant_scoped() -> None:
    migration = importlib.import_module(
        "migrations.versions.20261221_89_bitrix24_disk_root_policies"
    )
    assert migration.revision == "20261221_89"
    assert migration.down_revision == "20261220_88"
    source = inspect.getsource(migration)
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "BEFORE UPDATE OR DELETE" in source
    assert "Policy current-head CAS failed" in source
    assert "fk_b24_disk_root_content_policy" in source


def test_bitrix24_disk_policy_tables_keep_immutable_versions_and_current_heads() -> None:
    for table in (bitrix24_disk_content_policy_versions, bitrix24_disk_root_policy_versions):
        assert {"workspace_id", "environment_id", "policy_id", "version", "digest", "record"} <= {
            column.name for column in table.columns
        }
        assert any(constraint.name and constraint.name.startswith("uq_b24_disk_") for constraint in table.constraints)
    for table in (current_bitrix24_disk_content_policies, current_bitrix24_disk_root_policies):
        assert {"workspace_id", "environment_id", "policy_id", "current_version", "current_digest", "predecessor_version", "predecessor_digest", "generation"} <= {
            column.name for column in table.columns
        }
