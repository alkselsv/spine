from __future__ import annotations

import importlib


def test_requirements_review_command_migration_follows_artifact_versions() -> None:
    migration = importlib.import_module(
        "migrations.versions.20261205_73_requirements_review_commands"
    )

    assert migration.revision == "20261205_73"
    assert migration.down_revision == "20261204_72"
