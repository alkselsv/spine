from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from spine.evaluations.release_gates.applicability import (
    DEFAULT_R1_APPLICABILITY_MANIFEST,
    ExecutionEvidence,
    build_r1_applicability_report,
    classify_changed_files,
    run_r1_applicability_gate,
    validate_applicability_manifest,
)


@dataclass
class _OfflineResult:
    passed: bool = True


def _evidence(
    evidence_id: str,
    suite_id: str,
    suite_version: str = "v1",
) -> ExecutionEvidence:
    return ExecutionEvidence(
        evidence_id=evidence_id,
        suite_id=suite_id,
        suite_version=suite_version,
        manifest_id=DEFAULT_R1_APPLICABILITY_MANIFEST.manifest_id,
        manifest_version=DEFAULT_R1_APPLICABILITY_MANIFEST.schema_version,
        status="passed",
        component_versions=DEFAULT_R1_APPLICABILITY_MANIFEST.component_versions,
        evaluator_versions=DEFAULT_R1_APPLICABILITY_MANIFEST.evaluator_versions,
        dataset_versions=DEFAULT_R1_APPLICABILITY_MANIFEST.dataset_versions,
    )


def test_empty_change_set_still_runs_mandatory_offline_safety_suite() -> None:
    calls = 0

    def run_offline() -> _OfflineResult:
        nonlocal calls
        calls += 1
        return _OfflineResult()

    report = run_r1_applicability_gate(changed_files=(), run_offline_gate=run_offline)

    assert calls == 1
    assert report.passed is True
    assert report.mandatory_offline_executed is True
    assert report.selected_suite_ids == ("r1-offline-safety",)
    assert report.execution_evidence_ids == ("offline-regression-gate:v1",)


def test_parser_change_selects_versioned_parser_coverage_and_evidence() -> None:
    assert classify_changed_files(("src/spine/ingestion/pdf_parser.py",)) == (
        "parser_locator",
    )

    report = build_r1_applicability_report(
        changed_files=("src/spine/ingestion/pdf_parser.py",),
        execution_evidence=(
            _evidence("offline-regression-gate:v1", "r1-offline-safety"),
            _evidence("issue-194:qualification:v1", "issue-194-ingestion"),
        ),
    )

    assert report.passed is True
    assert report.selected_suite_ids == (
        "r1-offline-safety",
        "issue-194-ingestion",
    )
    assert [(suite.suite_id, suite.suite_version) for suite in report.selected_suites] == [
        ("r1-offline-safety", "v1"),
        ("issue-194-ingestion", "v1"),
    ]
    assert report.required_evidence_ids == (
        "offline-regression-gate:v1",
        "issue-194:qualification:v1",
    )


def test_specific_path_rules_win_over_broad_parent_rules() -> None:
    assert classify_changed_files(("src/spine/memory/retrieval/adapter.py",)) == ("retrieval",)
    assert classify_changed_files(("src/spine/evals/data/qa_r1.json",)) == ("dataset",)
    assert classify_changed_files(("src/spine/evals/applicability.py",)) == ("gate_report",)
    assert classify_changed_files(("src/spine/evaluations/release_gates/applicability.py",)) == (
        "gate_report",
    )


def test_manifest_cannot_replace_the_mandatory_offline_suite() -> None:
    weakened = DEFAULT_R1_APPLICABILITY_MANIFEST.model_copy(
        update={"mandatory_suite_id": "r1-dataset-contract"}
    )

    report = build_r1_applicability_report(
        changed_files=(),
        manifest=weakened,
        execution_evidence=(_evidence("offline-regression-gate:v1", "r1-offline-safety"),),
    )

    assert report.passed is False
    assert "r1-offline-safety" in report.selected_suite_ids
    assert any(failure.code == "inconsistent_manifest" for failure in report.failures)


def test_unmatched_changed_path_fails_closed_and_cannot_cancel_mandatory_suite() -> None:
    report = build_r1_applicability_report(
        changed_files=("src/spine/unknown_component.py",),
        execution_evidence=(_evidence("offline-regression-gate:v1", "r1-offline-safety"),),
    )

    assert report.passed is False
    assert report.mandatory_offline_executed is True
    assert report.selected_suite_ids == ("r1-offline-safety",)
    assert any(failure.code == "unknown_change_class" for failure in report.failures)


def test_stale_manifest_and_evaluator_catalog_fail_closed() -> None:
    stale = DEFAULT_R1_APPLICABILITY_MANIFEST.model_copy(
        update={
            "schema_version": "r1-gate-applicability:v0",
            "evaluator_versions": (
                {"evaluator_key": "qa.citations", "evaluator_version": "v999"},
            ),
        }
    )

    report = build_r1_applicability_report(
        changed_files=(),
        manifest=stale,
        execution_evidence=(),
    )

    assert report.passed is False
    assert {failure.code for failure in report.failures} >= {
        "stale_manifest",
        "evaluator_catalog_mismatch",
    }


def test_missing_component_version_pin_fails_closed() -> None:
    incomplete = DEFAULT_R1_APPLICABILITY_MANIFEST.model_copy(
        update={
            "component_versions": tuple(
                pin
                for pin in DEFAULT_R1_APPLICABILITY_MANIFEST.component_versions
                if pin.key not in {"prompt", "model", "capability", "evaluator", "dataset"}
            )
        }
    )

    failures = validate_applicability_manifest(incomplete)

    assert any(failure.code == "incomplete_manifest" for failure in failures)


def test_model_change_can_pass_offline_but_is_not_provider_quality_confirmation() -> None:
    report = build_r1_applicability_report(
        changed_files=("src/spine/agents/runtime/model_profile.py",),
        execution_evidence=(
            _evidence("offline-regression-gate:v1", "r1-offline-safety"),
        ),
    )

    assert report.passed is True
    assert report.provider_quality_status == "not-confirmed"
    assert report.selected_suite_ids == ("r1-offline-safety",)


def test_retrieval_change_is_not_provider_quality_confirmation() -> None:
    report = build_r1_applicability_report(
        changed_files=("src/spine/memory/retrieval/adapter.py",),
        execution_evidence=(
            _evidence("offline-regression-gate:v1", "r1-offline-safety"),
        ),
    )

    assert report.provider_quality_status == "not-confirmed"


def test_missing_required_execution_evidence_fails_closed() -> None:
    report = build_r1_applicability_report(
        changed_files=("src/spine/evaluations/citation_metrics.py",),
        execution_evidence=(_evidence("offline-regression-gate:v1", "r1-offline-safety"),),
    )

    assert report.passed is False
    assert any(failure.code == "missing_execution_evidence" for failure in report.failures)


def test_execution_evidence_requires_exact_version_pin_sets() -> None:
    with pytest.raises(ValidationError):
        ExecutionEvidence(
            evidence_id="offline-regression-gate:v1",
            suite_id="r1-offline-safety",
            suite_version="v1",
            manifest_id=DEFAULT_R1_APPLICABILITY_MANIFEST.manifest_id,
            manifest_version=DEFAULT_R1_APPLICABILITY_MANIFEST.schema_version,
            status="passed",
        )
