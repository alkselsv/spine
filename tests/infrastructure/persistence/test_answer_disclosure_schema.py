from __future__ import annotations

import importlib

from spine.infrastructure.persistence.postgresql_mappings import qa_answer_disclosure_records


def test_answer_disclosure_mapping_is_tenant_scoped_and_payload_backed() -> None:
    assert [column.name for column in qa_answer_disclosure_records.primary_key.columns] == [
        "workspace_id",
        "environment_id",
        "request_id",
    ]
    assert {
        "outcome",
        "payload",
        "created_at",
    } <= {column.name for column in qa_answer_disclosure_records.columns}


def test_answer_disclosure_migration_follows_revalidation_head() -> None:
    migration = importlib.import_module(
        "migrations.versions.20261028_33_answer_disclosure"
    )

    assert migration.revision == "20261028_33"
    assert migration.down_revision == "20261027_32"
