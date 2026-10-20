"""Executable traceability checks for Issue #509 qualification evidence."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = PROJECT_ROOT / "docs/qualification/issue-509-backup-recovery-qualification.json"

EXPECTED_CRITERIA = {f"AC{number}" for number in range(1, 10)}
OUTCOME_STATUSES = {"pass", "fail", "blocked", "not_verified", "unavailable"}
CRITERIA_STATUSES = OUTCOME_STATUSES - {"fail"}
FORBIDDEN_REPORT_TOKENS = (
    "password",
    "secret",
    "credential",
    "endpoint",
    "filename",
    "raw_payload",
    "source_text",
    "stack_trace",
)


def _load_report() -> dict[str, Any]:
    return json.loads(REPORT_PATH.read_text(encoding="utf-8"))


def _all_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for nested in value.values() for item in _all_strings(nested)]
    if isinstance(value, list):
        return [item for nested in value for item in _all_strings(nested)]
    return []


def test_issue509_report_is_closed_versioned_and_content_free() -> None:
    report = _load_report()

    assert set(report) == {
        "schema_version",
        "report_id",
        "ticket",
        "parent_ticket",
        "generated_at",
        "target_commit",
        "immutable",
        "content_free",
        "decision",
        "production_qualified",
        "scope",
        "outcome_semantics",
        "criteria",
        "evidence_artifacts",
        "unresolved_conditions",
        "safety_assertions",
        "unsupported_claims",
    }
    assert report["schema_version"] == "r1-backup-recovery-qualification:v1"
    assert report["report_id"] == "issue-509:qualification:v1"
    assert report["ticket"] == 509
    assert report["parent_ticket"] == 16
    assert report["immutable"] is True
    assert report["content_free"] is True
    assert report["decision"] == "blocked"
    assert report["production_qualified"] is False
    assert re.fullmatch(r"[0-9a-f]{40}", report["target_commit"])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", report["generated_at"])
    assert set(report["outcome_semantics"]) == OUTCOME_STATUSES

    for text in _all_strings(report):
        lowered = text.lower()
        assert not any(token in lowered for token in FORBIDDEN_REPORT_TOKENS)
        assert "\\" not in text
        assert not re.search(r"(?:^|[^:])/(?:[^/]|$)", text)


def test_issue509_report_covers_every_acceptance_criterion_fail_closed() -> None:
    report = _load_report()
    criteria = report["criteria"]

    assert {item["criterion_id"] for item in criteria} == EXPECTED_CRITERIA
    assert all(
        set(item) == {
            "criterion_id",
            "status",
            "reason_codes",
            "evidence_artifact_ids",
            "deployment_exercise",
        }
        for item in criteria
    )
    assert all(item["status"] in CRITERIA_STATUSES for item in criteria)
    assert {item["criterion_id"] for item in criteria if item["status"] == "blocked"} == {
        "AC1",
        "AC3",
    }
    assert {item["criterion_id"] for item in criteria if item["status"] == "not_verified"} == {
        "AC2",
        "AC5",
        "AC6",
        "AC7",
    }
    assert {item["criterion_id"] for item in criteria if item["status"] == "pass"} == {
        "AC4",
        "AC8",
        "AC9",
    }

    assert len({item["artifact_id"] for item in report["evidence_artifacts"]}) == len(
        report["evidence_artifacts"]
    )
    artifacts = {item["artifact_id"]: item for item in report["evidence_artifacts"]}
    assert artifacts["deployment-approved-recovery-manifest:v1"]["status"] == "unavailable"
    assert artifacts["deployment-approved-recovery-manifest:v1"]["digest"] is None
    assert artifacts["isolated-restore-evidence:v1"]["status"] == "unavailable"
    assert artifacts["authorized-recovery-canary:v1"]["status"] == "unavailable"
    assert all(item["physical_verification"] is False for item in artifacts.values())
    assert report["unresolved_conditions"]
    assert all(value is False for value in report["unsupported_claims"].values())


def test_issue509_report_does_not_promote_repository_evidence_to_live_recovery() -> None:
    report = _load_report()
    live_criteria = {"AC1", "AC2", "AC3", "AC5", "AC6", "AC7"}

    for item in report["criteria"]:
        if item["criterion_id"] in live_criteria:
            assert item["status"] != "pass"
            assert item["deployment_exercise"] in {"not_started", "covered_by_deterministic_tests_only"}

    assert report["safety_assertions"] == [
        "live_source_deployment_not_modified",
        "isolated_target_not_created_without_approval",
        "no_sensitive_auth_values_recorded",
        "no_production_readiness_or_cutover_claim_made",
        "physical_projection_remains_non_authoritative",
    ]
