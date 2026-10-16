"""Versioned, disclosure-safe comparison of offline evaluation reports."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
import re
from typing import Final, Literal

from pydantic import Field

from spine.domain.common import DefinitionModel
from spine.domain.evaluations import EvaluationResult, EvaluationVerdict
from spine.evals.offline_runner import (
    CaseDefinitionIdentity,
    ComponentVersions,
    OfflineEvaluationReport,
)


R1_BASELINE_COMPARISON_VERSION: Final[str] = "r1-baseline-comparison:v1"


class MetricDelta(DefinitionModel):
    evaluator_key: str = Field(min_length=1, max_length=128)
    evaluator_version: str = Field(min_length=1, max_length=128)
    metric: str = Field(min_length=1, max_length=128)
    case_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    status: Literal[
        "compared",
        "baseline_missing",
        "candidate_missing",
        "baseline_not_measured",
        "candidate_not_measured",
        "baseline_only",
        "candidate_only",
        "case_definition_changed",
    ]
    baseline_score: float | None = Field(default=None, ge=0.0, le=1.0)
    candidate_score: float | None = Field(default=None, ge=0.0, le=1.0)
    delta: float | None = Field(default=None, ge=-1.0, le=1.0)
    baseline_verdict: Literal["pass", "fail", "review"] | None = None
    candidate_verdict: Literal["pass", "fail", "review"] | None = None


class MetricAggregateDelta(DefinitionModel):
    evaluator_key: str = Field(min_length=1, max_length=128)
    evaluator_version: str = Field(min_length=1, max_length=128)
    metric: str = Field(min_length=1, max_length=128)
    baseline_denominator: int = Field(ge=0)
    candidate_denominator: int = Field(ge=0)
    baseline_pass_count: int = Field(ge=0)
    candidate_pass_count: int = Field(ge=0)
    baseline_fail_count: int = Field(ge=0)
    candidate_fail_count: int = Field(ge=0)
    baseline_review_count: int = Field(ge=0)
    candidate_review_count: int = Field(ge=0)
    baseline_mean_score: float | None = Field(default=None, ge=0.0, le=1.0)
    candidate_mean_score: float | None = Field(default=None, ge=0.0, le=1.0)
    delta: float | None = Field(default=None, ge=-1.0, le=1.0)


class FailureCluster(DefinitionModel):
    """A grouped failure described only by stable, bounded identifiers."""

    evaluator_key: str = Field(min_length=1, max_length=128)
    metric: str = Field(min_length=1, max_length=128)
    outcome: str = Field(min_length=1, max_length=64)
    reason: Literal[
        "evaluation_failed",
        "evaluator_failure",
        "scenario_failure",
        "unavailable_integration",
        "safety_violation",
    ]
    component_version: str | None = Field(
        default=None, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
    )
    case_tags: tuple[str, ...] = ()
    safety_category: str | None = Field(default=None, max_length=64)
    case_ids: tuple[str, ...] = ()
    count: int = Field(ge=1)


class SafetyFailure(DefinitionModel):
    case_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    category: str = Field(min_length=1, max_length=64)
    violation_codes: tuple[str, ...] = ()


class BaselineComparisonReport(DefinitionModel):
    """Reproducible metric deltas, denominator accounting and safety failures."""

    schema_version: Literal[R1_BASELINE_COMPARISON_VERSION] = R1_BASELINE_COMPARISON_VERSION
    baseline_selection_policy_version: Literal[R1_BASELINE_COMPARISON_VERSION] = (
        R1_BASELINE_COMPARISON_VERSION
    )
    status: Literal["comparable", "incomparable", "missing_baseline"]
    incompatibilities: tuple[str, ...] = ()
    dataset_digest_changed: bool = False
    baseline_report_id: str | None = None
    candidate_report_id: str | None = None
    baseline_component_versions: ComponentVersions | None = None
    candidate_component_versions: ComponentVersions | None = None
    added_case_ids: tuple[str, ...] = ()
    removed_case_ids: tuple[str, ...] = ()
    changed_case_ids: tuple[str, ...] = ()
    per_case_deltas: tuple[MetricDelta, ...] = ()
    aggregate_deltas: tuple[MetricAggregateDelta, ...] = ()
    failure_clusters: tuple[FailureCluster, ...] = ()
    safety_failures: tuple[SafetyFailure, ...] = ()
    safety_gate_failed: bool = False


def compare_r1_reports(
    baseline: OfflineEvaluationReport | None,
    candidate: OfflineEvaluationReport,
) -> BaselineComparisonReport:
    """Compare two reports under r1-baseline-comparison:v1."""

    if baseline is None:
        return BaselineComparisonReport(
            status="missing_baseline",
            candidate_report_id=str(candidate.report_id),
            candidate_component_versions=candidate.component_versions,
            failure_clusters=_failure_clusters(candidate),
            safety_failures=_safety_failures(candidate),
            safety_gate_failed=_safety_gate_failed(candidate),
        )

    incompatibilities = _incompatibilities(baseline, candidate)
    base_cases = {item.case_id: item.definition_digest for item in baseline.case_definitions}
    candidate_cases = {item.case_id: item.definition_digest for item in candidate.case_definitions}
    base_ids = set(base_cases)
    candidate_ids = set(candidate_cases)
    added = tuple(sorted(candidate_ids - base_ids))
    removed = tuple(sorted(base_ids - candidate_ids))
    changed = tuple(sorted(
        case_id for case_id in base_ids & candidate_ids
        if base_cases[case_id] != candidate_cases[case_id]
    ))
    dataset_digest_changed = baseline.dataset.digest != candidate.dataset.digest

    compared_case_ids = tuple(sorted(base_ids | candidate_ids))
    if incompatibilities:
        return BaselineComparisonReport(
            status="incomparable",
            incompatibilities=tuple(incompatibilities),
            dataset_digest_changed=dataset_digest_changed,
            baseline_report_id=str(baseline.report_id),
            candidate_report_id=str(candidate.report_id),
            baseline_component_versions=baseline.component_versions,
            candidate_component_versions=candidate.component_versions,
            added_case_ids=added,
            removed_case_ids=removed,
            changed_case_ids=changed,
            failure_clusters=_failure_clusters(candidate),
            safety_failures=_safety_failures(candidate),
            safety_gate_failed=_safety_gate_failed(candidate),
        )

    per_case = _per_case_deltas(
        baseline,
        candidate,
        compared_case_ids,
        added_case_ids=set(added),
        removed_case_ids=set(removed),
        changed_case_ids=set(changed),
    )
    aggregates = _aggregate_deltas(per_case)
    return BaselineComparisonReport(
        status="comparable",
        dataset_digest_changed=dataset_digest_changed,
        baseline_report_id=str(baseline.report_id),
        candidate_report_id=str(candidate.report_id),
        baseline_component_versions=baseline.component_versions,
        candidate_component_versions=candidate.component_versions,
        added_case_ids=added,
        removed_case_ids=removed,
        changed_case_ids=changed,
        per_case_deltas=per_case,
        aggregate_deltas=aggregates,
        failure_clusters=_failure_clusters(candidate),
        safety_failures=_safety_failures(candidate),
        safety_gate_failed=_safety_gate_failed(candidate),
    )


def _incompatibilities(
    baseline: OfflineEvaluationReport, candidate: OfflineEvaluationReport
) -> list[str]:
    issues: list[str] = []
    if not baseline.case_definitions or not candidate.case_definitions:
        issues.append("case_definitions_missing")
    if (baseline.dataset.dataset_id, baseline.dataset.version) != (
        candidate.dataset.dataset_id,
        candidate.dataset.version,
    ):
        issues.append("dataset_identity_mismatch")
    if (baseline.dataset.schema_version, baseline.dataset.case_schema_version) != (
        candidate.dataset.schema_version,
        candidate.dataset.case_schema_version,
    ):
        issues.append("dataset_schema_mismatch")
    if baseline.schema_version != candidate.schema_version:
        issues.append("report_schema_mismatch")
    if baseline.runner_version != candidate.runner_version:
        issues.append("runner_version_mismatch")
    if (baseline.execution_mode, baseline.observation_seam) != (
        candidate.execution_mode,
        candidate.observation_seam,
    ):
        issues.append("execution_protocol_mismatch")
    if _protocol_configuration(baseline) != _protocol_configuration(candidate):
        issues.append("evaluation_configuration_mismatch")
    if (
        baseline.dataset.digest != candidate.dataset.digest
        and baseline.case_definitions == candidate.case_definitions
    ):
        issues.append("dataset_digest_mismatch")
    if baseline.evaluator_catalog != candidate.evaluator_catalog:
        issues.append("evaluator_semantics_mismatch")
    return issues


def _protocol_configuration(report: OfflineEvaluationReport) -> tuple[object, ...]:
    config = report.configuration
    return (
        config.retrieval_index_version,
        config.retrieval_strategy_versions,
        config.retrieval_evaluator_version,
        config.answer_configuration_version,
        config.citation_evaluator_version,
        config.scenario_configuration_version,
        config.recall_k_values,
    )


def _safety_gate_failed(report: OfflineEvaluationReport) -> bool:
    return any(
        item.status == "failed"
        for item in (*report.safety_results, *report.grounded_answer_safety_results)
    )


def _evaluation_map(
    report: OfflineEvaluationReport,
) -> dict[tuple[str, str, str, str], EvaluationResult]:
    mapped: dict[tuple[str, str, str, str], EvaluationResult] = {}
    case_by_result_id = {
        result_id: scenario.case_id
        for scenario in report.scenarios
        for result_id in scenario.evaluation_ids
    }
    for result in report.evaluations:
        case_id = case_by_result_id.get(result.id)
        if case_id is None:
            raise ValueError("evaluation result is not associated with a report scenario")
        key = (
            case_id,
            result.evaluator_key,
            result.evaluator_version,
            _metric_semantics(result),
        )
        if key in mapped:
            raise ValueError("report contains duplicate case/evaluator results")
        mapped[key] = result
    return mapped


def _per_case_deltas(
    baseline: OfflineEvaluationReport,
    candidate: OfflineEvaluationReport,
    case_ids: tuple[str, ...],
    *,
    added_case_ids: set[str],
    removed_case_ids: set[str],
    changed_case_ids: set[str],
) -> tuple[MetricDelta, ...]:
    base = _evaluation_map(baseline)
    current = _evaluation_map(candidate)
    keys = sorted(key for key in set(base) | set(current) if key[0] in case_ids)
    deltas: list[MetricDelta] = []
    for case_id, evaluator_key, evaluator_version, metric in keys:
        previous = base.get((case_id, evaluator_key, evaluator_version, metric))
        observed = current.get((case_id, evaluator_key, evaluator_version, metric))
        if case_id in changed_case_ids:
            status = "case_definition_changed"
        elif case_id in added_case_ids:
            status = "candidate_only"
        elif case_id in removed_case_ids:
            status = "baseline_only"
        elif previous is None:
            status, previous_score, observed_score = "baseline_missing", None, observed.score
        elif observed is None:
            status, previous_score, observed_score = "candidate_missing", previous.score, None
        elif previous.score is None:
            status, previous_score, observed_score = "baseline_not_measured", None, observed.score
        elif observed.score is None:
            status, previous_score, observed_score = "candidate_not_measured", previous.score, None
        else:
            status, previous_score, observed_score = "compared", previous.score, observed.score
        if case_id in changed_case_ids or case_id in added_case_ids or case_id in removed_case_ids:
            previous_score = previous.score if previous is not None else None
            observed_score = observed.score if observed is not None else None
        deltas.append(MetricDelta(
            evaluator_key=_safe_identifier(evaluator_key),
            evaluator_version=_safe_identifier(evaluator_version),
            metric=metric,
            case_id=case_id,
            status=status,
            baseline_score=previous_score,
            candidate_score=observed_score,
            delta=(observed_score - previous_score)
            if previous_score is not None and observed_score is not None
            and case_id not in changed_case_ids
            and case_id not in added_case_ids
            and case_id not in removed_case_ids
            else None,
            baseline_verdict=_verdict_value(previous),
            candidate_verdict=_verdict_value(observed),
        ))
    return tuple(deltas)


def _aggregate_deltas(deltas: tuple[MetricDelta, ...]) -> tuple[MetricAggregateDelta, ...]:
    groups: dict[tuple[str, str, str], list[MetricDelta]] = defaultdict(list)
    for item in deltas:
        groups[item.evaluator_key, item.evaluator_version, item.metric].append(item)
    result: list[MetricAggregateDelta] = []
    for (key, version, metric), items in sorted(groups.items()):
        base_values = [Decimal(str(item.baseline_score)) for item in items if item.baseline_score is not None]
        candidate_values = [Decimal(str(item.candidate_score)) for item in items if item.candidate_score is not None]
        base_mean = sum(base_values, Decimal(0)) / len(base_values) if base_values else None
        candidate_mean = sum(candidate_values, Decimal(0)) / len(candidate_values) if candidate_values else None
        result.append(MetricAggregateDelta(
            evaluator_key=_safe_identifier(key),
            evaluator_version=_safe_identifier(version),
            metric=metric,
            baseline_denominator=len(base_values),
            candidate_denominator=len(candidate_values),
            baseline_pass_count=sum(item.baseline_verdict == "pass" for item in items),
            candidate_pass_count=sum(item.candidate_verdict == "pass" for item in items),
            baseline_fail_count=sum(item.baseline_verdict == "fail" for item in items),
            candidate_fail_count=sum(item.candidate_verdict == "fail" for item in items),
            baseline_review_count=sum(item.baseline_verdict == "review" for item in items),
            candidate_review_count=sum(item.candidate_verdict == "review" for item in items),
            baseline_mean_score=float(base_mean) if base_mean is not None else None,
            candidate_mean_score=float(candidate_mean) if candidate_mean is not None else None,
            delta=float(candidate_mean - base_mean)
            if base_mean is not None and candidate_mean is not None else None,
        ))
    return tuple(result)


def _verdict_value(result: EvaluationResult | None) -> str | None:
    if result is None:
        return None
    return result.verdict.value


def _metric_semantics(result: EvaluationResult) -> str:
    if result.evaluator_key == "retrieval.recall_at_k":
        k = result.details.get("k")
        if isinstance(k, int) and not isinstance(k, bool) and k > 0:
            return f"retrieval.recall_at_k:k={k}"
        return "unclassified"
    return _safe_identifier(result.evaluator_key)


def _failure_clusters(report: OfflineEvaluationReport) -> tuple[FailureCluster, ...]:
    scenarios = {item.case_id: item for item in report.scenarios}
    versions = report.component_versions
    result_by_case_evaluator = {
        (case_id, item.evaluator_key, item.evaluator_version, _metric_semantics(item)): item
        for item in report.evaluations
        for case_id in scenarios
        if item.id in scenarios[case_id].evaluation_ids
    }
    clusters: dict[tuple[object, ...], set[str]] = defaultdict(set)
    for (case_id, evaluator, _version, metric), evaluation in result_by_case_evaluator.items():
        if evaluation.verdict is not EvaluationVerdict.FAIL:
            continue
        case = scenarios[case_id]
        component_version = _component_version(evaluator, versions)
        for tag in case.scenario_tags:
            outcome = _safe_outcome(evaluation.details.get("outcome"))
            key = (evaluator, metric, outcome, "evaluation_failed", component_version, (tag,), None)
            clusters[key].add(case_id)
    for failure in report.failures:
        scenario = scenarios.get(failure.scenario_id)
        tags = tuple(sorted(scenario.scenario_tags)) if scenario else ()
        safe_component = _safe_component(failure.component)
        key = (
            safe_component,
            safe_component,
            _safe_outcome(scenario.observed_outcome if scenario else None),
            failure.category,
            _component_version(safe_component, versions),
            tags,
            None,
        )
        clusters[key].add(failure.scenario_id)
    for safety in (*report.safety_results, *report.grounded_answer_safety_results):
        if safety.status != "failed":
            continue
        codes = tuple(sorted(code.value for code in safety.violation_codes))
        key = (
            "safety",
            "safety",
            "failed",
            "safety_violation",
            versions.retrieval_implementation_version,
            (),
            ",".join(codes) or "unspecified",
        )
        clusters[key].add(safety.case_id)
    result: list[FailureCluster] = []
    for key, case_ids in sorted(clusters.items(), key=lambda pair: str(pair[0])):
        evaluator, metric, outcome, reason, component_version, tags, safety_category = key
        result.append(FailureCluster(
            evaluator_key=_safe_identifier(str(evaluator)),
            metric=_safe_identifier(str(metric)),
            outcome=outcome,
            reason=reason,
            component_version=component_version,
            case_tags=tuple(_safe_identifier(tag) for tag in tags),
            safety_category=safety_category,
            case_ids=tuple(sorted(_safe_identifier(case_id) for case_id in case_ids)),
            count=len(case_ids),
        ))
    return tuple(result)


def _safe_identifier(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value) is None:
        return "unclassified"
    return value


def _safe_component(value: str) -> str:
    known_components = {
        "answer-construction",
        "evaluator",
        "evaluator-version",
        "grounded-answer-safety",
        "measurement",
        "production-contract-observation",
        "retrieval",
        "scenario-assertions",
        "unauthorized-retrieval-disclosure",
    }
    return value if value in known_components else "unclassified"


def _safe_outcome(value: object) -> str:
    allowed_outcomes = {
        "answered",
        "abstained",
        "denied",
        "clarification_required",
        "failed",
        "cancelled",
    }
    return value if isinstance(value, str) and value in allowed_outcomes else "failed"


def _component_version(evaluator_or_component: str, versions: ComponentVersions) -> str | None:
    if evaluator_or_component.startswith("retrieval"):
        return versions.retrieval_implementation_version
    if evaluator_or_component in {"answer-construction", "production-contract-observation"}:
        return versions.prompt_version or versions.model_profile_version
    if evaluator_or_component.startswith("qa."):
        return versions.model_profile_version or versions.prompt_version
    return None


def _safety_failures(report: OfflineEvaluationReport) -> tuple[SafetyFailure, ...]:
    failures: list[SafetyFailure] = []
    for category, results in (
        ("retrieval_disclosure", report.safety_results),
        ("grounded_answer", report.grounded_answer_safety_results),
    ):
        for item in results:
            if item.status == "failed":
                failures.append(SafetyFailure(
                    case_id=_safe_identifier(item.case_id),
                    category=category,
                    violation_codes=tuple(sorted(code.value for code in item.violation_codes)),
                ))
    return tuple(sorted(failures, key=lambda item: (item.category, item.case_id)))


__all__ = [
    "R1_BASELINE_COMPARISON_VERSION",
    "BaselineComparisonReport",
    "CaseDefinitionIdentity",
    "ComponentVersions",
    "FailureCluster",
    "MetricAggregateDelta",
    "MetricDelta",
    "SafetyFailure",
    "compare_r1_reports",
]
