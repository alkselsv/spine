"""Offline evidence index for Shared Drive conformance and live preflight."""

from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs/runbooks/r2-google-shared-drive-offline.md"
REPORT = ROOT / "docs/qualification/issue-756-google-shared-drive-offline-report.json"

SCENARIOS: dict[str, tuple[str, ...]] = {
    "shared_drive_query_and_identity": (
        "tests/connectors/test_google_api.py::test_shared_drive_requests_bind_all_pages_to_exact_drive_identity",
        "tests/connectors/test_google_api.py::test_shared_drive_snapshot_rejects_cross_drive_item",
        "tests/connectors/test_google_api.py::test_shared_drive_metadata_rejects_allowlisted_id_from_another_drive",
        "tests/connectors/test_google_api.py::test_shared_drive_unlisted_file_fails_before_credentials_or_transport",
        "tests/connectors/test_google_api.py::test_shared_drive_permission_denial_is_typed_and_sanitized",
        "tests/connectors/test_google_api.py::test_shared_drive_changes_with_unknown_or_foreign_identity_are_incomplete",
        "tests/connectors/test_google_api.py::test_shared_drive_change_outside_configured_folder_is_incomplete",
        "tests/connectors/test_google_api.py::test_shared_drive_change_without_identity_is_incomplete",
        "tests/connectors/test_google_api.py::test_shared_drive_scope_does_not_expand_spreadsheet_allowlist",
    ),
    "mis_scope_and_zero_provider_calls": (
        "tests/connectors/test_google_api.py::test_out_of_scope_file_fails_before_credentials_or_transport",
        "tests/connectors/test_google_api.py::test_stale_runtime_scope_generation_fails_before_credentials_or_transport",
        "tests/connectors/test_google_api.py::test_shared_drive_scope_does_not_expand_spreadsheet_allowlist",
    ),
    "incomplete_pages_cancellation_and_cursor_safety": (
        "tests/application/test_google_drive_snapshot.py::test_incomplete_search_requires_reconciliation_and_keeps_last_cursor",
        "tests/application/test_google_drive_snapshot.py::test_cancellation_keeps_snapshot_cursor_and_resume_commits_once",
        "tests/application/test_google_drive_changes.py::test_incomplete_page_reconciles_without_advancing_cursor",
        "tests/application/test_google_drive_changes.py::test_cancellation_keeps_change_cursor_and_resume_commits_once",
        "tests/application/test_google_drive_changes.py::test_crash_after_canonical_effect_replays_stable_change_without_cursor_advance",
    ),
    "access_loss_and_sanitized_outcomes": (
        "tests/connectors/test_google_api.py::test_revoked_refresh_grant_maps_to_sanitized_authorization_outcome",
        "tests/connectors/test_google_api.py::test_provider_failures_are_typed_and_only_retryable_failures_retry",
        "tests/application/test_google_drive_changes.py::test_drive_access_outcomes_keep_cursor_and_enter_reconciliation",
    ),
    "live_preflight_refusal_and_offline_status": (
        "tests/qualification/test_google_live_harness.py::test_shared_drive_boolean_does_not_replace_external_preflight",
        "tests/qualification/test_google_live_harness.py::test_shared_drive_preflight_denies_missing_or_mismatched_binding_before_provider",
        "tests/qualification/test_google_live_harness.py::test_shared_drive_fake_run_keeps_status_not_exercised",
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


def test_issue756_scenario_matrix_points_to_offline_behaviors() -> None:
    for node_id in (node for scenario in SCENARIOS.values() for node in scenario):
        relative_path, test_name = node_id.split("::", 1)
        assert test_name in _test_names(ROOT / relative_path), node_id


def test_issue756_runbook_and_report_are_explicitly_offline() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8").lower()
    report_text = REPORT.read_text(encoding="utf-8")
    report = json.loads(report_text)

    assert "not_exercised" in runbook
    assert "zero provider calls" in runbook
    assert report["shared_drive_status"] == "NOT_EXERCISED"
    assert report["provider_calls"] == 0
    assert report["credentials_recorded"] is False
    assert report["resource_identifiers_recorded"] is False
    assert report["source_content_recorded"] is False
    assert report["raw_provider_errors_recorded"] is False
    for forbidden in (
        "synthetic-drive",
        "synthetic-file",
        "client-secret",
        "refresh-token",
    ):
        assert forbidden not in report_text.lower()
