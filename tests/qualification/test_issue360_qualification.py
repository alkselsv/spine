from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from spine.evals.regression_gate import (
    OfflineRegressionGateReport,
    run_offline_regression_gate,
)


ROOT = Path(__file__).parents[2]
REPORT_PATH = ROOT / "docs/qualification/issue-360-r1-final-qualification-report.json"


def _load_report() -> dict[str, Any]:
    value = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_issue360_final_report_is_versioned_and_limits_its_readiness_claim() -> None:
    report = _load_report()

    assert report["schema_version"] == "r1-final-qualification:v1"
    assert report["ticket"] == 360
    assert report["parent_ticket"] == 19
    assert report["gate_schema_version"] == "r1-gate-status:v1"
    assert report["execution"] == {
        "execution_id": "offline-regression-gate:v1",
        "gate_version": "spine-eval-gate:v1",
        "report_schema_version": "r1-gate-status:v1",
        "mode": "offline_deterministic_fake",
        "runs_completed": 2,
        "reproducible": True,
        "network_or_provider_credentials_required": False,
        "ci_execution_identity_fields": ["ci_run_id", "head_sha"],
        "ci_result_artifact": "r1-gate-status",
    }
    assert report["qualification"] == {
        "offline_regression": "qualified_offline",
        "full_r1_readiness": "not_qualified",
        "production_approval": False,
        "scope": "offline_contract_and_regression_only",
    }
    assert report["outstanding"] == {
        "issue_17_product_thresholds": "not_owned_by_this_report",
        "issue_20_anonymized_real_dataset": "not_supplied_or_approved",
        "issue_16_end_to_end_r1_qualification": "outstanding",
        "full_r1_production_readiness": "not_claimed",
    }


def test_issue360_report_traces_every_reused_qualification_artifact() -> None:
    report = _load_report()
    evidence_paths = {
        path
        for coverage in report["coverage"]
        for path in coverage["evidence"]
        if path.startswith("docs/")
    }

    assert evidence_paths == {
        "docs/qualification/issue-194-document-ingestion-report.json",
        "docs/qualification/issue-90-source-projection-lifecycle-report.json",
        "docs/qualification/issue-211-context-projection-report.json",
        "docs/qualification/issue-354-context-adapter-parity-report.json",
        "docs/qualification/issue-355-gate-applicability-report.json",
        "docs/qualification/issue-356-unauthorized-retrieval-disclosure-report.json",
        "docs/qualification/issue-357-grounded-answers-report.json",
        "docs/qualification/issue-358-baseline-comparison.md",
        "docs/qualification/r1-baseline-report.json",
    }
    assert all((ROOT / path).is_file() for path in evidence_paths)


def test_issue360_report_reuses_only_qualified_safety_and_adapter_evidence() -> None:
    report = _load_report()
    unauthorized = json.loads(
        (ROOT / "docs/qualification/issue-356-unauthorized-retrieval-disclosure-report.json").read_text()
    )
    grounded = json.loads(
        (ROOT / "docs/qualification/issue-357-grounded-answers-report.json").read_text()
    )
    parity = json.loads(
        (ROOT / "docs/qualification/issue-354-context-adapter-parity-report.json").read_text()
    )
    assert unauthorized["safety_violation_count"] == 0
    assert unauthorized["protected_payload_disclosure_count"] == 0
    assert grounded["status"] == "qualified_offline"
    assert grounded["production_approval"] is False
    assert all(
        status == "not_run" for status in grounded["external_qualification"].values()
    )
    assert parity["release_gate"]["status"] == "passed"
    assert all(item["safety_status"] == "passed" for item in parity["parity_results"])


def test_issue360_report_records_versioned_applicability_and_execution_identities() -> None:
    report = _load_report()
    applicability = json.loads(
        (ROOT / "docs/qualification/issue-355-gate-applicability-report.json").read_text()
    )
    assert report["identities"]["components"] == applicability["manifest"][
        "component_versions"
    ]
    assert report["identities"]["execution_evidence_ids"] == [
        "offline-regression-gate:v1",
        "issue-194:qualification:v1",
        "issue-90:qualification:v1",
        "issue-211:qualification:v1",
        "repository-pytest:execution:v1",
        "postgresql-acceptance:execution:v1",
        "g01:execution:v1",
        "g02:execution:v1",
        "g03:execution:v1",
        "g06:execution:v1",
        "r1-evaluator-catalog:v1",
        "qa-r1-dataset:v1",
        "r1-gate-applicability:v1",
        "r1-baseline-comparison:v1",
    ]


def test_issue360_final_report_keeps_mandatory_suites_and_provider_statuses_separate() -> None:
    report = _load_report()

    assert report["mandatory_checks"] == {
        "offline_deterministic_gate": {
            "status": "required",
            "evidence_id": "offline-regression-gate:v1",
        },
        "repository_pytest": {
            "status": "required",
            "evidence_id": "repository-pytest:execution:v1",
            "result_field": "repository_pytest_status",
            "artifact_name": "r1-gate-status",
        },
        "postgresql_acceptance": {
            "status": "required",
            "evidence_id": "postgresql-acceptance:execution:v1",
            "result_field": "postgresql_acceptance_status",
            "artifact_name": "r1-gate-status",
        },
        "ci_workflow": ".github/workflows/test.yml:r1-regression-gate",
    }
    assert report["provider_profiles"] == {
        "cognee": "skipped",
        "llm": "skipped",
        "selected_but_unavailable_status": "unavailable",
        "skipped_or_unavailable_can_count_as_passed": False,
    }

    workflow = (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")
    gate_documentation = (
        ROOT / "docs/qualification/issue-359-r1-regression-gate.md"
    ).read_text(encoding="utf-8")
    assert "Run mandatory repository and PostgreSQL suite" in workflow
    assert "--repository-tests-unavailable" in workflow
    assert '--ci-run-id "$GITHUB_RUN_ID"' in workflow
    assert '--head-sha "$GITHUB_SHA"' in workflow
    assert "name: r1-gate-status" in workflow
    assert "path: r1-gate-status.json" in workflow
    assert "A PostgreSQL prerequisite failure is" in gate_documentation
    assert "`unavailable`" in gate_documentation


def test_issue360_final_report_matches_mandatory_gate_and_skipped_profiles() -> None:
    result = run_offline_regression_gate(repository_pytest_status="passed")
    report = _load_report()

    assert result.status == "passed"
    assert result.passed is True
    assert result.postgresql_acceptance_status == "passed"
    assert result.schema_version == report["gate_schema_version"]
    assert result.dataset is not None
    assert result.dataset.model_dump(mode="json") == report["identities"]["dataset"]
    assert result.runs_completed == report["execution"]["runs_completed"]
    assert result.reproducible is report["execution"]["reproducible"]
    assert [item.model_dump(mode="json") for item in result.evaluator_catalog] == report[
        "identities"
    ]["evaluators"]
    assert result.applicability is not None and result.applicability.passed
    assert result.baseline_comparison is not None
    assert result.baseline_comparison.status == "comparable"
    baseline_summary = report["baseline_comparison"]
    assert str(result.baseline_comparison.baseline_report_id) == baseline_summary[
        "baseline_report_id"
    ]
    assert baseline_summary["added_case_ids"] == list(
        result.baseline_comparison.added_case_ids
    )
    assert baseline_summary["removed_case_ids"] == list(
        result.baseline_comparison.removed_case_ids
    )
    assert baseline_summary["changed_case_ids"] == list(
        result.baseline_comparison.changed_case_ids
    )
    assert {
        item["evaluator_key"]: item["delta"]
        for item in baseline_summary["aggregate_metric_deltas"]
    } == {
        item.evaluator_key: item.delta
        for item in result.baseline_comparison.aggregate_deltas
    }
    assert baseline_summary["failure_clusters"] == []
    assert baseline_summary["safety_failures"] == []
    assert {profile.status for profile in result.provider_profiles} == {"skipped"}
    assert all(item.status == "passed" for item in result.qualification_evidence)
    evidence_by_id = {item.evidence_id: item for item in result.qualification_evidence}
    assert evidence_by_id["repository-pytest:execution:v1"].status == "passed"
    assert evidence_by_id["postgresql-acceptance:execution:v1"].status == "passed"


def test_issue360_failed_or_unavailable_repository_gate_marks_postgresql_evidence() -> None:
    failed = run_offline_regression_gate(repository_pytest_status="failed")
    unavailable = run_offline_regression_gate(repository_pytest_status="unavailable")

    failed_evidence = {item.evidence_id: item.status for item in failed.qualification_evidence}
    unavailable_evidence = {
        item.evidence_id: item.status for item in unavailable.qualification_evidence
    }
    assert failed_evidence["repository-pytest:execution:v1"] == "failed"
    assert failed_evidence["postgresql-acceptance:execution:v1"] == "unavailable"
    assert unavailable_evidence["repository-pytest:execution:v1"] == "unavailable"
    assert unavailable_evidence["postgresql-acceptance:execution:v1"] == "unavailable"
    assert failed.postgresql_acceptance_status == "unavailable"
    assert unavailable.postgresql_acceptance_status == "unavailable"


def test_issue360_r1_gate_v1_reads_older_reports_without_postgresql_field() -> None:
    current = run_offline_regression_gate(repository_pytest_status="passed")
    payload = current.model_dump(mode="python")
    del payload["postgresql_acceptance_status"]

    legacy = OfflineRegressionGateReport.model_validate(payload)

    assert legacy.postgresql_acceptance_status is None


def test_issue360_missing_qualification_evidence_cannot_be_qualified(monkeypatch) -> None:
    import spine.evals.regression_gate as regression_gate

    monkeypatch.setattr(regression_gate, "_qualification_evidence", lambda **_: ())

    result = run_offline_regression_gate(repository_pytest_status="passed")

    assert result.status == "not-qualified"
    assert not result.passed
    assert result.applicability is not None
    assert not result.applicability.passed


def test_issue360_report_hard_safety_violations_fail_even_when_quality_metrics_pass(
    monkeypatch,
) -> None:
    import spine.evals.regression_gate as regression_gate
    from spine.domain.evaluations import EvaluationVerdict
    from spine.evals.offline_runner import run_r1_evaluation_sync

    monkeypatch.setattr(
        regression_gate,
        "_qualification_evidence",
        lambda **_: (
            regression_gate.QualificationEvidence(
                evidence_id="g02:execution:v1",
                source="synthetic-safety-report",
                status="failed",
            ),
        ),
    )

    quality_passing_report = run_r1_evaluation_sync()
    quality_passing_report = quality_passing_report.model_copy(
        update={
            "evaluations": tuple(
                evaluation.model_copy(update={"verdict": EvaluationVerdict.PASS})
                for evaluation in quality_passing_report.evaluations
            )
        }
    )
    result = run_offline_regression_gate(
        repository_pytest_status="passed",
        report_factory=lambda: quality_passing_report,
    )

    assert result.report is not None
    assert all(metric.verdict == "pass" for metric in result.report.evaluations)
    assert result.status == "failed"
    assert not result.passed
