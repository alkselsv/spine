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
