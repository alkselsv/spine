"""Executable traceability for issue #729 offline source-admission qualification."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "docs/qualification/issue-729-bitrix24-source-admission.json"
RUNBOOK_PATH = ROOT / "docs/runbooks/r2-bitrix24-source-admission-qualification.md"

# #729 coordinates already-implemented behavior at public seams. Keeping exact
# node IDs here makes renamed or removed evidence fail instead of silently
# weakening the offline qualification.
SCENARIO_TESTS: dict[str, tuple[str, ...]] = {
    "root_scope_membership_and_workspace_isolation": (
        "tests/application/test_bitrix24_disk_scope_admission.py::test_admission_resolves_exact_current_tenant_scoped_policy_versions",
        "tests/application/test_bitrix24_disk_scope_admission.py::test_admission_cannot_resolve_policy_from_another_workspace",
        "tests/application/test_bitrix24_disk_scope_admission.py::test_admission_denies_empty_content_allowlist",
        "tests/connectors/test_bitrix24_disk_traversal.py::test_nested_membership_requires_matching_parent_chain_and_independent_discovery",
    ),
    "incomplete_scan_and_stale_authority_fencing": (
        "tests/connectors/test_bitrix24_disk_traversal.py::test_incomplete_listing_does_not_prove_non_membership",
        "tests/connectors/test_bitrix24_disk_traversal.py::test_permission_filtered_provider_failure_stays_incomplete",
        "tests/application/test_bitrix24_disk_scope_admission.py::test_admission_fences_superseded_root_policy",
        "tests/application/test_bitrix24_disk_scope_admission.py::test_traversal_coordinator_rechecks_scope_and_s1_grant_after_scan",
    ),
    "trusted_verifier_exact_receipt_and_failure": (
        "tests/application/storage/test_trusted_content.py::test_verifier_accepts_qualified_utf8_content_and_binds_receipt",
        "tests/application/storage/test_trusted_content.py::test_verifier_denies_substituted_association_before_reading",
        "tests/application/storage/test_trusted_content.py::test_verifier_returns_safe_typed_failure_for_untrusted_content",
        "tests/application/storage/test_trusted_content.py::test_verifier_denies_bytes_that_do_not_match_finalized_receipt",
    ),
    "selector_conflicts_and_compatibility": (
        "tests/application/test_canonical_acceptance_selectors.py::test_resolver_returns_explicit_google_v1_compatibility_selector",
        "tests/application/test_canonical_acceptance_selectors.py::test_legacy_provider_requires_explicit_v1_compatibility_selector",
        "tests/application/test_canonical_acceptance_selectors.py::test_bitrix24_s2_uses_server_selected_v2_and_rejects_v1_downgrade",
        "tests/application/test_canonical_acceptance_selectors.py::test_resolver_denies_overlapping_authority_with_conflicting_policy",
        "tests/application/test_canonical_acceptance_selectors.py::test_resolver_denies_current_head_mismatch",
    ),
    "canonical_retry_idempotency_and_unknown_commit": (
        "tests/application/persistence/test_canonical_acceptance_retry.py::test_serialization_retry_reopens_uow_and_returns_one_result",
        "tests/application/persistence/test_canonical_acceptance_retry.py::test_unknown_commit_reconciles_with_same_command_identity",
        "tests/application/persistence/test_canonical_acceptance_retry.py::test_v2_processing_authority_is_revalidated_inside_each_uow",
    ),
    "postgresql_constraints_rls_and_immutable_history": (
        "tests/integration/postgresql/test_bitrix24_disk_policy_persistence.py::test_postgresql_policy_heads_are_tenant_scoped_immutable_and_cas_fenced",
        "tests/integration/postgresql/test_initial_tenancy_migration.py::test_initial_schema_has_named_tenancy_constraints_and_postgresql_types",
        "tests/integration/postgresql/test_initial_tenancy_migration.py::test_governed_reprocessing_catalog_has_exact_tenant_foreign_keys",
        "tests/integration/postgresql/test_initial_tenancy_migration.py::test_processing_admission_and_selector_history_are_immutable",
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_selector_postgresql_publish_replays_and_serializes_cas",
    ),
    "migration_upgrade_rollback_and_version_interpretation": (
        "tests/integration/postgresql/test_initial_tenancy_migration.py::test_every_retained_revision_upgrades_to_one_final_head",
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_selector_migration_rollback_preserves_existing_v1_acceptance",
        "tests/domain/sources/test_processing_admission.py::test_canonical_acceptance_v2_requires_exact_processing_link",
        "tests/domain/sources/test_processing_admission.py::test_canonical_acceptance_v1_cannot_fabricate_processing_link",
    ),
    "lock_order_and_revocation_acceptance_races": (
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_policy_change_wins_before_acceptance_and_stale_evidence_fails_closed",
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_acceptance_locks_authoritative_policy_until_commit",
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_admission_change_wins_before_acceptance_and_fails_closed",
    ),
    "opaque_exclusion_and_no_old_current_fallback": (
        "tests/application/test_processing_commands.py::test_opaque_reprocess_is_rejected_before_item_parser_or_byte_read",
        "tests/application/test_projection_request_resolution.py::test_opaque_current_revision_is_rejected_before_physical_projection",
        "tests/application/test_projection_request_execution.py::test_current_turning_opaque_after_resolution_never_reaches_projection_adapter",
        "tests/application/test_projection_request_resolution.py::test_opaque_boundary_member_is_excluded_from_projection_command",
        "tests/application/test_authorized_context_broker.py::test_replay_is_denied_when_cached_revision_is_now_opaque",
        "tests/application/test_disclosure_revalidation.py::test_opaque_current_dependency_suppresses_cached_answer",
    ),
    "process_only_reprocessing_preserves_canonical_state": (
        "tests/application/test_processing_commands.py::test_authorized_opaque_reprocess_pins_exact_process_only_lineage",
        "tests/application/test_processing_commands.py::test_ordinary_reprocess_preserves_ordinary_admission_and_process_only_output",
        "tests/application/test_processing_commands.py::test_reprocess_accepts_historical_revision_without_moving_current_head_or_publication",
        "tests/application/test_projection_request_resolution.py::test_process_only_receipt_is_rejected_before_physical_projection",
    ),
    "provider_compatibility_regression_gates": (
        "tests/qualification/test_issue598_google_qualification.py::test_issue598_report_records_offline_pass_results",
        "tests/qualification/test_issue665_yandex_disk.py::test_issue665_traceability_points_to_executable_public_seams",
        "tests/qualification/test_issue680_mounted_filesystem.py::test_missing_live_prerequisites_are_not_verified",
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


def test_issue729_report_traces_every_required_scenario_to_executable_evidence() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

    assert report["schema_version"] == "issue-729-bitrix24-source-admission:v1"
    assert report["ticket"] == 729
    assert set(report["required_scenarios"]) == set(SCENARIO_TESTS)
    assert report["traceability"] == {
        scenario: list(node_ids) for scenario, node_ids in SCENARIO_TESTS.items()
    }
    for node_id in (
        node_id for node_ids in SCENARIO_TESTS.values() for node_id in node_ids
    ):
        relative_path, test_name = node_id.split("::", 1)
        assert test_name in _test_names(ROOT / relative_path), node_id


def test_issue729_runbook_executes_every_traceability_suite() -> None:
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    command = re.search(r"```bash\n(.*?)\n```", runbook, flags=re.DOTALL)

    assert command is not None
    evidence_paths = {
        node_id.split("::", 1)[0]
        for node_ids in SCENARIO_TESTS.values()
        for node_id in node_ids
    }
    for path in evidence_paths:
        assert path in command.group(1), path
    assert "uv run pytest -q" in command.group(1)
    assert "uv run python -m compileall -q src tests" in command.group(1)


def test_issue729_report_is_offline_only_and_records_exact_verification() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

    assert report["status"] == "offline_pass"
    assert re.fullmatch(r"[0-9a-f]{40}", report["qualified_base_commit"])
    assert re.fullmatch(r"[0-9a-f]{40}", report["qualification_commit"])
    assert report["migration_revision"] == "20261230_98"
    assert {
        key: {
            count: report["verification"][key][count]
            for count in ("passed", "skipped", "failed")
        }
        for key in ("focused", "postgresql", "repository")
    } == {
        "focused": {"passed": 144, "skipped": 0, "failed": 0},
        "postgresql": {"passed": 10, "skipped": 0, "failed": 0},
        "repository": {"passed": 6286, "skipped": 7, "failed": 0},
    }
    evidence_commands = "\n".join(
        report["verification"][key]["command"]
        for key in ("focused", "postgresql")
    )
    assert "<" not in evidence_commands
    for node_ids in SCENARIO_TESTS.values():
        for node_id in node_ids:
            path, _ = node_id.split("::", 1)
            assert path in evidence_commands
    assert report["verification"]["compileall"] == "passed"
    assert report["live_provider_status"] == "not_verified"
    assert report["production_activation_status"] == "not_verified"
    assert report["unverified_prerequisites"]


def test_issue729_report_contains_only_synthetic_content_safe_evidence() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    serialized = REPORT_PATH.read_text(encoding="utf-8").lower()

    assert report["privacy"] == {
        "synthetic_fakes_only": True,
        "credentials_recorded": False,
        "provider_payloads_recorded": False,
        "production_data_recorded": False,
        "opaque_content_recorded": False,
    }
    for forbidden in ("access_token", "refresh_token", "bearer ", "signed_url"):
        assert forbidden not in serialized
