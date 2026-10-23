from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "docs/qualification/issue-561-five-format-ingestion-report.json"
REQUIRED_FORMATS = {"pdf", "docx", "xlsx", "markdown", "text"}
REQUIRED_SCENARIOS = {
    "five_format_upload",
    "duplicate_idempotent_replay",
    "changed_content_new_revision",
    "tombstone_fences_disclosure",
    "parser_failure_retry",
    "original_storage_failure_retry",
    "post_write_pre_acceptance_failure_retry",
    "workspace_environment_isolation",
    "safe_ui_states",
}
REQUIRED_UI_STATES = {
    "running",
    "partial",
    "retryable_failure",
    "accepted",
    "stale",
    "deleted",
}


def _report() -> dict[str, Any]:
    value = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_issue561_report_is_metadata_only_and_fail_closed() -> None:
    report = _report()

    assert report["schema_version"] == "issue-561-five-format-ingestion:v1"
    assert report["ticket"] == 561
    assert report["status"] in {"pass", "blocked", "not_verified", "fail"}
    assert set(report["required_formats"]) == REQUIRED_FORMATS
    assert set(report["required_scenarios"]) == REQUIRED_SCENARIOS
    assert set(report["formats"]) <= REQUIRED_FORMATS
    assert set(report["scenarios"]) <= REQUIRED_SCENARIOS
    assert set(report["ui_states"]) <= REQUIRED_UI_STATES
    assert report["execution"] == {
        "api_mocking": False,
        "browser": "chromium",
        "cli_user_actions": False,
        "controlled_fixture": False,
        "route_fulfillment": False,
    }
    assert report["privacy"] == {
        "content_fields_recorded": False,
        "credentials_recorded": False,
        "filenames_recorded": False,
        "metadata_only": True,
        "raw_errors_recorded": False,
        "screenshots": False,
        "trace": False,
        "video": False,
    }
    serialized = REPORT_PATH.read_text(encoding="utf-8").lower()
    for forbidden in (
        "access_token",
        "bearer ",
        '"filename":',
        '"document_text":',
        '"raw_error":',
    ):
        assert forbidden not in serialized


def test_issue561_pass_requires_every_live_format_and_lifecycle_edge() -> None:
    report = _report()

    if report["status"] != "pass":
        assert report["reason_code"]
        return

    assert set(report["formats"]) == REQUIRED_FORMATS
    assert set(report["scenarios"]) == REQUIRED_SCENARIOS
    assert set(report["ui_states"]) == REQUIRED_UI_STATES
    assert report["reason_code"] is None
    assert report["q16_01_compatibility"] == "pass"
    assert report["authorization"]["corpus_approval"] == "pass"
    assert report["authorization"]["trusted_upload_authority"] == "pass"
    assert report["isolation"] == {
        "denied_upload": "pass",
        "denied_status_read": "pass",
        "environment_isolation": "pass",
        "protected_metadata_disclosed": False,
        "workspace_isolation": "pass",
    }
    assert len(report["deployment"]["commit_sha"]) == 40
    assert len(report["deployment"]["deployment_fingerprint"]) == 64
    assert report["deployment"]["authorization_generation"] >= 1
    observations = report["format_observations"]
    assert {item["format"] for item in observations} == REQUIRED_FORMATS
    assert all(item["accepted"] for item in observations)
    assert all(item["checksum_reference"] for item in observations)
    assert all(item["locator_kinds"] for item in observations)
    assert all(item["source_reference"] for item in observations)
    assert all(item["revision_reference"] for item in observations)
    assert report["lifecycle"]["duplicate_logical_effect_count"] == 1
    assert report["lifecycle"]["duplicate_result_stable"] is True
    assert report["lifecycle"]["changed_revision_preserved_history"] is True
    assert report["lifecycle"]["tombstone_fenced_disclosure"] is True
    assert report["failures"]["parser"]["retry_passed"] is True
    assert report["failures"]["original_storage"]["retry_passed"] is True
    assert report["failures"]["post_write_pre_acceptance"]["retry_passed"] is True
