"""Executable traceability for issue #1036 CRM v2 offline qualification."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "docs/qualification/issue-1036-crm-v2-offline.json"

# #1036 qualifies already-implemented behavior through public seams. Exact node
# IDs make renamed or removed evidence fail instead of silently weakening the
# cross-slice matrix.
SCENARIO_TESTS: dict[str, tuple[str, ...]] = {
    "acting_human_and_exact_scope_admission": (
        "tests/application/test_crm_relationship_v2_admission.py::test_exact_current_scope_is_admitted_with_raw_entity_type_and_profile",
        "tests/application/test_crm_relationship_v2_admission.py::test_missing_or_invalid_acting_human_denies_before_provider_call",
        "tests/application/test_crm_relationship_v2_admission.py::test_service_principal_substitution_denies_before_provider_call",
        "tests/application/test_crm_relationship_v2_admission.py::test_wrong_exact_scope_denies_before_provider_call",
        "tests/application/test_crm_relationship_v2_admission.py::test_stale_or_ambiguous_profile_denies_before_provider_call",
    ),
    "binding_normalization_pagination_and_uncertainty": (
        "tests/connectors/test_bitrix24_crm_relationship_v2.py::test_exact_raw_pair_on_first_or_later_page_is_positive",
        "tests/connectors/test_bitrix24_crm_relationship_v2.py::test_raw_entity_pair_mismatch_is_not_positive",
        "tests/connectors/test_bitrix24_crm_relationship_v2.py::test_profile_with_unknown_normalizer_fails_closed_without_provider_call",
        "tests/connectors/test_bitrix24_crm_relationship_v2.py::test_filtered_absence_is_inaccessible",
        "tests/connectors/test_bitrix24_crm_relationship_v2.py::test_incomplete_page_without_cursor_is_not_classified_as_absence",
        "tests/connectors/test_bitrix24_crm_relationship_v2.py::test_page_limit_exhaustion_keeps_absence_uncertain",
        "tests/connectors/test_bitrix24_crm_relationship_v2.py::test_duplicate_pairs_are_typed_uncertainty",
    ),
    "files_item_isolation_and_namespace_collisions": (
        "tests/connectors/test_bitrix24_crm_files_v2.py::test_direct_mapping_preserves_each_exact_files_identity",
        "tests/connectors/test_bitrix24_crm_files_v2.py::test_malformed_item_becomes_uncertain_without_contaminating_valid_sibling",
        "tests/connectors/test_bitrix24_crm_files_v2.py::test_sibling_namespace_cannot_substitute_for_exact_files_item",
        "tests/connectors/test_bitrix24_crm_files_v2.py::test_files_and_disk_namespace_collision_fails_closed",
        "tests/connectors/test_bitrix24_crm_files_v2.py::test_namespace_mismatch_does_not_use_numeric_or_filename_fallback",
    ),
    "attached_ownership_and_strategy_separation": (
        "tests/connectors/test_bitrix24_crm_attached_object_v2.py::test_exact_ownership_resolves_namespaced_disk_identity",
        "tests/connectors/test_bitrix24_crm_attached_object_v2.py::test_wrong_or_missing_ownership_fields_fail_closed",
        "tests/connectors/test_bitrix24_crm_attached_object_v2.py::test_direct_strategy_never_calls_attached_object_port",
        "tests/connectors/test_bitrix24_crm_attached_object_v2.py::test_each_sibling_files_item_resolves_only_its_own_attached_id",
        "tests/connectors/test_bitrix24_crm_attached_object_v2.py::test_equal_file_and_disk_values_remain_separate_namespaces",
    ),
    "disk_revision_source_head_and_workspace_isolation": (
        "tests/application/test_crm_relationship_v2_source_match.py::test_exact_disk_revision_returns_fenced_current_source",
        "tests/application/test_crm_relationship_v2_source_match.py::test_changed_disk_revision_requires_its_own_match",
        "tests/application/test_crm_relationship_v2_source_match.py::test_stale_current_head_is_rejected_before_source_use",
        "tests/application/test_crm_relationship_v2_revalidation.py::test_changed_source_authorization_generation_is_stale_context",
        "tests/application/test_crm_relationship_v2_revalidation.py::test_foreign_workspace_target_is_denied_before_provider_use",
    ),
    "postgresql_atomicity_generation_cas_and_retry": (
        "tests/integration/postgresql/test_postgresql_crm_relationship_v2_handoff.py::test_verified_handoff_commits_append_head_audit_and_outbox_atomically",
        "tests/integration/postgresql/test_postgresql_crm_relationship_v2_handoff.py::test_verified_handoff_late_generation_is_rejected_without_effects",
        "tests/integration/postgresql/test_postgresql_crm_relationship_v2_handoff.py::test_verified_handoff_changed_t5_authority_is_rejected_atomically",
        "tests/integration/postgresql/test_postgresql_crm_relationship_v2_handoff.py::test_verified_handoff_concurrent_connection_successor_is_fenced",
        "tests/integration/postgresql/test_postgresql_crm_relationship_v2_handoff.py::test_verified_handoff_exact_retry_replays_and_changed_payload_conflicts",
        "tests/integration/postgresql/test_postgresql_crm_relationship_v2_handoff.py::test_verified_handoff_unknown_commit_reconciles_with_original_payload",
        "tests/integration/postgresql/test_postgresql_crm_relationship_v2_handoff.py::test_verified_handoff_concurrent_changed_payload_has_one_logical_effect",
        "tests/integration/postgresql/test_postgresql_crm_relationship_v2_handoff.py::test_verified_handoff_foreign_tenant_context_is_denied_before_write",
    ),
    "postgresql_v2_persistence_migration_and_lifecycle": (
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_crm_relationship_identity_registration_is_race_safe",
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_crm_relationship_identity_registration_rejects_persisted_key_collision",
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_crm_relationship_identity_registration_denies_foreign_tenant",
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_crm_relationship_profile_versions_are_immutable_and_replayable",
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_crm_relationship_claim_rejects_source_without_current_lifecycle_authority",
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_crm_relationship_withdrawal_and_audit_outbox_are_atomic",
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_crm_relationship_receipt_reapproval_cannot_resurrect_stale_evidence",
        "tests/integration/postgresql/test_initial_tenancy_migration.py::test_crm_relationship_v2_has_immutable_history_and_rollback_writer_guards",
    ),
    "withdrawal_late_results_and_no_resurrection": (
        "tests/application/test_crm_relationship_v2_revalidation.py::test_governance_withdrawal_short_circuits_positive_reads_and_cannot_resurrect",
        "tests/application/test_crm_relationship_v2_revalidation.py::test_late_positive_response_cannot_advance_a_new_verification_generation",
        "tests/application/test_crm_relationship_v2_revalidation.py::test_withdrawal_during_read_wins_over_positive_observations",
        "tests/application/test_crm_relationship_v2_revalidation.py::test_same_idempotency_key_is_stable_after_transient_read_failure",
    ),
    "sanitized_evidence_without_disclosure_or_pricing_authority": (
        "tests/application/test_crm_relationship_v2_revalidation.py::test_current_admission_denial_is_sanitized_and_typed",
        "tests/connectors/test_bitrix24_crm_attached_object_v2.py::test_partial_provider_failure_is_redacted_and_never_falls_back",
        "tests/connectors/test_bitrix24_crm_files_v2.py::test_files_field_shape_error_is_bounded_and_content_free",
        "tests/qualification/test_issue671_bitrix24_crm_material.py::test_issue671_accepted_handoff_has_no_disclosure_or_pricing_authority",
    ),
    "legacy_v1_replay_and_live_operation_fence": (
        "tests/infrastructure/persistence/test_postgresql_bitrix24_crm_material_fence.py::test_historical_claim_read_preserves_persisted_v1_record",
        "tests/infrastructure/persistence/test_postgresql_bitrix24_crm_material_fence.py::test_historical_handoff_read_preserves_acceptance_and_revocation",
        "tests/connectors/test_bitrix24_crm_material.py::test_legacy_crm_reader_denies_before_provider_access_without_acting_human",
        "tests/application/test_bitrix24_crm_material_service.py::test_legacy_observation_denies_before_repository_access",
        "tests/application/test_bitrix24_crm_material_service.py::test_legacy_acceptance_denies_before_repository_access",
        "tests/application/test_bitrix24_crm_material_service.py::test_legacy_revocation_denies_before_repository_access",
    ),
}


def _test_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def test_issue1036_report_traces_every_scenario_to_executable_evidence() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

    assert report["schema_version"] == "issue-1036-crm-v2-offline:v1"
    assert report["ticket"] == 1036
    assert report["parent_ticket"] == 717
    assert set(report["required_scenarios"]) == set(SCENARIO_TESTS)
    assert report["traceability"] == {
        scenario: list(node_ids) for scenario, node_ids in SCENARIO_TESTS.items()
    }
    for node_id in (
        node_id for node_ids in SCENARIO_TESTS.values() for node_id in node_ids
    ):
        relative_path, test_name = node_id.split("::", 1)
        assert test_name in _test_names(ROOT / relative_path), node_id


def test_issue1036_report_records_exact_offline_verification() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

    assert report["status"] == "offline_pass"
    assert re.fullmatch(r"[0-9a-f]{40}", report["tested_sha"])
    assert report["tested_sha"] == "0970131f482edf627d7e5cf3a598d7ea41a72f51"
    assert report["qualification_harness_sha"] == (
        "db06251815a9c51885e0d16d490fb8bbe4b9884f"
    )
    assert report["verification"]["focused"]["passed"] > 0
    assert report["verification"]["postgresql"] == {
        "command": "uv run pytest -q tests/integration/postgresql/test_postgresql_crm_relationship_v2_handoff.py",
        "passed": 12,
        "skipped": 0,
        "failed": 0,
    }
    assert report["verification"]["postgresql_v2_contract"] == {
        "command": "uv run pytest -q tests/integration/postgresql/test_postgresql_unit_of_work.py::test_crm_relationship_identity_registration_is_race_safe tests/integration/postgresql/test_postgresql_unit_of_work.py::test_crm_relationship_identity_registration_rejects_persisted_key_collision tests/integration/postgresql/test_postgresql_unit_of_work.py::test_crm_relationship_identity_registration_denies_foreign_tenant tests/integration/postgresql/test_postgresql_unit_of_work.py::test_crm_relationship_profile_versions_are_immutable_and_replayable tests/integration/postgresql/test_postgresql_unit_of_work.py::test_crm_relationship_claim_rejects_source_without_current_lifecycle_authority tests/integration/postgresql/test_postgresql_unit_of_work.py::test_crm_relationship_withdrawal_and_audit_outbox_are_atomic tests/integration/postgresql/test_postgresql_unit_of_work.py::test_crm_relationship_receipt_reapproval_cannot_resurrect_stale_evidence tests/integration/postgresql/test_initial_tenancy_migration.py::test_crm_relationship_v2_has_immutable_history_and_rollback_writer_guards",
        "passed": 8,
        "skipped": 0,
        "failed": 0,
    }
    assert report["verification"]["compileall"] == {
        "command": "uv run python -m compileall -q src tests",
        "status": "passed",
    }
    assert report["verification"]["repository"] == {
        "command": "uv run pytest -q",
        "passed": 7677,
        "skipped": 7,
        "failed": 0,
    }
    assert report["live_provider_status"] == "not_verified"
    assert report["production_activation_status"] == "not_approved"
    assert report["atomic_provider_snapshot_status"] == "not_claimed"
    assert report["unverified_provider_assumptions"]


def test_issue1036_report_contains_only_synthetic_sanitized_evidence() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    serialized = REPORT_PATH.read_text(encoding="utf-8").lower()

    assert report["privacy"] == {
        "synthetic_fakes_only": True,
        "credentials_recorded": False,
        "provider_payloads_recorded": False,
        "signed_urls_recorded": False,
        "production_data_recorded": False,
    }
    assert report["authority_boundaries"] == {
        "relationship_grants_disclosure": False,
        "relationship_grants_pricing_basis": False,
        "crm_code_writes_source_head": False,
        "v1_live_operations_enabled": False,
    }
    for forbidden in (
        "access_token",
        "refresh_token",
        "bearer ",
        "client_secret",
        "https://",
        "?token=",
        "access-secret",
        "refresh-secret",
    ):
        assert forbidden not in serialized
