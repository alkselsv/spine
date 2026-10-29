"""Executable offline evidence index for issue #652."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs/runbooks/r2-google-api-adapters.md"

SCENARIOS: dict[str, tuple[str, ...]] = {
    "drive_metadata_download_export": (
        "tests/connectors/test_google_api.py::test_drive_metadata_resolves_opaque_credentials_and_maps_scoped_request",
        "tests/connectors/test_google_api.py::test_drive_download_and_export_preserve_bytes_and_media_type",
    ),
    "drive_snapshot_changes_pagination": (
        "tests/connectors/test_google_api.py::test_drive_snapshot_and_changes_map_every_provider_page",
    ),
    "sheets_representations_metadata_pagination": (
        "tests/connectors/test_google_api.py::test_sheets_range_page_preserves_representations_metadata_and_pagination",
        "tests/connectors/test_google_api.py::test_sparse_sheets_page_uses_configured_range_for_continuation",
        "tests/connectors/test_google_api.py::test_sheets_grid_start_outside_requested_page_fails_closed",
        "tests/connectors/test_google_api.py::test_sheets_misscoped_response_fails_closed_without_returning_rows",
    ),
    "oauth_refresh_revocation_and_reauthorization": (
        "tests/connectors/test_google_api.py::test_reference_resolver_keeps_client_and_grant_behind_opaque_references",
        "tests/connectors/test_google_api.py::test_revoked_refresh_grant_maps_to_sanitized_authorization_outcome",
        "tests/connectors/test_google_api.py::test_provider_unauthorized_invalidates_token_and_refreshes_once",
    ),
    "allowlist_and_generation_zero_call_denial": (
        "tests/connectors/test_google_composition.py::test_production_composition_denies_before_credentials_and_transport",
        "tests/connectors/test_google_api.py::test_out_of_scope_file_fails_before_credentials_or_transport",
        "tests/connectors/test_google_api.py::test_stale_runtime_scope_generation_fails_before_credentials_or_transport",
        "tests/connectors/test_google_api.py::test_cross_workspace_connection_is_denied_before_credentials_or_http",
        "tests/application/test_google_read_admission.py::test_denied_request_fails_before_provider_call",
    ),
    "bounded_retry_timeout_and_sanitized_transport": (
        "tests/connectors/test_google_api.py::test_transient_provider_failure_retries_with_configured_backoff",
        "tests/connectors/test_google_api.py::test_provider_timeout_retries_only_to_explicit_attempt_limit",
        "tests/connectors/test_google_api.py::test_httpx_transport_maps_request_without_exposing_raw_request_errors",
        "tests/connectors/test_google_api.py::test_provider_failures_are_typed_and_only_retryable_failures_retry",
    ),
    "default_refusing_sanitized_live_harness": (
        "tests/qualification/test_google_live_harness.py::test_live_harness_refuses_before_provider_calls_without_double_opt_in",
        "tests/qualification/test_google_live_harness.py::test_live_harness_writes_only_sanitized_versioned_evidence",
        "tests/qualification/test_google_live_harness.py::test_live_config_rejects_targets_that_do_not_equal_exact_allowlist",
        "tests/qualification/test_google_live_harness.py::test_live_harness_rejects_noncurrent_connection_before_provider_calls",
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


def test_issue652_traceability_points_to_existing_offline_behaviors() -> None:
    for node_id in (node for scenario in SCENARIOS.values() for node in scenario):
        relative_path, test_name = node_id.split("::", 1)
        assert test_name in _test_names(ROOT / relative_path), node_id


def test_issue652_runbook_records_safe_configuration_and_unverified_live_scope() -> (
    None
):
    runbook = RUNBOOK.read_text(encoding="utf-8")
    lowered = runbook.lower()

    assert "httpx>=0.27,<1" in runbook
    assert "google-api-python-client" not in lowered
    assert "spine-google-live-qualification" in runbook
    assert "not_verified" in lowered
    assert "shared drive remains `not_exercised`" in lowered
    assert "cognee" not in lowered
