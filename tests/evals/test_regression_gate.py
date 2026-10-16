from __future__ import annotations

from pathlib import Path
import socket
import sys

from spine.domain.evaluations import EvaluationVerdict
from spine.evals.offline_runner import run_r1_evaluation_sync
from spine.evals.regression_gate import (
    OfflineRegressionGateReport,
    QualificationEvidence,
    main,
    _qualification_record_is_valid,
    run_offline_regression_gate,
)


def test_offline_regression_gate_passes_reproducible_r1_contract() -> None:
    result = run_offline_regression_gate(repository_pytest_status="passed")

    assert result.passed is True
    assert result.reproducible is True
    assert result.failures == ()
    assert result.report is not None
    assert result.report.qualification_scope == "offline-contract-only"
    assert result.report.dataset.version == "1.0.0"
    assert result.schema_version == "r1-gate-status:v1"
    assert result.status == "passed"
    assert result.applicability is not None and result.applicability.passed
    assert result.baseline_comparison is not None
    assert result.baseline_comparison.status == "comparable"
    assert result.baseline_comparison.baseline_report_id == "f05c7bcd-5702-5d2c-a9f2-0dd86c2ba7f6"
    assert result.baseline_comparison.baseline_component_versions.prompt_version == "r1-baseline:v1"
    assert result.baseline_comparison.aggregate_deltas
    assert {item.status for item in result.provider_profiles} == {"skipped"}
    assert {item.evidence_id for item in result.qualification_evidence} >= {
        "issue-194:qualification:v1",
        "issue-90:qualification:v1",
        "issue-211:qualification:v1",
        "g01:execution:v1",
        "g02:execution:v1",
        "g03:execution:v1",
        "g06:execution:v1",
        "r1-gate-applicability:v1",
        "r1-baseline-comparison:v1",
    }


def test_offline_regression_gate_reports_schema_drift() -> None:
    baseline = run_r1_evaluation_sync()

    result = run_offline_regression_gate(
        report_factory=lambda: baseline.model_copy(update={"runner_version": "v999"})
    )

    assert result.passed is False
    assert any(failure.category == "schema" for failure in result.failures)


def test_offline_regression_gate_reports_expected_outcome_and_evidence_leakage() -> None:
    baseline = run_r1_evaluation_sync()
    scenario = next(item for item in baseline.scenarios if item.case_id == "denied-acl")
    leaked = scenario.model_copy(
        update={
            "observed_outcome": "denied",
            "observed_evidence_ids": ("ev-policy-secret",),
            "status": "failed",
        }
    )
    report = baseline.model_copy(
        update={
            "scenarios": tuple(
                leaked if item.case_id == "denied-acl" else item
                for item in baseline.scenarios
            )
        }
    )

    result = run_offline_regression_gate(report_factory=lambda: report)

    assert result.passed is False
    assert any(
        failure.case_id == "denied-acl"
        and failure.category == "scenario"
        and "evidence" in failure.reason
        for failure in result.failures
    )


def test_offline_regression_gate_reports_wrong_expected_outcome() -> None:
    baseline = run_r1_evaluation_sync()
    scenario = next(item for item in baseline.scenarios if item.case_id == "allowed-acl")
    wrong_outcome = scenario.model_copy(
        update={"observed_outcome": "abstained", "status": "failed"}
    )
    report = baseline.model_copy(
        update={
            "scenarios": tuple(
                wrong_outcome if item.case_id == "allowed-acl" else item
                for item in baseline.scenarios
            )
        }
    )

    result = run_offline_regression_gate(report_factory=lambda: report)

    assert result.passed is False
    assert any(
        failure.case_id == "allowed-acl"
        and failure.category == "scenario"
        and "expected answer" in failure.reason
        for failure in result.failures
    )


def test_offline_regression_gate_identifies_invalid_citation_evaluator() -> None:
    baseline = run_r1_evaluation_sync()
    original = next(
        result for result in baseline.evaluations if result.evaluator_key == "qa.citations"
    )
    invalid = original.model_copy(
        update={
            "verdict": EvaluationVerdict.FAIL,
            "details": {"failure_counts": {"invalid_locator": 1}, "deterministic_gate": "failed"},
        }
    )
    report = baseline.model_copy(
        update={
            "evaluations": tuple(
                invalid if result.id == original.id else result
                for result in baseline.evaluations
            )
        }
    )

    result = run_offline_regression_gate(report_factory=lambda: report)

    failures = [
        failure
        for failure in result.failures
        if failure.evaluator_key == "qa.citations"
    ]
    assert failures
    assert failures[0].case_id == "allowed-acl"
    assert failures[0].evaluator_version == "v1"
    assert "invalid_locator" in failures[0].reason


def test_offline_regression_gate_checks_deterministic_retrieval_metric_value() -> None:
    baseline = run_r1_evaluation_sync()
    original = next(
        result
        for result in baseline.evaluations
        if result.evaluator_key == "retrieval.recall_at_k"
    )
    changed = original.model_copy(update={"score": 0.0})
    report = baseline.model_copy(
        update={
            "evaluations": tuple(
                changed if result.id == original.id else result
                for result in baseline.evaluations
            )
        }
    )

    result = run_offline_regression_gate(report_factory=lambda: report)

    assert result.passed is False
    assert any(
        failure.evaluator_key == "retrieval.recall_at_k"
        and "deterministic fixture outcome" in failure.reason
        for failure in result.failures
    )


def test_offline_regression_gate_default_is_provider_free(monkeypatch) -> None:
    before = set(sys.modules)

    def reject_network(*args, **kwargs):
        del args, kwargs
        raise AssertionError("offline regression gate attempted network access")

    monkeypatch.setattr(socket, "create_connection", reject_network)
    result = run_offline_regression_gate(repository_pytest_status="passed")
    imported_after = set(sys.modules) - before

    assert result.passed is True
    assert result.report is not None
    assert result.report.unavailable_integrations == ()
    assert not {
        name
        for name in imported_after
        if name == "cognee" or name.startswith(("cognee.", "openai.", "anthropic."))
    }


def test_offline_regression_gate_cli_writes_machine_readable_artifact(tmp_path: Path) -> None:
    output = tmp_path / "offline-regression-gate.json"

    try:
        main(["--repository-tests-passed", "--output", str(output)])
    except SystemExit as error:
        assert error.code == 0

    artifact = OfflineRegressionGateReport.model_validate_json(output.read_text())
    assert artifact.passed is True
    assert artifact.runs_completed == 2


def test_r1_gate_is_not_qualified_without_repository_pytest_evidence() -> None:
    result = run_offline_regression_gate()

    assert result.passed is False
    assert result.status == "not-qualified"
    assert result.repository_pytest_status == "unavailable"


def test_stale_applicability_manifest_evidence_is_not_qualified() -> None:
    from spine.evaluations.release_gates.applicability import (
        DEFAULT_R1_APPLICABILITY_MANIFEST,
    )

    record = {
        "policy_version": DEFAULT_R1_APPLICABILITY_MANIFEST.schema_version,
        "manifest_id": DEFAULT_R1_APPLICABILITY_MANIFEST.manifest_id,
        "manifest": DEFAULT_R1_APPLICABILITY_MANIFEST.model_dump(mode="json"),
        "mandatory_offline_suite": {
            "always_selected": True,
            "evidence_id": "offline-regression-gate:v1",
        },
        "provider_quality_confirmation": "not-confirmed",
    }

    assert _qualification_record_is_valid(
        "docs/qualification/issue-355-gate-applicability-report.json", record
    )
    record["manifest"] = {**record["manifest"], "manifest_id": "stale-manifest"}
    assert not _qualification_record_is_valid(
        "docs/qualification/issue-355-gate-applicability-report.json", record
    )


def test_selected_unavailable_provider_profile_cannot_be_reported_as_passed() -> None:
    result = run_offline_regression_gate(
        repository_pytest_status="passed",
        selected_provider_profiles=("cognee",),
    )

    profiles = {profile.profile: profile for profile in result.provider_profiles}
    assert result.status == "not-qualified"
    assert profiles["cognee"].status == "unavailable"
    assert profiles["llm"].status == "skipped"


def test_reported_retrieval_safety_violation_fails_gate_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import spine.evals.regression_gate as regression_gate

    monkeypatch.setattr(
        regression_gate,
        "_qualification_evidence",
        lambda *, mandatory_offline_passed: (
            QualificationEvidence(
                evidence_id="g02:execution:v1",
                source="synthetic-safety-report",
                status="failed",
            ),
        ),
    )

    result = run_offline_regression_gate(repository_pytest_status="passed")

    assert result.status == "failed"
    assert result.passed is False
