from __future__ import annotations

import importlib

from spine.infrastructure.persistence.postgresql_mappings import requirements_artifact_versions


def test_requirements_artifact_versions_are_scoped_immutable_records() -> None:
    table = requirements_artifact_versions
    assert [column.name for column in table.primary_key.columns] == [
        "workspace_id", "environment_id", "artifact_id", "artifact_version"
    ]
    assert {
        "run_id", "work_item_id", "production_identity", "production_digest",
        "predecessor_version", "payload", "created_at",
    } <= set(column.name for column in table.columns)
    assert "uq_requirements_artifact_production" in {item.name for item in table.constraints}


def test_requirements_artifact_migration_follows_workflow_run_commands() -> None:
    migration = importlib.import_module(
        "migrations.versions.20261204_72_requirements_artifacts"
    )

    assert migration.revision == "20261204_72"
    assert migration.down_revision == "20261203_71"
