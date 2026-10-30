"""Executable offline evidence index for issue #741."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs/runbooks/r2-yandex-qualification-api.md"

SCENARIOS: dict[str, tuple[str, ...]] = {
    "identity_preflight": (
        (
            "tests/connectors/test_yandex_disk_api.py::"
            "test_list_resources_verifies_identity_before_disk_and_maps_shape"
        ),
        (
            "tests/connectors/test_yandex_disk_api.py::"
            "test_identity_mismatch_fails_closed_before_disk_request"
        ),
    ),
    "zero_call_scope_denial": (
        (
            "tests/connectors/test_yandex_disk_api.py::"
            "test_out_of_scope_path_is_denied_before_token_resolution"
        ),
        (
            "tests/connectors/test_yandex_disk_api.py::"
            "test_connection_mismatch_is_denied_before_token_resolution"
        ),
    ),
    "bounded_download": (
        (
            "tests/connectors/test_yandex_disk_api.py::"
            "test_download_approved_https_host_enforces_byte_limit"
        ),
        (
            "tests/connectors/test_yandex_disk_api.py::"
            "test_download_rejects_unapproved_signed_url_without_fetching_it"
        ),
        (
            "tests/connectors/test_yandex_disk_api.py::"
            "test_httpx_transport_stops_stream_after_approved_response_budget"
        ),
    ),
    "sanitized_failures_and_ops_profile": (
        (
            "tests/connectors/test_yandex_disk_api.py::"
            "test_provider_status_maps_to_content_free_port_error"
        ),
        (
            "tests/connectors/test_yandex_disk_api.py::"
            "test_qualification_request_policy_is_fixed_to_approved_ops_budget"
        ),
        (
            "tests/connectors/test_yandex_disk_api.py::"
            "test_qualification_composition_pins_reader_to_approved_ops_budget"
        ),
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


def test_issue741_traceability_points_to_existing_offline_behaviors() -> None:
    for node_id in (node for scenario in SCENARIOS.values() for node in scenario):
        relative_path, test_name = node_id.split("::", 1)
        assert test_name in _test_names(ROOT / relative_path), node_id


def test_issue741_runbook_keeps_live_identity_and_calls_unverified() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    lowered = runbook.lower()

    assert "/Spine-Qualification-678-v1" in runbook
    assert "spine-yandex-disk-qualification-ops:v1" in runbook
    assert "not_verified" in lowered
    assert "no live call" in lowered
    assert "expected yandex user id" in lowered
