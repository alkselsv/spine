"""Executable traceability for the synthetic Gmail end-to-end qualification."""

from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "docs/qualification/issue-841-gmail-offline-qualification.json"
RUNBOOK_PATH = ROOT / "docs/runbooks/r2-gmail-offline-qualification.md"

SCENARIO_TESTS: dict[str, tuple[str, ...]] = {
    "initial_no_gap_snapshot_and_incremental_history": (
        "tests/application/test_gmail_initial_snapshot.py::test_captured_baseline_snapshot_and_history_drain_publish_one_terminal_cursor",
        "tests/application/test_gmail_initial_snapshot.py::test_concurrent_add_delete_and_label_change_are_each_applied_once",
        "tests/connectors/test_gmail_incremental.py::test_multi_page_history_commits_terminal_cursor_after_all_effect_receipts",
    ),
    "cursor_404_bounded_reconciliation": (
        "tests/application/test_gmail_history_cursor_recovery.py::test_history_404_preserves_checkpoint_and_requires_reconciliation",
        "tests/application/test_gmail_history_cursor_recovery.py::test_recovery_starts_one_idempotent_bounded_successor_from_old_checkpoint",
        "tests/application/test_gmail_initial_snapshot.py::test_stale_history_cursor_recovery_reuses_no_gap_snapshot_protocol",
    ),
    "labels_thread_evidence_and_content_revision": (
        "tests/application/test_gmail_message_state.py::test_label_changes_bind_exact_source_without_content_effects",
        "tests/application/test_gmail_message_state.py::test_thread_and_header_relationships_retain_distinct_authority",
        "tests/qualification/test_issue840_gmail_message_state.py::test_metadata_only_state_and_relationships_preserve_raw_revision",
    ),
    "replay_and_idempotent_effects": (
        "tests/application/test_gmail_initial_snapshot.py::test_crash_after_durable_boundary_replays_without_cursor_gap",
        "tests/connectors/test_gmail_incremental.py::test_duplicate_history_delivery_reuses_receipts_and_logical_effects",
        "tests/integration/postgresql/test_email_sync_durability.py::test_postgresql_gmail_snapshot_crash_replays_every_durable_boundary",
        "tests/integration/postgresql/test_email_sync_durability.py::test_postgresql_gmail_crash_after_canonical_acceptance_has_one_effect",
    ),
    "provider_outage_and_safe_diagnostics": (
        "tests/connectors/test_gmail_history.py::test_provider_errors_return_typed_safe_history_outcomes",
        "tests/connectors/test_gmail_history.py::test_untyped_provider_error_does_not_disclose_exception_text",
        "tests/connectors/test_gmail.py::test_google_error_details_are_sanitized_and_tokens_are_not_in_request",
        "tests/application/test_google_sync_diagnostics.py::test_google_sync_diagnostic_snapshot_contains_only_allowlisted_operational_data",
    ),
    "tenant_mailbox_label_and_authority_denial_before_disclosure": (
        "tests/connectors/test_gmail.py::test_inactive_or_mismatched_connection_never_reaches_gmail",
        "tests/connectors/test_gmail.py::test_missing_or_wrong_exact_mail_scope_never_reaches_gmail",
        "tests/connectors/test_gmail.py::test_raw_message_maps_invalid_provider_results_to_safe_outcomes",
        "tests/connectors/test_gmail.py::test_gmail_history_rejects_state_writer_for_another_mailbox_scope",
        "tests/application/test_gmail_message_state.py::test_wrong_workspace_cannot_record_message_state",
        "tests/application/test_gmail_initial_snapshot.py::test_stale_history_cursor_recovery_revocation_blocks_provider_effects",
    ),
    "stale_generation_and_cursor_fencing": (
        "tests/application/test_gmail_initial_snapshot.py::test_stale_generation_before_terminal_commit_requires_reconciliation",
        "tests/application/test_gmail_history_cursor_recovery.py::test_recovery_does_not_read_after_generation_change",
        "tests/connectors/test_gmail_incremental.py::test_stale_or_revoked_authority_holds_history_checkpoint",
    ),
    "google_drive_sheets_and_v1_v2_compatibility": (
        "tests/qualification/test_issue614_google_offline_qualification.py::test_issue614_traceability_matrix_covers_required_scenarios",
        "tests/qualification/test_issue614_google_offline_qualification.py::test_issue614_report_records_live_provider_prerequisites_and_metadata_only_policy",
        "tests/qualification/test_issue677_cross_provider_offline.py::test_issue677_matrix_points_to_existing_executable_evidence",
        "tests/application/test_connector_ingestion.py::test_google_v1_and_provider_v2_produce_equivalent_canonical_outcomes",
    ),
}

FORBIDDEN_REPORT_MARKERS = (
    "access_token",
    "refresh_token",
    "bearer ",
    "subject@example",
    "raw_mime",
    "provider_body",
    '"message_content"',
)


def _test_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def test_issue841_traceability_matrix_covers_required_scenarios() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

    assert report["schema_version"] == "issue-841-gmail-offline-qualification:v1"
    assert report["ticket"] == 841
    assert report["offline_result"] == "PASS"
    assert report["live_gmail_semantics"] == "NOT_VERIFIED"
    assert set(report["required_scenarios"]) == set(SCENARIO_TESTS)
    assert report["scenario_status"] == {
        scenario: "PASS" for scenario in SCENARIO_TESTS
    }
    assert report["traceability"] == {
        scenario: list(nodes) for scenario, nodes in SCENARIO_TESTS.items()
    }
    for node_id in (node for nodes in SCENARIO_TESTS.values() for node in nodes):
        path, test_name = node_id.split("::", 1)
        assert test_name in _test_names(ROOT / path), node_id


def test_issue841_report_pins_synthetic_fixture_adapter_and_profile_versions() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

    assert report["versions"] == {
        "fixture": "gmail-offline-fixtures:v1",
        "adapter": "gmail-connector-contracts:v1",
        "qualification_profile": "gmail-offline-qualification:v1",
    }
    assert report["fixture_policy"]["synthetic_only"] is True
    assert report["fixture_policy"]["network_access"] is False
    assert report["fixture_policy"]["credentials"] is False
    assert report["fixture_policy"]["provider_payloads"] is False
    assert report["privacy"] == {
        "tokens_recorded": False,
        "subjects_recorded": False,
        "addresses_recorded": False,
        "raw_content_recorded": False,
        "provider_bodies_recorded": False,
    }
    serialized = REPORT_PATH.read_text(encoding="utf-8").lower()
    assert not any(marker in serialized for marker in FORBIDDEN_REPORT_MARKERS)


def test_issue841_runbook_executes_every_offline_evidence_suite() -> None:
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    command = runbook.split("```bash\n", 1)[1].split("\n```", 1)[0]
    required_paths = {
        node.split("::", 1)[0]
        for nodes in SCENARIO_TESTS.values()
        for node in nodes
    }

    assert "uv run pytest -q" in command
    for path in sorted(required_paths):
        assert path in command
    assert "no network, Gmail/Yandex credentials" in runbook
    assert "NOT_VERIFIED" in runbook
