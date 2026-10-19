from __future__ import annotations

from datetime import datetime, timezone

from spine.evals.real_baseline import BaselineCaseInput, BaselineMetricInput, build_r1_baseline_report
from spine.evaluations.release_gates.thresholds import (
    MandatorySafetyResult,
    MetricThreshold,
    PolicyApproval,
    ThresholdEvidence,
    ThresholdPolicy,
    evaluate_threshold_policy,
    policy_digest,
)


def _evidence(
    *,
    value: float = 0.9,
    coverage: str = "complete",
    metric: str = "qa.answer_outcome",
    failure_counts: dict[str, int] | None = None,
) -> ThresholdEvidence:
    measured = 149 if coverage == "complete" else 148
    baseline = build_r1_baseline_report(
        cases=tuple(
            BaselineCaseInput(
                case_key=f"private-case-{index}",
                source_formats=("pdf",),
                expected_outcome="answered",
                observation_status="measured",
                metric_eligibility=(
                    {"qa.answer_outcome": 149, "qa.denial_quality": 0}
                    if index == 0
                    else {}
                ),
                metrics=(
                    BaselineMetricInput(
                        metric=metric,
                        version="v1",
                        value=value,
                        eligible_count=measured,
                        measured_denominator=1,
                        matched_count=1,
                    ),
                ) if metric == "qa.answer_outcome" and index == 0 else (),
            )
            for index in range(149)
        ),
        observer_latencies_ms=(),
        worker_latencies_ns=(),
        observer_attempt_count=0,
        failure_counts=failure_counts,
    )
    return ThresholdEvidence(
        evidence_version="baseline-run-7",
        dataset_id="qa-r1-real-approved",
        dataset_version=baseline.dataset_version,
        dataset_digest=baseline.dataset_sha256,
        runtime_identity="worker-build-abc",
        runtime_scope="r1-production-profile",
        baseline=baseline,
    )


def _policy(
    *, threshold: float = 0.8, approved: bool = True, metric: str = "qa.answer_outcome",
    direction: str = "minimum", comparison: str = "gte", minimum_coverage: float = 0.5,
    optional_denial_metric: bool = False,
) -> ThresholdPolicy:
    metrics = [
        MetricThreshold(
            metric=metric,
            evidence_kind="metric",
            direction=direction,
            comparison=comparison,
            statistic="weighted_mean",
            denominator="measured_denominator",
            expected_eligible_count=149 if metric == "qa.answer_outcome" else 0,
            applicability="required",
            minimum_coverage=minimum_coverage,
            threshold=threshold,
        )
    ]
    evaluator_versions = [(metric, "v1")]
    if optional_denial_metric:
        metrics.append(
            MetricThreshold(
                metric="qa.denial_quality",
                evidence_kind="metric",
                direction="minimum",
                comparison="gte",
                statistic="weighted_mean",
                denominator="measured_denominator",
                expected_eligible_count=0,
                applicability="optional",
                minimum_coverage=0,
                threshold=0.5,
            )
        )
        evaluator_versions.append(("qa.denial_quality", "v1"))
    policy = ThresholdPolicy(
        policy_id="r1-quality-policy",
        version="v1",
        dataset_id="qa-r1-real-approved",
        dataset_version="0.3.9-user-approved",
        dataset_digest="7c2787272fd34643ff5168a46f5672b5a091f3070888546fa7b6ed0c918b3a62",
        metric_evaluator_versions=tuple(evaluator_versions),
        runtime_identity="worker-build-abc",
        runtime_scope="r1-production-profile",
        metrics=tuple(metrics),
        state="draft",
    )
    if not approved:
        return policy
    return policy.model_copy(
        update={
            "state": "approved",
            "approval": PolicyApproval(
                approval_id="approval-42",
                approver_id="product-owner-1",
                authority="product_owner",
                approval_version="v1",
                approved_at=datetime(2026, 10, 19, tzinfo=timezone.utc),
                policy_digest=policy_digest(policy),
            ),
        }
    )


def _safety(status: str = "passed") -> tuple[MandatorySafetyResult, ...]:
    return (
        MandatorySafetyResult(gate_id="hard-safety", status=status),  # type: ignore[arg-type]
    )


def test_approved_compatible_policy_passes_at_exact_boundary_deterministically() -> None:
    policy = _policy(threshold=0.9)
    evidence = _evidence(value=0.9)

    first = evaluate_threshold_policy(
        policy=policy,
        evidence=evidence,
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )
    second = evaluate_threshold_policy(
        policy=policy,
        evidence=evidence,
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )

    assert first == second
    assert first.status == "passed"
    assert first.metrics[0].status == "passed"
    assert first.metrics[0].applied_threshold == 0.9
    assert first.metrics[0].denominator == 1
    assert first.metrics[0].eligible_count == 149


def test_draft_policy_is_not_approved_and_cannot_pass() -> None:
    result = evaluate_threshold_policy(
        policy=_policy(approved=False),
        evidence=_evidence(),
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )

    assert result.status == "not_approved"
    assert {metric.status for metric in result.metrics} == {"not_approved"}


def test_missing_safety_result_blocks_quality_evaluation() -> None:
    result = evaluate_threshold_policy(
        policy=_policy(),
        evidence=_evidence(),
        safety_results=(),
        required_safety_gates=("hard-safety",),
    )

    assert result.status == "unavailable"
    assert result.metrics[0].status == "unavailable"
    assert result.reason_codes == ("mandatory_safety_missing",)


def test_failed_safety_result_blocks_quality_evaluation() -> None:
    result = evaluate_threshold_policy(
        policy=_policy(),
        evidence=_evidence(),
        safety_results=_safety("failed"),
        required_safety_gates=("hard-safety",),
    )

    assert result.status == "failed"
    assert result.metrics[0].status == "unavailable"
    assert result.reason_codes == ("mandatory_safety_failed",)


def test_empty_or_conflicting_mandatory_safety_set_cannot_be_omitted() -> None:
    empty = evaluate_threshold_policy(
        policy=_policy(), evidence=_evidence(), safety_results=(), required_safety_gates=()
    )
    conflicting = evaluate_threshold_policy(
        policy=_policy(),
        evidence=_evidence(),
        safety_results=(
            MandatorySafetyResult(gate_id="hard-safety", status="passed"),
            MandatorySafetyResult(gate_id="hard-safety", status="failed"),
        ),
        required_safety_gates=("hard-safety",),
    )
    assert empty.status == "unavailable"
    assert empty.reason_codes == ("mandatory_safety_missing",)
    assert conflicting.status == "failed"


def test_zero_eligible_denied_metric_is_not_applicable() -> None:
    result = evaluate_threshold_policy(
        policy=_policy(metric="qa.denial_quality", minimum_coverage=0.0),
        evidence=_evidence(metric="qa.denial_quality"),
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )

    assert result.status == "unavailable"
    assert result.metrics[0].status == "not_applicable"


def test_optional_not_applicable_metric_does_not_replace_required_decision() -> None:
    result = evaluate_threshold_policy(
        policy=_policy(optional_denial_metric=True),
        evidence=_evidence(),
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )
    assert result.status == "passed"
    assert result.metrics[0].status == "passed"
    assert result.metrics[1].status == "not_applicable"


def test_policy_identity_digest_excludes_approval_and_is_stable() -> None:
    policy = _policy(approved=False)
    assert policy_digest(policy) == policy_digest(policy)


def test_wrong_approval_authority_is_not_approved() -> None:
    policy = _policy()
    invalid_approval = policy.approval.model_copy(update={"authority": "operator"})
    result = evaluate_threshold_policy(
        policy=policy.model_copy(update={"approval": invalid_approval}),
        evidence=_evidence(),
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )
    assert result.status == "not_approved"


def test_below_minimum_threshold_fails() -> None:
    result = evaluate_threshold_policy(
        policy=_policy(threshold=0.91),
        evidence=_evidence(value=0.9),
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )
    assert result.status == "failed"
    assert result.metrics[0].status == "failed"


def test_metadata_result_does_not_include_private_case_keys() -> None:
    result = evaluate_threshold_policy(
        policy=_policy(),
        evidence=_evidence(),
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )

    serialized = result.model_dump_json()
    assert "private-case-0" not in serialized


def test_maximum_threshold_passes_on_exact_boundary() -> None:
    result = evaluate_threshold_policy(
        policy=_policy(threshold=0.9, direction="maximum", comparison="lte"),
        evidence=_evidence(value=0.9),
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )
    assert result.status == "passed"


def test_partial_metric_population_below_required_coverage_is_insufficient() -> None:
    result = evaluate_threshold_policy(
        policy=_policy(minimum_coverage=1.0),
        evidence=_evidence(coverage="partial"),
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )
    assert result.status == "unavailable"
    assert result.metrics[0].status == "insufficient_evidence"
    assert result.metrics[0].reason_code == "coverage_below_minimum"


def test_partial_metric_observations_cannot_pass_even_with_low_minimum_coverage() -> None:
    result = evaluate_threshold_policy(
        policy=_policy(minimum_coverage=0.0),
        evidence=_evidence(coverage="partial"),
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )
    assert result.status == "unavailable"
    assert result.metrics[0].status == "insufficient_evidence"
    assert result.metrics[0].reason_code == "partial_observation"


def test_failed_observation_cluster_blocks_positive_quality_decision() -> None:
    result = evaluate_threshold_policy(
        policy=_policy(),
        evidence=_evidence(failure_counts={"evaluator_failure": 1}),
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )
    assert result.status == "unavailable"
    assert result.metrics[0].status == "unavailable"
    assert "failed_observation" in result.reason_codes


def test_incompatible_dataset_or_runtime_pins_cannot_pass() -> None:
    evidence = _evidence().model_copy(update={"runtime_scope": "other-scope"})
    result = evaluate_threshold_policy(
        policy=_policy(),
        evidence=evidence,
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )
    assert result.status == "unavailable"
    assert "runtime_pin_mismatch" in result.reason_codes

    changed_dataset = _evidence().model_copy(update={"dataset_id": "other-dataset"})
    dataset_result = evaluate_threshold_policy(
        policy=_policy(), evidence=changed_dataset, safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )
    assert "dataset_pin_mismatch" in dataset_result.reason_codes


def test_incompatible_evaluator_pin_cannot_pass() -> None:
    evidence = _evidence()
    changed_metric = evidence.baseline.metrics[0].model_copy(update={"version": "v2"})
    changed_baseline = evidence.baseline.model_copy(update={"metrics": (changed_metric,)})
    changed_evidence = evidence.model_copy(update={"baseline": changed_baseline})
    result = evaluate_threshold_policy(
        policy=_policy(),
        evidence=changed_evidence,
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )
    assert result.status == "unavailable"
    assert result.metrics[0].reason_code == "evaluator_pin_mismatch"


def test_incomplete_dataset_population_cannot_pass() -> None:
    evidence = _evidence()
    partial_baseline = evidence.baseline.model_copy(
        update={
            "eligible_case_count": 148,
            "measured_case_count": 148,
            "unavailable_case_count": 0,
        }
    )
    partial_evidence = evidence.model_copy(update={"baseline": partial_baseline})
    result = evaluate_threshold_policy(
        policy=_policy(),
        evidence=partial_evidence,
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )
    assert result.status == "unavailable"
    assert "population_incomplete" in result.reason_codes


def test_decision_required_threshold_never_passes() -> None:
    result = evaluate_threshold_policy(
        policy=_policy(threshold=0.8, approved=False).model_copy(
            update={"metrics": (_policy(approved=False).metrics[0].model_copy(update={"threshold": None}),)}
        ),
        evidence=_evidence(),
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )
    assert result.status == "not_approved"
