from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).parents[2]
REPORT_PATH = ROOT / "docs/qualification/issue-422-r1-chat-runs-report.json"
REQUIRED_PATHS = {
    "session_reload",
    "ask_canonical_outcome",
    "persisted_sse_replay",
    "cancellation",
    "clarification_retry",
    "citation_revocation",
    "feedback",
    "runs_attempts_navigation",
    "two_workspace_isolation",
    "stale_state_and_safe_diagnostics",
    "accessibility_localization_responsive",
    "agno_halaska_mapping",
    "production_api_integration",
    "external_provider_qualification",
}


def _load_report() -> dict[str, Any]:
    value = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_issue422_report_covers_every_required_path_with_resolvable_evidence() -> None:
    report = _load_report()

    assert report["schema_version"] == "r1-chat-runs-qualification:v1"
    assert report["ticket"] == 422
    assert report["production_contract_changes"] is False
    paths = report["paths"]
    assert {entry["path"] for entry in paths} == REQUIRED_PATHS
    assert len(paths) == len(REQUIRED_PATHS)
    evidence_kinds = {item["kind"] for item in report["evidence_kinds"]}

    for entry in paths:
        assert entry["result"] in {"pass", "partial", "not_exercised"}
        assert entry["reason"]
        assert entry["evidence"], entry["path"]
        for evidence in entry["evidence"]:
            assert evidence["kind"] in evidence_kinds
            source_path = ROOT / evidence["file"]
            assert source_path.is_file(), evidence["file"]
            source = source_path.read_text(encoding="utf-8")
            assert evidence["reference"] in source, evidence


def test_issue422_report_distinguishes_synthetic_contracts_from_live_integrations() -> None:
    report = _load_report()
    evidence_kinds = {item["kind"] for item in report["evidence_kinds"]}

    assert evidence_kinds == {
        "browser_with_synthetic_api",
        "in_process_api_contract",
        "deterministic_application_fake",
        "component_test_with_fake",
        "design_documentation",
        "not_exercised",
    }
    paths = {entry["path"]: entry for entry in report["paths"]}
    assert paths["production_api_integration"]["result"] == "not_exercised"
    assert paths["external_provider_qualification"]["result"] == "not_exercised"
    assert all(
        entry["kind"] != "browser_with_synthetic_api"
        for path in ("production_api_integration", "external_provider_qualification")
        for entry in paths[path]["evidence"]
    )
    assert report["external_provider_claim"] is False
