"""Deterministic CI gate for the synthetic R1 offline evaluation contract."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, ValidationError

from spine.domain.common import DefinitionModel
from spine.domain.evaluations import EvaluationResult, EvaluationVerdict
from spine.evals.offline_runner import (
    ANSWER_CONFIGURATION_VERSION,
    DatasetIdentity,
    EvaluatorIdentity,
    OfflineEvaluationReport,
    OFFLINE_REPORT_SCHEMA_VERSION,
    OFFLINE_RUNNER_VERSION,
    QUALIFICATION_SCOPE,
    REPLAY_CONTEXT_PROFILE,
    SCENARIO_CONFIGURATION_VERSION,
    run_r1_evaluation_sync,
)
from spine.evals.qa_r1 import QACase, QADataset, load_r1_dataset


OFFLINE_GATE_REPORT_SCHEMA_VERSION: Final[str] = "offline-regression-gate-report:v1"
OFFLINE_GATE_VERSION: Final[str] = "v1"
EXPECTED_R1_DATASET_IDENTITY: Final[DatasetIdentity] = DatasetIdentity(
    dataset_id="qa-r1-synthetic",
    version="1.0.0",
    digest="4f8ccc5202ea3471c0ffbc06dd2b534ecdc38cb498724e2da801181543778141",
)
EXPECTED_EVALUATOR_VERSIONS: Final[dict[str, str]] = {
    "retrieval.recall_at_k": "v1",
    "retrieval.mrr": "v1",
    "qa.answer_outcome": "v1",
    "qa.citations": "v1",
    "qa.citation_precision": "v1",
    "qa.citation_recall": "v1",
    "qa.locator_validity": "v1",
    "qa.groundedness": "v1",
}
_HARD_EVALUATOR_KEYS = frozenset(
    key for key in EXPECTED_EVALUATOR_VERSIONS if key.startswith("qa.")
)

GateFailureCategory = Literal[
    "schema",
    "dataset",
    "scenario",
    "evaluator",
    "reproducibility",
    "integration",
]


class RegressionGateFailure(DefinitionModel):
    """Disclosure-safe, machine-readable reason for one gate failure."""

    category: GateFailureCategory
    reason: str = Field(min_length=1, max_length=512)
    case_id: str | None = Field(default=None, min_length=1, max_length=128)
    evaluator_key: str | None = Field(default=None, min_length=1, max_length=128)
    evaluator_version: str | None = Field(default=None, min_length=1, max_length=128)


class OfflineRegressionGateReport(DefinitionModel):
    """Complete CI artifact for one two-pass offline regression gate."""

    schema_version: Literal[OFFLINE_GATE_REPORT_SCHEMA_VERSION] = (
        OFFLINE_GATE_REPORT_SCHEMA_VERSION
    )
    gate_version: Literal[OFFLINE_GATE_VERSION] = OFFLINE_GATE_VERSION
    qualification_scope: Literal[QUALIFICATION_SCOPE] = QUALIFICATION_SCOPE
    passed: bool
    reproducible: bool
    runs_completed: int = Field(ge=0, le=2)
    dataset: DatasetIdentity | None = None
    evaluator_catalog: tuple[EvaluatorIdentity, ...] = ()
    report: OfflineEvaluationReport | None = None
    failures: tuple[RegressionGateFailure, ...] = ()


ReportFactory = Callable[[], OfflineEvaluationReport]


def run_offline_regression_gate(
    *,
    report_factory: ReportFactory | None = None,
) -> OfflineRegressionGateReport:
    """Run the offline report twice and enforce hard, non-threshold invariants.

    The default factory uses only the checked-in synthetic dataset and the
    deterministic replay/evaluators. Provider-backed integrations are not part
    of this interface; they require a separate explicitly selected suite.
    """

    factory = report_factory or (
        lambda: run_r1_evaluation_sync(expected_dataset=EXPECTED_R1_DATASET_IDENTITY)
    )
    reports: list[OfflineEvaluationReport] = []
    failures: list[RegressionGateFailure] = []

    for _ in range(2):
        try:
            raw_report = factory()
            report = OfflineEvaluationReport.model_validate(
                raw_report.model_dump(mode="python")
            )
        except ValidationError as error:
            failures.append(
                RegressionGateFailure(
                    category="schema",
                    reason=f"offline report schema validation failed: {error.errors()[0]['type']}",
                )
            )
        except Exception as error:  # pragma: no cover - defensive CLI boundary
            failures.append(
                RegressionGateFailure(
                    category="dataset",
                    reason=f"offline run could not complete: {type(error).__name__}",
                )
            )
        else:
            reports.append(report)

    if len(reports) == 2 and reports[0].model_dump(mode="json") != reports[1].model_dump(mode="json"):
        failures.append(
            RegressionGateFailure(
                category="reproducibility",
                reason="two offline runs produced different substantive reports",
            )
        )

    report = reports[0] if reports else None
    if report is not None:
        failures.extend(_validate_report(report))

    failures = _deduplicate_failures(failures)
    return OfflineRegressionGateReport(
        passed=len(failures) == 0 and len(reports) == 2,
        reproducible=len(reports) == 2
        and not any(failure.category == "reproducibility" for failure in failures),
        runs_completed=len(reports),
        dataset=report.dataset if report is not None else None,
        evaluator_catalog=report.evaluator_catalog if report is not None else (),
        report=report,
        failures=tuple(failures),
    )


def _validate_report(report: OfflineEvaluationReport) -> list[RegressionGateFailure]:
    failures: list[RegressionGateFailure] = []
    if report.schema_version != OFFLINE_REPORT_SCHEMA_VERSION:
        failures.append(
            RegressionGateFailure(
                category="schema",
                reason="offline report schema version is not pinned to the gate",
            )
        )
    if report.runner_version != OFFLINE_RUNNER_VERSION:
        failures.append(
            RegressionGateFailure(
                category="schema",
                reason="offline runner version is not pinned to the gate",
            )
        )
    if report.qualification_scope != QUALIFICATION_SCOPE:
        failures.append(
            RegressionGateFailure(
                category="integration",
                reason="report is outside the offline-contract qualification scope",
            )
        )
    if report.dataset != EXPECTED_R1_DATASET_IDENTITY:
        failures.append(
            RegressionGateFailure(
                category="dataset",
                reason="dataset identity changed without updating the pinned version metadata",
            )
        )

    expected_catalog = tuple(
        EvaluatorIdentity(evaluator_key=key, evaluator_version=version)
        for key, version in sorted(EXPECTED_EVALUATOR_VERSIONS.items())
    )
    if report.evaluator_catalog != expected_catalog:
        failures.append(
            RegressionGateFailure(
                category="schema",
                reason="evaluator catalog/version metadata does not match the gate",
            )
        )

    try:
        dataset = load_r1_dataset()
    except Exception as error:  # pragma: no cover - loader boundary
        failures.append(
            RegressionGateFailure(
                category="dataset",
                reason=f"synthetic dataset integrity validation failed: {type(error).__name__}",
            )
        )
        return failures

    expected_strategy_versions = tuple(
        sorted({case.retrieval_strategy for case in dataset.cases})
    )
    configuration = report.configuration
    if (
        configuration.retrieval_index_version != REPLAY_CONTEXT_PROFILE
        or configuration.retrieval_strategy_versions != expected_strategy_versions
        or configuration.retrieval_evaluator_version != "v1"
        or configuration.answer_configuration_version != ANSWER_CONFIGURATION_VERSION
        or configuration.citation_evaluator_version != "v1"
        or configuration.scenario_configuration_version != SCENARIO_CONFIGURATION_VERSION
        or configuration.recall_k_values != (1,)
    ):
        failures.append(
            RegressionGateFailure(
                category="schema",
                reason="evaluation configuration/version metadata does not match the gate",
            )
        )

    expected_cases = {case.case_id: case for case in dataset.cases}
    observed_cases = {scenario.case_id: scenario for scenario in report.scenarios}
    for case_id in sorted(set(expected_cases) - set(observed_cases)):
        failures.append(
            RegressionGateFailure(
                category="scenario",
                case_id=case_id,
                reason="dataset case is missing from the offline report",
            )
        )
    for case_id in sorted(set(observed_cases) - set(expected_cases)):
        failures.append(
            RegressionGateFailure(
                category="scenario",
                case_id=case_id,
                reason="offline report contains an unknown dataset case",
            )
        )

    results_by_id = {result.id: result for result in report.evaluations}
    if len(results_by_id) != len(report.evaluations):
        failures.append(
            RegressionGateFailure(
                category="schema",
                reason="evaluation result identifiers are not unique",
            )
        )

    for case_id, case in expected_cases.items():
        scenario = observed_cases.get(case_id)
        if scenario is None:
            continue
        if scenario.expected_outcome != case.expected_outcome:
            failures.append(
                RegressionGateFailure(
                    category="scenario",
                    case_id=case_id,
                    reason="reported expected outcome differs from the dataset contract",
                )
            )
        if scenario.status != "passed" or scenario.observed_outcome != case.expected_outcome:
            failures.append(
                RegressionGateFailure(
                    category="scenario",
                    case_id=case_id,
                    reason="scenario did not produce its expected answer or abstention outcome",
                )
            )
        if scenario.observed_evidence_ids != case.expected_evidence_ids:
            failures.append(
                RegressionGateFailure(
                    category="scenario",
                    case_id=case_id,
                    reason="retrieval evidence differs from the dataset contract",
                )
            )

        case_results = [results_by_id.get(result_id) for result_id in scenario.evaluation_ids]
        if any(result is None for result in case_results):
            failures.append(
                RegressionGateFailure(
                    category="schema",
                    case_id=case_id,
                    reason="scenario references a missing evaluation result",
                )
            )
            continue
        concrete_results = tuple(result for result in case_results if result is not None)
        observed_evaluators = {result.evaluator_key for result in concrete_results}
        missing_evaluators = set(EXPECTED_EVALUATOR_VERSIONS) - observed_evaluators
        if missing_evaluators:
            failures.append(
                RegressionGateFailure(
                    category="schema",
                    case_id=case_id,
                    reason="scenario is missing registered evaluator results: "
                    + ", ".join(sorted(missing_evaluators)),
                )
            )
        for result in concrete_results:
            failures.extend(_validate_evaluation(case_id, case, result, report))

    if report.failures:
        for recorded in report.failures:
            category: GateFailureCategory = (
                "integration"
                if recorded.category == "unavailable_integration"
                else "evaluator"
                if recorded.category == "evaluator_failure"
                else "scenario"
            )
            failures.append(
                RegressionGateFailure(
                    category=category,
                    case_id=recorded.scenario_id,
                    reason=(
                        f"runner recorded {recorded.category} in {recorded.component} "
                        f"({recorded.error_type})"
                    ),
                )
            )
    for scenario_id in report.unavailable_integrations:
        failures.append(
            RegressionGateFailure(
                category="integration",
                case_id=scenario_id,
                reason="offline report contains an unavailable integration",
            )
        )
    return failures


def _validate_evaluation(
    case_id: str,
    case: QACase,
    result: EvaluationResult,
    report: OfflineEvaluationReport,
) -> list[RegressionGateFailure]:
    failures: list[RegressionGateFailure] = []
    expected_version = EXPECTED_EVALUATOR_VERSIONS.get(result.evaluator_key)
    if expected_version is None:
        failures.append(
            RegressionGateFailure(
                category="schema",
                case_id=case_id,
                evaluator_key=result.evaluator_key,
                evaluator_version=result.evaluator_version,
                reason="evaluation uses an evaluator not registered by the offline gate",
            )
        )
    elif result.evaluator_version != expected_version:
        failures.append(
            RegressionGateFailure(
                category="evaluator",
                case_id=case_id,
                evaluator_key=result.evaluator_key,
                evaluator_version=result.evaluator_version,
                reason="evaluator version differs from the pinned gate metadata",
            )
        )

    if result.evaluator_key.startswith("retrieval."):
        if result.details.get("index_version") not in {
            None,
            report.configuration.retrieval_index_version,
        }:
            failures.append(
                RegressionGateFailure(
                    category="evaluator",
                    case_id=case_id,
                    evaluator_key=result.evaluator_key,
                    evaluator_version=result.evaluator_version,
                    reason="retrieval evaluator used an unexpected index version",
                )
            )
        if result.details.get("status") not in {"measured", "not_applicable"}:
            failures.append(
                RegressionGateFailure(
                    category="evaluator",
                    case_id=case_id,
                    evaluator_key=result.evaluator_key,
                    evaluator_version=result.evaluator_version,
                    reason="retrieval evaluator did not produce a deterministic status",
                )
            )

    if result.verdict is EvaluationVerdict.FAIL:
        failures.append(
            RegressionGateFailure(
                category="evaluator",
                case_id=case_id,
                evaluator_key=result.evaluator_key,
                evaluator_version=result.evaluator_version,
                reason=_evaluation_failure_reason(result),
            )
        )
    elif (
        result.evaluator_key in _HARD_EVALUATOR_KEYS
        and result.details.get("deterministic_gate") != "passed"
    ):
        failures.append(
            RegressionGateFailure(
                category="evaluator",
                case_id=case_id,
                evaluator_key=result.evaluator_key,
                evaluator_version=result.evaluator_version,
                reason="citation, locator, groundedness or answer-outcome hard check failed",
            )
        )
    if result.evaluator_key == "retrieval.recall_at_k":
        failures.extend(_validate_recall_metric(case_id, case, result))
    elif result.evaluator_key == "retrieval.mrr":
        failures.extend(_validate_mrr_metric(case_id, case, result))
    return failures


def _validate_recall_metric(
    case_id: str,
    case: QACase,
    result: EvaluationResult,
) -> list[RegressionGateFailure]:
    """Check exact fixture-derived recall without introducing a quality threshold."""

    # The scenario evidence identity check runs before evaluator checks.  Once
    # it passes, the pinned dataset's expected IDs are the safe, normalized
    # ranking contract rather than an untrusted duplicate reference list.
    observed_evidence_ids = case.expected_evidence_ids
    details = result.details
    k = details.get("k")
    if not isinstance(k, int) or isinstance(k, bool) or k < 1:
        return [
            RegressionGateFailure(
                category="evaluator",
                case_id=case_id,
                evaluator_key=result.evaluator_key,
                evaluator_version=result.evaluator_version,
                reason="retrieval recall evaluator did not report a valid k",
            )
        ]
    relevant_ids = {
        label.evidence_id for label in case.relevance_labels if label.label == "relevant"
    }
    if case.expected_outcome != "answered" or not relevant_ids:
        expected_score: float | None = None
        expected_status = "not_applicable"
    else:
        expected_score = len(set(observed_evidence_ids[:k]) & relevant_ids) / len(relevant_ids)
        expected_status = "measured"
    if result.score != expected_score or details.get("status") != expected_status:
        return [
            RegressionGateFailure(
                category="evaluator",
                case_id=case_id,
                evaluator_key=result.evaluator_key,
                evaluator_version=result.evaluator_version,
                reason="retrieval recall does not match the deterministic fixture outcome",
            )
        ]
    return []


def _validate_mrr_metric(
    case_id: str,
    case: QACase,
    result: EvaluationResult,
) -> list[RegressionGateFailure]:
    """Check exact fixture-derived MRR without introducing a quality threshold."""

    relevant_ids = {
        label.evidence_id for label in case.relevance_labels if label.label == "relevant"
    }
    if case.expected_outcome != "answered" or not relevant_ids:
        expected_score: float | None = None
        expected_status = "not_applicable"
    else:
        first_rank = next(
            (
                rank
                for rank, evidence_id in enumerate(case.expected_evidence_ids, start=1)
                if evidence_id in relevant_ids
            ),
            None,
        )
        expected_score = 0.0 if first_rank is None else 1.0 / first_rank
        expected_status = "measured"
    if result.score != expected_score or result.details.get("status") != expected_status:
        return [
            RegressionGateFailure(
                category="evaluator",
                case_id=case_id,
                evaluator_key=result.evaluator_key,
                evaluator_version=result.evaluator_version,
                reason="retrieval MRR does not match the deterministic fixture outcome",
            )
        ]
    return []


def _evaluation_failure_reason(result: EvaluationResult) -> str:
    failure_counts = result.details.get("failure_counts")
    if isinstance(failure_counts, dict) and failure_counts:
        return "deterministic evaluator rejected the result: " + ", ".join(
            f"{key}={failure_counts[key]}" for key in sorted(failure_counts)
        )
    reason = result.details.get("reason")
    if isinstance(reason, str) and reason:
        return f"deterministic evaluator rejected the result: {reason}"
    return "deterministic evaluator returned fail"


def _deduplicate_failures(
    failures: list[RegressionGateFailure],
) -> list[RegressionGateFailure]:
    unique: dict[tuple[object, ...], RegressionGateFailure] = {}
    for failure in failures:
        key = (
            failure.category,
            failure.case_id,
            failure.evaluator_key,
            failure.evaluator_version,
            failure.reason,
        )
        unique.setdefault(key, failure)
    return list(unique.values())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Spine's deterministic offline regression gate twice"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional path for the machine-readable JSON gate artifact",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    result = run_offline_regression_gate()
    payload = result.model_dump_json(indent=2) + "\n"
    if args.output is not None:
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    raise SystemExit(0 if result.passed else 1)


if __name__ == "__main__":
    main()


__all__ = [
    "EXPECTED_EVALUATOR_VERSIONS",
    "EXPECTED_R1_DATASET_IDENTITY",
    "OFFLINE_GATE_REPORT_SCHEMA_VERSION",
    "OFFLINE_GATE_VERSION",
    "OfflineRegressionGateReport",
    "RegressionGateFailure",
    "build_parser",
    "main",
    "run_offline_regression_gate",
]
