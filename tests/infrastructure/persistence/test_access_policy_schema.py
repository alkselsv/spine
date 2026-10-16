from __future__ import annotations

import importlib


def test_access_policy_migration_declares_complete_ticket_82_ledger() -> None:
    migration = importlib.import_module(
        "migrations.versions.20261015_18_access_policy_persistence"
    )

    assert migration.POLICY_TABLES == (
        "access_policy_versions",
        "access_policy_grants",
        "policy_proposals",
        "policy_approvals",
        "policy_activations",
        "current_access_policies",
    )
    assert migration.revision == "20261015_18"
    assert migration.down_revision == "20261014_17"


def test_safe_policy_metadata_migration_adds_immutable_binding_head() -> None:
    migration = importlib.import_module(
        "migrations.versions.20261030_35_safe_policy_metadata"
    )

    assert migration.revision == "20261030_35"
    assert migration.down_revision == "20261029_34"
