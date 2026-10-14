from __future__ import annotations

import importlib

from spine.infrastructure.persistence.postgresql_mappings import context_profiles


def test_context_profile_mapping_contains_immutable_tenant_identity_and_restrictions() -> None:
    assert [column.name for column in context_profiles.primary_key.columns] == [
        "workspace_id",
        "environment_id",
        "profile_id",
        "profile_version",
    ]
    assert {
        "profile_digest",
        "retrieval_strategy",
        "max_results",
        "max_tokens",
        "max_relation_hops",
        "freshness_max_age_seconds",
        "completeness_requirement",
        "partial_answer_fallback",
        "server_approved",
        "revoked",
    } <= set(column.name for column in context_profiles.columns)


def test_context_profile_migration_follows_projection_publication_head() -> None:
    migration = importlib.import_module(
        "migrations.versions.20261023_27_context_profiles"
    )

    assert migration.revision == "20261023_27"
    assert migration.down_revision == "20261022_26"
