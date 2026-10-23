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


def _assert_pass_report(report: dict[str, Any]) -> None:
    assert set(report["formats"]) == REQUIRED_FORMATS
    assert set(report["scenarios"]) == REQUIRED_SCENARIOS
    assert set(report["ui_states"]) == REQUIRED_UI_STATES
    assert report["reason_code"] is None
    assert report["q16_01_compatibility"] == "pass"
    assert report["authorization"] == {
        "corpus_approval": "pass",
        "corpus_approval_reference": report["authorization"]["corpus_approval_reference"],
        "trusted_upload_authority": "pass",
        "trusted_upload_authority_reference": report["authorization"]["trusted_upload_authority_reference"],
    }
    assert report["authorization"]["corpus_approval_reference"]
    assert report["authorization"]["trusted_upload_authority_reference"]
    assert report["isolation"] == {
        "denied_environment_upload": "pass",
        "denied_environment_status_read": "pass",
        "denied_workspace_upload": "pass",
        "denied_workspace_status_read": "pass",
        "protected_metadata_disclosed": False,
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
    duplicate = report["lifecycle"]["duplicate"]
    assert duplicate["logical_effect_count"] == 1
    assert duplicate["replay"] is True
    assert duplicate["result_stable"] is True
    changed_revision = report["lifecycle"]["changed_revision"]
    assert changed_revision["predecessor_preserved"] is True
    assert changed_revision["replay"] is True
    assert changed_revision["result_stable"] is True
    assert report["lifecycle"]["tombstone"]["disclosure_fenced"] is True
    assert report["lifecycle"]["tombstone"]["history_preserved"] is True
    for name in ("parser", "original_storage", "post_write_pre_acceptance"):
        failure = report["failures"][name]
        assert failure["accepted_before_retry"] is (
            name != "post_write_pre_acceptance"
        )
        assert failure["logical_effect_count"] == 1
        assert failure["retry_passed"] is True
        assert failure["retry_replay"] is True


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

    _assert_pass_report(report)


def test_issue561_complete_pass_evidence_satisfies_gate() -> None:
    report = _report()
    report.update(
        status="pass",
        reason_code=None,
        formats=sorted(REQUIRED_FORMATS),
        scenarios=sorted(REQUIRED_SCENARIOS),
        ui_states=sorted(REQUIRED_UI_STATES),
        q16_01_compatibility="pass",
        authorization={
            "corpus_approval": "pass",
            "corpus_approval_reference": "corpus-approval-42",
            "trusted_upload_authority": "pass",
            "trusted_upload_authority_reference": "authority-approval-17",
        },
        isolation={
            "denied_environment_upload": "pass",
            "denied_environment_status_read": "pass",
            "denied_workspace_upload": "pass",
            "denied_workspace_status_read": "pass",
            "protected_metadata_disclosed": False,
        },
        lifecycle={
            "duplicate": {
                "logical_effect_count": 1,
                "replay": True,
                "result_stable": True,
            },
            "changed_revision": {
                "predecessor_preserved": True,
                "replay": True,
                "result_stable": True,
            },
            "tombstone": {"disclosure_fenced": True, "history_preserved": True},
        },
        failures={
            name: {
                "accepted_before_retry": name != "post_write_pre_acceptance",
                "logical_effect_count": 1,
                "retry_passed": True,
                "retry_replay": True,
            }
            for name in ("parser", "original_storage", "post_write_pre_acceptance")
        },
    )
    report["format_observations"] = [
        {
            "accepted": True,
            "checksum_reference": "a" * 64,
            "format": format_name,
            "locator_kinds": ["stable_locator"],
            "source_reference": "10000000-0000-4000-8000-000000000001",
            "revision_reference": "20000000-0000-4000-8000-000000000001",
        }
        for format_name in REQUIRED_FORMATS
    ]

    _assert_pass_report(report)
