"""Executable checks for Issue #511's final qualification record."""

from __future__ import annotations

import json
from pathlib import Path

from spine.evaluations.release_gates.final_qualification import (
    EvidenceIndex,
    FinalQualificationPackage,
    FinalQualificationReport,
    evaluate_final_qualification,
    verify_evidence_index,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUT_PATH = (
    PROJECT_ROOT / "docs/qualification/issue-511-final-qualification-input.json"
)
REPORT_PATH = (
    PROJECT_ROOT / "docs/qualification/issue-511-final-qualification-report.json"
)
EVIDENCE_INDEX_PATH = PROJECT_ROOT / "docs/qualification/issue-511-evidence-index.json"
SUMMARY_PATH = (
    PROJECT_ROOT / "docs/qualification/issue-511-final-r0-r1-qualification.md"
)
REMEDIATION_INPUT_PATH = (
    PROJECT_ROOT
    / "docs/qualification/issue-511-final-qualification-input-v2.json"
)
REMEDIATION_REPORT_PATH = (
    PROJECT_ROOT
    / "docs/qualification/issue-511-final-qualification-report-v2.json"
)
REMEDIATION_INDEX_PATH = (
    PROJECT_ROOT / "docs/qualification/issue-511-evidence-index-v2.json"
)
REMEDIATION_EVIDENCE_PATH = (
    PROJECT_ROOT / "docs/qualification/issue-511-r1-remediation-evidence.json"
)


def test_issue511_report_is_exact_recomputation_of_immutable_input() -> None:
    package = FinalQualificationPackage.model_validate_json(
        INPUT_PATH.read_text(encoding="utf-8")
    )
    checked_in = FinalQualificationReport.model_validate_json(
        REPORT_PATH.read_text(encoding="utf-8")
    )
    index = EvidenceIndex.model_validate_json(
        EVIDENCE_INDEX_PATH.read_text(encoding="utf-8")
    )
    verified = verify_evidence_index(index, root=PROJECT_ROOT)

    assert checked_in == evaluate_final_qualification(package, verified)
    assert checked_in.decision == "no_go"
    assert tuple(item.criterion_id for item in checked_in.criteria) == tuple(
        f"R{number}" for number in range(1, 18)
    )
    assert checked_in.criteria[-1].status == "fail"
    assert checked_in.counts.model_dump() == {
        "passed": 0,
        "failed": 1,
        "blocked": 4,
        "not_verified": 12,
        "not_applicable": 1,
    }


def test_issue511_referenced_artifact_digests_match_repository_artifacts() -> None:
    package = FinalQualificationPackage.model_validate_json(
        INPUT_PATH.read_text(encoding="utf-8")
    )
    index = EvidenceIndex.model_validate_json(
        EVIDENCE_INDEX_PATH.read_text(encoding="utf-8")
    )
    verified = verify_evidence_index(index, root=PROJECT_ROOT)

    for criterion in package.criteria:
        for evidence in criterion.evidence:
            assert verified.is_verified(evidence.evidence_id, evidence.digest)


def test_issue511_preserves_component_boundaries_and_not_applicable_status() -> None:
    package = FinalQualificationPackage.model_validate_json(
        INPUT_PATH.read_text(encoding="utf-8")
    )
    by_id = {item.criterion_id: item for item in package.criteria}

    provider_evidence = by_id["R5"].evidence[0]
    assert provider_evidence.status == "pass"
    assert provider_evidence.boundary == "physical"
    assert by_id["R5"].status == "not_verified"
    assert provider_evidence.target_commit is None

    denied_quality = by_id["R15"].evidence[2]
    assert denied_quality.status == "not_applicable"
    assert denied_quality.mandatory is False
    assert by_id["R15"].status == "blocked"


def test_issue511_summary_documents_reproduction_without_release_claim() -> None:
    summary = SUMMARY_PATH.read_text(encoding="utf-8")

    assert "Status: **NO-GO**" in summary
    assert "uv run spine-final-qualification" in summary
    assert "Exit code `1` is the expected valid NO-GO decision" in summary
    assert "No ROADMAP checkbox" in summary


def test_issue511_r1_remediation_is_exact_recomputation_for_integrated_main() -> None:
    package = FinalQualificationPackage.model_validate_json(
        REMEDIATION_INPUT_PATH.read_text(encoding="utf-8")
    )
    checked_in = FinalQualificationReport.model_validate_json(
        REMEDIATION_REPORT_PATH.read_text(encoding="utf-8")
    )
    index = EvidenceIndex.model_validate_json(
        REMEDIATION_INDEX_PATH.read_text(encoding="utf-8")
    )
    verified = verify_evidence_index(index, root=PROJECT_ROOT)

    assert checked_in == evaluate_final_qualification(package, verified)
    assert checked_in.target_commit == "5acb97d4a5d47f8d85a91f66c7c6b471b086a91e"
    assert checked_in.criteria[0].criterion_id == "R1"
    assert checked_in.criteria[0].status == "pass"
    assert checked_in.decision == "no_go"
    assert checked_in.counts.model_dump() == {
        "passed": 1,
        "failed": 1,
        "blocked": 3,
        "not_verified": 12,
        "not_applicable": 1,
    }


def test_issue511_r1_remediation_preserves_repository_only_scope() -> None:
    evidence = json.loads(
        REMEDIATION_EVIDENCE_PATH.read_text(encoding="utf-8")
    )

    assert evidence["github_actions"]["conclusion"] == "success"
    assert evidence["repository_gate"]["failed"] == 0
    assert evidence["repository_gate"]["passed"] == 5773
    assert evidence["migration_graph"]["status"] == "passed"
    assert evidence["scope"] == {
        "production_approval": False,
        "qualification_scope": "repository-migration-and-test-gates",
        "provider_profiles": "not_exercised",
    }
