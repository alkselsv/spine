from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import subprocess
from typing import Any

import pytest
from pydantic import ValidationError

from spine.evaluations.operational_foundation import (
    BlockerCode,
    EvidenceTimestampKind,
    OperationalFoundationQualificationReport,
    QualificationEnvironment,
    QualificationStatus,
    load_operational_foundation_report,
    split_evidence_reference,
)
from tests.qualification.gate_runner import qualified_tree_sha256


ROOT = Path(__file__).parents[2]
REPORT_PATH = ROOT / "docs/qualification/issue-369-operational-foundation-report.json"
TRACEABILITY_PATH = ROOT / "docs/qualification/issue-369-operational-foundation.md"


def _load_report() -> OperationalFoundationQualificationReport:
    return load_operational_foundation_report(REPORT_PATH)


def _git(*arguments: str) -> str:
    result = subprocess.run(
        ("git", *arguments),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_issue369_report_contract_is_closed_versioned_and_content_free() -> None:
    report = _load_report()

    assert report.schema_version == "r1-operational-foundation-qualification:v1"
    assert report.report_id == "issue-369:qualification:v1"
    assert report.ticket == 369
    assert report.parent_ticket == 18
    assert report.immutable is True
    assert report.content_free is True
    assert report.server_logs_used is False
    assert report.undocumented_claims_used is False

    valid = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

    def extra_field(payload: dict[str, Any]) -> None:
        payload["unexpected"] = "not allowed"

    def non_utc_timestamp(payload: dict[str, Any]) -> None:
        payload["generated_at"] = "2026-10-18T05:39:16"

    def non_utc_evidence_timestamp(payload: dict[str, Any]) -> None:
        payload["evidence"][0]["observed_at"] = "2026-10-16T18:42:31"

    def source_record_timestamp_without_record(payload: dict[str, Any]) -> None:
        payload["evidence"][0]["timestamp_kind"] = "qualification_record"

    def non_utc_execution_timestamp(payload: dict[str, Any]) -> None:
        payload["execution_evidence"][0]["executed_at"] = "2026-10-18T05:38:00"

    def pass_with_blocker(payload: dict[str, Any]) -> None:
        payload["evidence"][0]["blocker"] = "must be rejected"

    def missing_blocker(payload: dict[str, Any]) -> None:
        del payload["evidence"][2]["blocker"]

    def false_physical_claim(payload: dict[str, Any]) -> None:
        payload["evidence"][2]["independently_verified_physical_evidence"] = True

    def duplicate_gate(payload: dict[str, Any]) -> None:
        payload["evidence"][8]["gate_id"] = "O08"

    def incomplete_outcomes(payload: dict[str, Any]) -> None:
        del payload["outcome_semantics"]["unavailable"]

    def incomplete_cross_cutting(payload: dict[str, Any]) -> None:
        del payload["cross_cutting_verification"]["workspace_isolation"]

    def dropped_decision_blocker(payload: dict[str, Any]) -> None:
        payload["gate_decision"]["reason_codes"].pop()

    def invalid_path_gate(payload: dict[str, Any]) -> None:
        payload["operational_path"][0]["gate_ids"] = ["O10"]

    def all_pass(payload: dict[str, Any]) -> None:
        for evidence in payload["evidence"]:
            evidence["verification_status"] = "pass"
            evidence.pop("blocker", None)

    def incomplete_execution_coverage(payload: dict[str, Any]) -> None:
        payload["execution_evidence"][0]["gate_ids"].remove("O09")

    for mutate in (
        extra_field,
        non_utc_timestamp,
        non_utc_evidence_timestamp,
        source_record_timestamp_without_record,
        non_utc_execution_timestamp,
        pass_with_blocker,
        missing_blocker,
        false_physical_claim,
        duplicate_gate,
        incomplete_outcomes,
        incomplete_cross_cutting,
        dropped_decision_blocker,
        invalid_path_gate,
        all_pass,
        incomplete_execution_coverage,
    ):
        payload = json.loads(json.dumps(valid))
        mutate(payload)
        with pytest.raises(ValidationError):
            OperationalFoundationQualificationReport.model_validate(payload)


def test_issue369_gate_decision_retains_every_fail_closed_condition() -> None:
    report = _load_report()
    evidence = {item.gate_id: item for item in report.evidence}

    assert report.gate_decision.status is QualificationStatus.BLOCKED
    assert report.gate_decision.production_qualified is False
    assert report.gate_decision.downstream_ticket == 16
    assert set(report.gate_decision.reason_codes) == set(BlockerCode)
    assert set(report.unresolved_conditions) == set(BlockerCode)
    assert evidence["O03"].verification_status is QualificationStatus.BLOCKED
    assert evidence["O04"].verification_status is QualificationStatus.BLOCKED
    assert evidence["O03"].independently_verified_physical_evidence is False
    assert evidence["O04"].independently_verified_physical_evidence is False
    assert report.unsupported_claims.model_dump() == {
        "rpo_rto": False,
        "pitr": False,
        "ha": False,
        "quality_threshold": False,
    }


def test_issue369_evidence_is_pinned_to_real_merged_git_lineage() -> None:
    report = _load_report()
    expected_environments = {
        "O01": QualificationEnvironment.OFFLINE,
        "O02": QualificationEnvironment.OFFLINE,
        "O03": QualificationEnvironment.SYNTHETIC_AND_DISPOSABLE,
        "O04": QualificationEnvironment.SYNTHETIC_AND_DISPOSABLE,
        "O05": QualificationEnvironment.SYNTHETIC_AND_DISPOSABLE,
        "O06": QualificationEnvironment.OFFLINE,
        "O07": QualificationEnvironment.OFFLINE,
        "O08": QualificationEnvironment.BROWSER,
        "O09": QualificationEnvironment.SYNTHETIC_AND_DISPOSABLE,
    }

    assert {item.gate_id for item in report.evidence} == set(expected_environments)
    for item in report.evidence:
        assert item.environment is expected_environments[item.gate_id]
        _git("cat-file", "-e", f"{item.source_commit}^{{commit}}")
        subprocess.run(
            ("git", "merge-base", "--is-ancestor", item.source_commit, "origin/main"),
            cwd=ROOT,
            check=True,
        )
        if item.timestamp_kind is EvidenceTimestampKind.COMMIT:
            committed_at = datetime.fromisoformat(
                _git("show", "-s", "--format=%cI", item.source_commit)
            )
            assert item.observed_at == committed_at
        else:
            assert item.source_record is not None
            source_record = json.loads(
                _git("show", f"{item.source_commit}:{item.source_record}")
            )
            assert source_record["ticket"] == item.ticket
            assert source_record["schema_version"] == item.source_schema_version
            assert datetime.fromisoformat(
                source_record["generated_at"].replace("Z", "+00:00")
            ) == item.observed_at
        for reference in item.evidence_references:
            relative_path, node_id = split_evidence_reference(reference)
            assert (ROOT / relative_path).is_file()
            _git("cat-file", "-e", f"{item.source_commit}:{relative_path}")
            assert node_id in _git(
                "grep",
                "-F",
                "-e",
                node_id,
                item.source_commit,
                "--",
                relative_path,
            )

    execution_by_id = {item.execution_id: item for item in report.execution_evidence}
    repository_execution = execution_by_id["issue-68-repository-gate:issue-369:v1"]
    assert repository_execution.source_artifact is not None
    repository_artifact = json.loads(
        (ROOT / repository_execution.source_artifact).read_text(encoding="utf-8")
    )
    assert all(result["status"] == "passed" for result in repository_artifact["results"])
    assert repository_artifact["results"][2]["summary"] == "3463 passed, 5 skipped"
    # This immutable record describes the tree qualified at the time of Issue
    # #369. Current source changes are evaluated by the active CI suite.
    web_execution = execution_by_id["issue-369-web-diagnostics:v1"]
    assert web_execution.environment is QualificationEnvironment.BROWSER
    assert web_execution.working_directory == "web"
    assert (ROOT / web_execution.working_directory / "package.json").is_file()


def test_issue369_report_covers_the_complete_operational_path_and_safety_matrix() -> None:
    report = _load_report()

    assert [stage.model_dump(mode="json") for stage in report.operational_path] == [
        {"stage": "safe_runtime_telemetry", "gate_ids": ["O01"]},
        {"stage": "metrics_semantics", "gate_ids": ["O02"]},
        {"stage": "quarantine_inspection", "gate_ids": ["O06"]},
        {"stage": "operational_correlation", "gate_ids": ["O07"]},
        {"stage": "ui_no_log_diagnosis", "gate_ids": ["O08"]},
        {"stage": "backup_and_restore", "gate_ids": ["O03", "O04"]},
        {"stage": "projection_rebuild_and_rollback", "gate_ids": ["O05"]},
        {"stage": "operator_runbooks", "gate_ids": ["O09"]},
    ]
    for references in report.cross_cutting_verification.values():
        assert references
        for reference in references:
            relative_path, _ = split_evidence_reference(reference)
            assert (ROOT / relative_path).is_file()


def test_issue369_traceability_names_every_gate_and_the_downstream_handoff() -> None:
    content = TRACEABILITY_PATH.read_text(encoding="utf-8")

    assert all(f"| O{index:02d} |" in content for index in range(1, 10))
    assert "Issue #16" in content
    assert "Production qualification: **blocked**" in content
    assert "server logs" in content
