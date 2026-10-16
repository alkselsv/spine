from __future__ import annotations

import pytest
from pydantic import ValidationError
from uuid import UUID

from spine.evals.baseline_comparison import MetricDelta, compare_r1_reports
from spine.evals.offline_runner import (
    CaseDefinitionIdentity,
    ComponentVersions,
    FailureRecord,
    OfflineEvaluationReport,
    ScenarioResult,
    run_r1_evaluation_sync,
)
from spine.evaluations.grounded_answer_safety import (
    GroundedAnswerSafetyResult,
    GroundedAnswerSafetyViolation,
)


def test_component_version_changes_remain_comparable_and_have_deterministic_deltas() -> None:
    baseline = run_r1_evaluation_sync(
        component_versions=ComponentVersions(
            prompt_version="prompt:v1",
            model_profile_version="model:v1",
            retrieval_implementation_version="retriever:v1",
        )
    )
    candidate = run_r1_evaluation_sync(
        component_versions=ComponentVersions(
            prompt_version="prompt:v2",
            model_profile_version="model:v2",
            retrieval_implementation_version="retriever:v2",
        )
    )

    first = compare_r1_reports(baseline, candidate)
    second = compare_r1_reports(baseline, candidate)

    assert first.status == "comparable"
    assert first.baseline_component_versions != first.candidate_component_versions
    assert len(first.per_case_deltas) == len(baseline.evaluations)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert all(item.status in {
        "compared",
        "baseline_not_measured",
        "candidate_not_measured",
    } for item in first.per_case_deltas)


def test_case_changes_are_explicit_and_aggregate_denominators_are_visible() -> None:
    report = run_r1_evaluation_sync()
    case_ids = {item.case_id for item in report.case_definitions}
    existing = report.case_definitions[0]
    changed = existing.model_copy(update={"definition_digest": "0" * 64})
    added_id = "added-case"
    removed_id = report.case_definitions[-1].case_id
    added = CaseDefinitionIdentity(case_id=added_id, definition_digest="1" * 64)
    added_evaluation = report.evaluations[0].model_copy(
        update={"id": UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")}
    )
    added_scenario = ScenarioResult(
        scenario_id=added_id,
        case_id=added_id,
        scenario_tags=("added_case",),
        expected_outcome="answered",
        status="passed",
        evaluation_ids=(added_evaluation.id,),
    )
    candidate_payload = report.model_dump()
    candidate_payload["dataset"]["digest"] = "2" * 64
    candidate_payload["case_definitions"] = tuple(
        changed if item.case_id == existing.case_id
        else item
        for item in report.case_definitions
        if item.case_id != removed_id
    ) + (added,)
    removed_scenario = next(item for item in report.scenarios if item.case_id == removed_id)
    candidate_payload["evaluations"] = tuple(
        item for item in report.evaluations if item.id not in removed_scenario.evaluation_ids
    ) + (added_evaluation,)
    candidate_payload["scenarios"] = tuple(
        item for item in report.scenarios if item.case_id != removed_id
    ) + (added_scenario,)
    candidate = OfflineEvaluationReport.model_validate(candidate_payload)

    comparison = compare_r1_reports(report, candidate)

    assert comparison.status == "comparable"
    assert comparison.dataset_digest_changed is True
    assert comparison.changed_case_ids == (existing.case_id,)
    assert comparison.added_case_ids == (added_id,)
    assert comparison.removed_case_ids == (removed_id,)
    assert all(
        item.status == "case_definition_changed"
        for item in comparison.per_case_deltas
        if item.case_id == existing.case_id
    )
    assert any(
        item.case_id == added_id and item.status == "candidate_only"
        for item in comparison.per_case_deltas
    )
    assert any(
        item.case_id == removed_id and item.status == "baseline_only"
        for item in comparison.per_case_deltas
    )
    recall = next(
        item
        for item in comparison.aggregate_deltas
        if item.metric == "retrieval.recall_at_k:k=1"
    )
    assert recall.baseline_denominator == 7
    assert recall.candidate_denominator == 8
    assert len(case_ids) == len(report.case_definitions)


def test_metric_semantics_distinguish_recall_cutoffs() -> None:
    report = run_r1_evaluation_sync(recall_k_values=(1, 2))

    comparison = compare_r1_reports(report, report)

    recall_metrics = {
        item.metric
        for item in comparison.per_case_deltas
        if item.evaluator_key == "retrieval.recall_at_k"
    }
    assert recall_metrics == {
        "retrieval.recall_at_k:k=1",
        "retrieval.recall_at_k:k=2",
    }


def test_missing_baseline_and_incompatible_protocol_are_explicit() -> None:
    report = run_r1_evaluation_sync()
    missing = compare_r1_reports(None, report)
    incompatible = compare_r1_reports(
        report,
        report.model_copy(update={
            "configuration": report.configuration.model_copy(update={"recall_k_values": (1, 2)})
        }),
    )

    assert missing.status == "missing_baseline"
    assert incompatible.status == "incomparable"
    assert "evaluation_configuration_mismatch" in incompatible.incompatibilities
    assert incompatible.per_case_deltas == ()


def test_dataset_and_evaluator_semantic_changes_are_incomparable() -> None:
    report = run_r1_evaluation_sync()
    other_dataset = report.model_copy(update={
        "dataset": report.dataset.model_copy(update={"dataset_id": "other-dataset"})
    })
    other_evaluators = report.model_copy(update={
        "evaluator_catalog": report.evaluator_catalog[:-1]
    })
    changed_dataset_content = report.model_copy(update={
        "dataset": report.dataset.model_copy(update={"digest": "f" * 64})
    })
    legacy_payload = report.model_dump()
    legacy_payload["schema_version"] = "offline-evaluation-report:v1"
    legacy_payload["runner_version"] = "v1"
    legacy_payload["case_definitions"] = ()
    legacy_baseline = OfflineEvaluationReport.model_validate(legacy_payload)

    assert "dataset_identity_mismatch" in compare_r1_reports(report, other_dataset).incompatibilities
    assert "evaluator_semantics_mismatch" in compare_r1_reports(report, other_evaluators).incompatibilities
    assert "dataset_digest_mismatch" in compare_r1_reports(report, changed_dataset_content).incompatibilities
    legacy_comparison = compare_r1_reports(legacy_baseline, report)
    assert legacy_comparison.status == "incomparable"
    assert "report_schema_mismatch" in legacy_comparison.incompatibilities
    assert "case_definitions_missing" in legacy_comparison.incompatibilities


def test_failure_clusters_omit_protected_text_and_safety_failures_stay_separate() -> None:
    report = run_r1_evaluation_sync()
    protected = "Protected answer excerpt: confidential quarterly revenue"
    with pytest.raises(ValidationError):
        FailureRecord(
            scenario_id=report.scenarios[0].case_id,
            category="scenario_failure",
            component=protected,
            error_type="RuntimeError",
        )
    failure = FailureRecord.model_construct(
        scenario_id=report.scenarios[0].case_id,
        category="scenario_failure",
        component=protected,
        error_type="RuntimeError",
    )
    candidate = report.model_copy(update={
        "failures": (failure,),
        "grounded_answer_safety_results": (
            GroundedAnswerSafetyResult(
                case_id=report.scenarios[0].case_id,
                status="failed",
                violation_codes=(GroundedAnswerSafetyViolation.OBSERVATION_UNAVAILABLE,),
            ),
        ),
    })

    comparison = compare_r1_reports(report, candidate)
    serialized = comparison.model_dump_json()

    assert comparison.safety_gate_failed is True
    assert comparison.safety_failures
    assert protected not in serialized
    assert any(item.reason == "scenario_failure" for item in comparison.failure_clusters)


def test_duplicate_case_definitions_and_invalid_metric_values_are_rejected() -> None:
    report = run_r1_evaluation_sync()
    with pytest.raises(ValidationError):
        CaseDefinitionIdentity(
            case_id="same",
            definition_digest="not-a-digest",
        )
    with pytest.raises(ValidationError):
        OfflineEvaluationReport.model_validate({
            **report.model_dump(),
            "case_definitions": (report.case_definitions[0],) * 2,
        })
    with pytest.raises(ValidationError):
        MetricDelta(
            evaluator_key="qa.metric",
            evaluator_version="v1",
            metric="qa.metric",
            case_id="case-1",
            status="compared",
            baseline_score=1.4,
        )
