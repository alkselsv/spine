"""Executable traceability for the offline R2 Google connector qualification."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "docs/qualification/issue-614-google-offline-report.json"
RUNBOOK_PATH = ROOT / "docs/runbooks/r2-google-offline-qualification.md"

# Every scenario is tied to existing behavioral tests at the production seam.
# CI runs these files directly; this matrix prevents the qualification from
# silently losing coverage as test modules are renamed or removed.
SCENARIO_TESTS: dict[str, tuple[str, ...]] = {
    "drive_initial_snapshot_and_incremental_cursor": (
        "tests/application/test_google_drive_snapshot.py::test_initial_snapshot_commits_proposed_start_token_after_all_pages_are_durable",
        "tests/application/test_google_drive_changes.py::test_incremental_changes_drains_every_page_before_committing_terminal_cursor",
    ),
    "sheets_full_and_incremental_reconciliation": (
        "tests/application/test_google_sheets_reconciliation.py::test_reconcile_reads_every_configured_page_before_canonical_ingestion",
        "tests/application/test_google_sheets_reconciliation.py::test_drive_wakeup_reconciles_sheets_before_committing_drive_cursor",
    ),
    "changed_content_and_metadata_only_observation": (
        "tests/application/test_google_drive_reader.py::test_drive_operational_metadata_changes_keep_revision_identity_but_append_provenance",
        "tests/application/test_google_sheets_reader.py::test_sheets_location_changes_preserve_content_digest_and_append_locator_provenance",
        "tests/application/test_google_sheets_reader.py::test_sheets_revision_digest_changes_when_a_configured_representation_changes",
    ),
    "stable_row_identity_and_invalid_identity_quarantine": (
        "tests/application/test_google_sheets_reader.py::test_sheets_range_reader_quarantines_duplicate_business_ids",
        "tests/application/test_google_sheets_reconciliation.py::test_invalid_identity_scope_is_durably_quarantined_without_acceptance",
    ),
    "interruption_replay_and_idempotent_projection_effect": (
        "tests/application/test_google_drive_snapshot.py::test_interrupted_page_resumes_without_repeating_durable_items",
        "tests/application/test_google_drive_snapshot.py::test_replay_after_canonical_effect_before_item_checkpoint_reuses_stable_item_command",
        "tests/application/test_google_sheets_reconciliation.py::test_duplicate_wakeup_replays_one_durable_row_effect",
        "tests/application/test_connector_ingestion.py::test_handoff_uses_real_in_memory_source_admission_canonical_and_ledger_repositories",
    ),
    "composed_provider_to_persisted_canonical_outbox_path": (
        "tests/application/test_connector_ingestion.py::test_google_reconciliation_composes_fake_provider_through_persisted_canonical_outbox",
    ),
    "revocation_and_generation_fencing": (
        "tests/application/test_google_drive_snapshot.py::test_revoked_grant_suspends_snapshot_before_starting_source_ingestion",
        "tests/application/test_google_sheets_reconciliation.py::test_revoked_grant_suspends_full_scope_before_any_row_is_accepted",
        "tests/application/test_google_drive_changes.py::test_generation_change_fences_resumed_run_before_provider_call",
    ),
    "incomplete_pagination_and_partial_failure": (
        "tests/application/test_google_drive_snapshot.py::test_incomplete_search_requires_reconciliation_and_keeps_last_cursor",
        "tests/application/test_google_sheets_reconciliation.py::test_incomplete_late_page_holds_cursor_and_accepts_no_rows",
        "tests/application/test_google_sheets_reconciliation.py::test_retry_skips_durable_row_outcome_after_later_canonical_failure",
    ),
    "conservative_removal_tombstone_and_reappearance": (
        "tests/application/test_google_drive_lifecycle.py::test_trash_and_scope_exit_are_non_authoritative_by_default",
        "tests/application/test_google_drive_lifecycle.py::test_complete_authorized_scoped_absence_tombstones_once_and_replays",
        "tests/application/test_google_drive_lifecycle.py::test_drive_reappearance_uses_source_lifecycle_with_tombstone_lineage",
        "tests/application/test_google_sheets_lifecycle.py::test_incomplete_scan_cannot_create_candidate_or_tombstone",
        "tests/application/test_google_sheets_lifecycle.py::test_complete_authorized_absence_records_candidate_and_tombstones_once",
    ),
    "workspace_isolation_and_safe_diagnostics": (
        "tests/application/test_google_drive_lifecycle.py::test_postgresql_drive_source_reader_requires_matching_tenant_and_provenance",
        "tests/application/test_google_sheets_lifecycle.py::test_cross_workspace_source_inventory_cannot_create_removal_decision",
        "tests/application/test_google_sync_diagnostics.py::test_google_sync_diagnostic_snapshot_contains_only_allowlisted_operational_data",
        "tests/application/test_google_read_admission.py::test_denied_request_fails_before_provider_call",
    ),
    "revision_scoped_locators_and_association_lineage": (
        "tests/application/test_google_sheets_reader.py::test_sheets_location_changes_preserve_content_digest_and_append_locator_provenance",
        "tests/application/test_register_associations.py::test_accept_register_claim_replay_returns_same_revision_scoped_handoff",
        "tests/application/test_register_associations.py::test_extract_register_claims_from_effective_cells_is_revision_scoped_and_replayable",
    ),
    "canonical_acceptance_and_postgresql_contracts": (
        "tests/contracts/persistence/test_canonical_acceptance_contract.py::test_canonical_happy_path_and_exact_replay",
        "tests/application/persistence/test_canonical_acceptance.py::test_canonical_acceptance_advances_head_and_replays_without_second_effect",
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_adapter_satisfies_canonical_acceptance_contract",
    ),
    "migration_graph_and_upgrade_path": (
        "tests/integration/postgresql/test_initial_tenancy_migration.py::test_every_retained_revision_upgrades_to_one_final_head",
    ),
}


def _test_node_ids(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                names.add(node.name)
    return names


def test_issue614_traceability_matrix_covers_required_scenarios() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

    assert report["schema_version"] == "issue-614-google-offline:v1"
    assert report["ticket"] == 614
    assert set(report["required_scenarios"]) == set(SCENARIO_TESTS)
    assert report["traceability"] == {
        name: list(nodes) for name, nodes in SCENARIO_TESTS.items()
    }
    for node_id in (node for nodes in SCENARIO_TESTS.values() for node in nodes):
        path, test_name = node_id.split("::", 1)
        assert test_name in _test_node_ids(ROOT / path), node_id


def test_issue614_runbook_executes_the_traceability_suites() -> None:
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    command = re.search(r"```bash\n(.*?)\n```", runbook, flags=re.DOTALL)

    assert command is not None
    for path in sorted({node.split("::", 1)[0] for nodes in SCENARIO_TESTS.values() for node in nodes}):
        assert path in command.group(1)


def test_google_connector_adapters_do_not_call_cognee_or_change_its_pin() -> None:
    connector_root = ROOT / "src/spine/connectors"
    imports = []
    direct_remember_calls = []
    for path in connector_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imports.append(node.module)
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "remember":
                    if isinstance(func.value, ast.Name) and func.value.id == "cognee":
                        direct_remember_calls.append(path)

    assert not any(name == "cognee" or name.startswith("cognee.") for name in imports)
    assert direct_remember_calls == []

    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert '"cognee==1.5.4"' in project
    assert re.search(r'(?m)^name = "cognee"\nversion = "1\.5\.4"$', lock)


def test_issue614_report_records_live_provider_prerequisites_and_metadata_only_policy() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    serialized = REPORT_PATH.read_text(encoding="utf-8").lower()

    assert report["status"] == "offline_pass"
    assert report["verification"]["focused_result"] == "341 passed, 0 skipped"
    assert report["verification"]["composed_path_result"].startswith("1 passed;")
    assert report["verification"]["repository_result"] == "4451 passed, 6 skipped, 0 failed"
    assert report["verification"]["compileall_result"] == "passed"
    assert report["migration_checks"]["schema_changed_by_issue"] is False
    assert report["migration_checks"]["migration_required"] is False
    assert report["migration_checks"]["upgrade_graph_result"] == (
        "passed in focused qualification run"
    )
    assert report["live_provider_status"] == "not_verified"
    assert set(report["unverified_live_behaviors"]) == {
        "oauth_and_grant_rotation_against_google",
        "provider_quota_and_retry_behavior",
        "shared_drive_and_real_scope_semantics",
        "google_changes_feed_delivery_and_token_expiry",
        "google_sheets_range_pagination_and_concurrent_edits",
        "provider_specific_removed_trashed_and_404_meaning",
    }
    assert report["required_prerequisites"]
    assert report["privacy"] == {
        "credentials_recorded": False,
        "provider_payloads_recorded": False,
        "raw_errors_recorded": False,
        "source_content_recorded": False,
        "synthetic_fakes_only": True,
    }
    for forbidden in ("access_token", "bearer ", '"source_content":', '"raw_error":'):
        assert forbidden not in serialized
