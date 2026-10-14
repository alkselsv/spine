from __future__ import annotations

import importlib

from spine.infrastructure.persistence.postgresql_mappings import context_bundles, context_profiles


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


def test_context_bundle_mapping_contains_attempt_lineage_and_safe_references() -> None:
    assert [column.name for column in context_bundles.primary_key.columns] == [
        "workspace_id",
        "environment_id",
        "bundle_id",
        "bundle_version",
    ]
    assert {
        "bundle_digest",
        "idempotency_key",
        "profile_id",
        "profile_version",
        "profile_digest",
        "trusted_request_context_ref",
        "authorization_context_ref",
        "projection_snapshot_id",
        "projection_activation_id",
        "activation_generation",
        "canonical_boundary_id",
        "projection_config_version_id",
        "freshness",
        "health",
        "reconciliation_evidence",
        "evidence_references",
        "policy_decision_generations",
        "outcome",
        "state",
    } <= set(column.name for column in context_bundles.columns)


def test_context_bundle_migration_follows_context_profile_head() -> None:
    migration = importlib.import_module(
        "migrations.versions.20261024_28_context_bundles"
    )

    assert migration.revision == "20261024_28"
    assert migration.down_revision == "20261023_27"
