from __future__ import annotations

import importlib
import inspect

from spine.infrastructure.persistence.postgresql_mappings import (
    current_trusted_canonical_acceptance_selectors,
    trusted_canonical_acceptance_selector_versions,
)


def test_selector_migration_is_additive_tenant_scoped_and_immutable() -> None:
    migration = importlib.import_module(
        "migrations.versions.20261226_94_canonical_acceptance_selectors"
    )
    assert migration.revision == "20261226_94"
    assert migration.down_revision == "20261225_93"
    source = inspect.getsource(migration)
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "BEFORE UPDATE OR DELETE" in source
    assert "guard_current_trusted_canonical_acceptance_selector_step" in source
    assert "fk_trusted_canonical_acceptance_selector_root_policy" in source
    assert "fk_trusted_canonical_acceptance_selector_content_policy" in source


def test_selector_schema_separates_immutable_history_from_mutable_cas_head() -> None:
    assert {
        "selector_id", "selector_series_id", "selector_version", "record",
        "workspace_id", "environment_id", "idempotency_identity",
        "approval_reference", "audit_reference",
    } <= {column.name for column in trusted_canonical_acceptance_selector_versions.columns}
    assert {
        "workspace_id", "environment_id", "selector_series_id",
        "current_selector_id", "cas_version",
    } <= {column.name for column in current_trusted_canonical_acceptance_selectors.columns}
    assert any(
        constraint.name == "fk_current_trusted_canonical_acceptance_selector"
        for constraint in current_trusted_canonical_acceptance_selectors.foreign_key_constraints
    )
