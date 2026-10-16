"""Deterministic CI gate for the synthetic R1 offline evaluation contract."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import hashlib
import json
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
from spine.evals.baseline_comparison import (
    BaselineComparisonReport,
    compare_r1_reports,
)
from spine.evaluations.release_gates.applicability import (
    DEFAULT_R1_APPLICABILITY_MANIFEST,
    ExecutionEvidence,
    ApplicabilityReport,
    build_r1_applicability_report,
)


OFFLINE_GATE_REPORT_SCHEMA_VERSION: Final[str] = "r1-gate-status:v1"
OFFLINE_GATE_VERSION: Final[str] = "v1"
GateStatus = Literal["passed", "failed", "skipped", "unavailable", "not-qualified"]
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
R1_BASELINE_REPORT_PATH: Final[Path] = Path("docs/qualification/r1-baseline-report.json")
R1_BASELINE_REPORT_SHA256: Final[str] = (
    "50964e810416fcef6857be1ecf13124fea0d053bbc0511a4980361ff122381e9"
)
R1_BASELINE_REPORT_ID: Final[str] = "f05c7bcd-5702-5d2c-a9f2-0dd86c2ba7f6"
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
    """Complete R1 gate artifact, including offline and qualification evidence."""

    schema_version: Literal[OFFLINE_GATE_REPORT_SCHEMA_VERSION] = (
        OFFLINE_GATE_REPORT_SCHEMA_VERSION
    )
    gate_version: Literal[OFFLINE_GATE_VERSION] = OFFLINE_GATE_VERSION
    qualification_scope: Literal[QUALIFICATION_SCOPE] = QUALIFICATION_SCOPE
    passed: bool
    production_approval: Literal[False] = False
    reproducible: bool
    runs_completed: int = Field(ge=0, le=2)
    status: GateStatus
    repository_pytest_status: Literal["passed", "failed", "unavailable"]
    dataset: DatasetIdentity | None = None
    evaluator_catalog: tuple[EvaluatorIdentity, ...] = ()
    report: OfflineEvaluationReport | None = None
    applicability: ApplicabilityReport | None = None
    baseline_comparison: BaselineComparisonReport | None = None
    qualification_evidence: tuple["QualificationEvidence", ...] = ()
    provider_profiles: tuple["ProviderProfileStatus", ...] = ()
    failures: tuple[RegressionGateFailure, ...] = ()


class QualificationEvidence(DefinitionModel):
    evidence_id: str = Field(min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=256)
    status: Literal["passed", "failed", "unavailable"]


class ProviderProfileStatus(DefinitionModel):
    profile: Literal["cognee", "llm"]
    status: Literal["skipped", "unavailable"]
    reason: str = Field(min_length=1, max_length=128)


ReportFactory = Callable[[], OfflineEvaluationReport]


def run_offline_regression_gate(
    *,
    report_factory: ReportFactory | None = None,
    repository_pytest_status: Literal["passed", "failed", "unavailable"] = "unavailable",
    selected_provider_profiles: tuple[str, ...] = (),
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
    mandatory_offline_passed = len(failures) == 0 and len(reports) == 2
    qualification_evidence = _qualification_evidence(
        mandatory_offline_passed=mandatory_offline_passed
    )
    evidence_by_id = {item.evidence_id: item for item in qualification_evidence}
    execution_evidence = tuple(
        ExecutionEvidence(
            evidence_id=evidence_id,
            suite_id=suite.suite_id,
            suite_version=suite.suite_version,
            manifest_id=DEFAULT_R1_APPLICABILITY_MANIFEST.manifest_id,
            manifest_version=DEFAULT_R1_APPLICABILITY_MANIFEST.schema_version,
            status=("passed" if evidence_by_id[evidence_id].status == "passed" else "unavailable"),
            component_versions=DEFAULT_R1_APPLICABILITY_MANIFEST.component_versions,
            evaluator_versions=DEFAULT_R1_APPLICABILITY_MANIFEST.evaluator_versions,
            dataset_versions=DEFAULT_R1_APPLICABILITY_MANIFEST.dataset_versions,
        )
        for suite in DEFAULT_R1_APPLICABILITY_MANIFEST.suites
        for evidence_id in suite.evidence_ids
        if evidence_id in evidence_by_id
    )
    applicability = build_r1_applicability_report(
        changed_files=(
            "src/spine/ingestion/parser.py",
            "src/spine/domain/source/models.py",
            "src/spine/memory/projection.py",
            "src/spine/memory/retrieval/adapter.py",
            "src/spine/auth/policy.py",
            "src/spine/agents/prompts/r1.md",
            "src/spine/models/profile.py",
            "src/spine/domain/capabilities/answer.py",
            "src/spine/evaluations/answer.py",
            "src/spine/evals/data/qa_r1.json",
            ".github/workflows/test.yml",
        ),
        execution_evidence=execution_evidence,
    )
    baseline = _load_r1_baseline()
    comparison = compare_r1_reports(baseline, report) if baseline and report else None
    if baseline is None:
        failures.append(
            RegressionGateFailure(
                category="integration",
                reason="pinned R1 baseline report is unavailable or invalid",
            )
        )
    provider_profiles = tuple(
        ProviderProfileStatus(
            profile=profile,
            status="unavailable" if profile in selected_provider_profiles else "skipped",
            reason=(
                "provider_profile_execution_not_configured"
                if profile in selected_provider_profiles
                else "profile_not_selected"
            ),
        )
        for profile in ("cognee", "llm")
    )
    status = _aggregate_status(
        mandatory_passed=len(failures) == 0 and len(reports) == 2,
        repository_pytest_status=repository_pytest_status,
        applicability_passed=applicability.passed,
        comparison=comparison,
        qualification_evidence=qualification_evidence,
        provider_profiles=provider_profiles,
    )
    if status != "passed" and not failures:
        failures.append(
            RegressionGateFailure(
                category="integration",
                reason=f"aggregate R1 qualification status is {status}",
            )
        )
    return OfflineRegressionGateReport(
        passed=status == "passed",
        reproducible=len(reports) == 2
        and not any(failure.category == "reproducibility" for failure in failures),
        runs_completed=len(reports),
        status=status,
        repository_pytest_status=repository_pytest_status,
        dataset=report.dataset if report is not None else None,
        evaluator_catalog=report.evaluator_catalog if report is not None else (),
        report=report,
        applicability=applicability,
        baseline_comparison=comparison,
        qualification_evidence=qualification_evidence,
        provider_profiles=provider_profiles,
        failures=tuple(failures),
    )


def _qualification_evidence(
    *, mandatory_offline_passed: bool
) -> tuple[QualificationEvidence, ...]:
    """Read prior parser, projection and R1 safety qualification records."""

    root = Path(__file__).parents[3]
    definitions = (
        ("issue-194:qualification:v1", "docs/qualification/issue-194-document-ingestion-report.json", "release_gate.status", "passed"),
        ("issue-90:qualification:v1", "docs/qualification/issue-90-source-projection-lifecycle-report.json", "release_gate.status", "qualified"),
        ("issue-211:qualification:v1", "docs/qualification/issue-211-context-projection-report.json", "release_gate.status", "passed"),
        ("g01:execution:v1", "docs/qualification/issue-356-unauthorized-retrieval-disclosure-report.json", "observation_seam", "r1-production-contract-observation:v1"),
        ("g02:execution:v1", "docs/qualification/issue-356-unauthorized-retrieval-disclosure-report.json", "status", "passed"),
        ("g03:execution:v1", "docs/qualification/issue-357-grounded-answers-report.json", "status", "qualified_offline"),
        ("g06:execution:v1", "docs/qualification/issue-354-context-adapter-parity-report.json", "release_gate.status", "passed"),
        ("r1-evaluator-catalog:v1", "docs/qualification/issue-357-grounded-answers-report.json", "status", "qualified_offline"),
        ("qa-r1-dataset:v1", "docs/qualification/issue-357-grounded-answers-report.json", "status", "qualified_offline"),
        ("r1-gate-applicability:v1", "docs/qualification/issue-355-gate-applicability-report.json", "status", "qualified_offline"),
        ("r1-baseline-comparison:v1", "docs/qualification/issue-358-baseline-comparison.md", "comparison_policy", "r1-baseline-comparison:v1"),
    )
    expected_schemas = {
        "docs/qualification/issue-194-document-ingestion-report.json": "document-ingestion-qualification:v1",
        "docs/qualification/issue-90-source-projection-lifecycle-report.json": "source-projection-lifecycle-qualification:v1",
        "docs/qualification/issue-211-context-projection-report.json": "context-projection-qualification:v1",
        "docs/qualification/issue-354-context-adapter-parity-report.json": "context-adapter-parity-qualification:v1",
        "docs/qualification/issue-355-gate-applicability-report.json": "r1-gate-applicability-qualification:v1",
        "docs/qualification/issue-356-unauthorized-retrieval-disclosure-report.json": "unauthorized-retrieval-disclosure-qualification:v1",
        "docs/qualification/issue-357-grounded-answers-report.json": "grounded-answer-qualification:v1",
    }
    evidence: list[QualificationEvidence] = []
    evidence.append(
        QualificationEvidence(
            evidence_id="offline-regression-gate:v1",
            source="spine-eval-gate:offline-r1",
            status="passed" if mandatory_offline_passed else "unavailable",
        )
    )
    for evidence_id, relative_path, status_path, expected_status in definitions:
        source = root / relative_path
        observed: object = None
        record: dict[str, object] | None = None
        try:
            if source.suffix == ".json":
                parsed = json.loads(source.read_text(encoding="utf-8"))
                if (
                    not isinstance(parsed, dict)
                    or parsed.get("schema_version") != expected_schemas[relative_path]
                    or not _qualification_record_is_valid(
                        relative_path, parsed
                    )
                ):
                    record = None
                else:
                    record = parsed
                    observed = record
                    for key in status_path.split("."):
                        observed = observed[key]  # type: ignore[index]
            elif status_path == "comparison_policy":
                observed = (
                    expected_status
                    if expected_status in source.read_text(encoding="utf-8")
                    else None
                )
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            pass
        evidence_status: Literal["passed", "failed", "unavailable"] = "unavailable"
        if record is not None and relative_path.endswith(
            "issue-356-unauthorized-retrieval-disclosure-report.json"
        ) and (
            record.get("safety_violation_count", 0) > 0
            or record.get("protected_payload_disclosure_count", 0) > 0
        ):
            evidence_status = "failed"
        if evidence_status != "failed":
            evidence_status = (
                "passed" if observed == expected_status else "unavailable"
            )
        evidence.append(
            QualificationEvidence(
                evidence_id=evidence_id,
                source=relative_path,
                status=evidence_status,
            )
        )
    return tuple(evidence)


def _qualification_record_is_valid(
    relative_path: str,
    record: dict[str, object],
) -> bool:
    """Reject stale or internally contradictory checked-in qualification evidence."""

    if (
        "production_approval" in record
        and record.get("production_approval") is not False
    ):
        return False
    criteria = record.get("criteria")
    if criteria is not None and (
        not isinstance(criteria, list)
        or not criteria
        or any(
            not isinstance(item, dict)
            or item.get("status") not in {"passed", "qualified"}
            for item in criteria
        )
    ):
        return False
    if relative_path.endswith("issue-356-unauthorized-retrieval-disclosure-report.json"):
        return (
            record.get("safety_violation_count") == 0
            and record.get("protected_payload_disclosure_count") == 0
            and record.get("qualification_scope") == "offline-contract-only"
        )
    if relative_path.endswith("issue-355-gate-applicability-report.json"):
        mandatory = record.get("mandatory_offline_suite")
        manifest = record.get("manifest")
        return (
            record.get("policy_version") == DEFAULT_R1_APPLICABILITY_MANIFEST.schema_version
            and record.get("manifest_id") == DEFAULT_R1_APPLICABILITY_MANIFEST.manifest_id
            and manifest == DEFAULT_R1_APPLICABILITY_MANIFEST.model_dump(mode="json")
            and isinstance(mandatory, dict)
            and mandatory.get("always_selected") is True
            and mandatory.get("evidence_id") == "offline-regression-gate:v1"
            and record.get("provider_quality_confirmation") == "not-confirmed"
        )
    if relative_path.endswith("issue-357-grounded-answers-report.json"):
        return (
            record.get("qualification_scope") == "offline-contract-only"
            and record.get("external_qualification")
            == {
                "real_cognee": "not_run",
                "real_llm": "not_run",
                "production_provider": "not_run",
            }
        )
    if relative_path.endswith("issue-354-context-adapter-parity-report.json"):
        release_gate = record.get("release_gate")
        return (
            isinstance(release_gate, dict)
            and release_gate.get("provider_qualification") == "not_approved"
        )
    if relative_path.endswith("issue-194-document-ingestion-report.json"):
        release_gate = record.get("release_gate")
        return isinstance(release_gate, dict) and release_gate.get("r1_release") == "not_approved"
    if relative_path.endswith("issue-90-source-projection-lifecycle-report.json"):
        release_gate = record.get("release_gate")
        return (
            isinstance(release_gate, dict)
            and release_gate.get("postgresql") == "mandatory_in_full_repository_gate"
        )
    if relative_path.endswith("issue-211-context-projection-report.json"):
        release_gate = record.get("release_gate")
        return (
            isinstance(release_gate, dict)
            and release_gate.get("provider_qualification") == "not_approved"
        )
    return True


def _load_r1_baseline() -> OfflineEvaluationReport | None:
    """Load the checked-in, versioned synthetic report used as the R1 baseline."""

    path = Path(__file__).parents[3] / R1_BASELINE_REPORT_PATH
    try:
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != R1_BASELINE_REPORT_SHA256:
            return None
        baseline = OfflineEvaluationReport.model_validate_json(payload)
    except (OSError, ValidationError):
        return None
    if (
        str(baseline.report_id) != R1_BASELINE_REPORT_ID
        or baseline.dataset != EXPECTED_R1_DATASET_IDENTITY
        or baseline.runner_version != OFFLINE_RUNNER_VERSION
        or baseline.evaluator_catalog
        != tuple(
            EvaluatorIdentity(evaluator_key=key, evaluator_version=version)
            for key, version in sorted(EXPECTED_EVALUATOR_VERSIONS.items())
        )
    ):
        return None
    return baseline


def _aggregate_status(
    *,
    mandatory_passed: bool,
    repository_pytest_status: Literal["passed", "failed", "unavailable"],
    applicability_passed: bool,
    comparison: BaselineComparisonReport | None,
    qualification_evidence: tuple[QualificationEvidence, ...],
    provider_profiles: tuple[ProviderProfileStatus, ...],
) -> GateStatus:
    if not mandatory_passed:
        return "failed"
    if repository_pytest_status == "failed":
        return "failed"
    if any(item.status == "failed" for item in qualification_evidence):
        return "failed"
    if any(item.status == "unavailable" for item in provider_profiles):
        return "not-qualified"
    if repository_pytest_status != "passed" or not applicability_passed or comparison is None:
        return "not-qualified"
    if comparison.status != "comparable" or comparison.safety_gate_failed:
        return "not-qualified"
    if any(item.status != "passed" for item in qualification_evidence):
        return "not-qualified"
    return "passed"


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
    repository_tests = parser.add_mutually_exclusive_group()
    repository_tests.add_argument(
        "--repository-tests-passed",
        dest="repository_pytest_status",
        action="store_const",
        const="passed",
        help="record that the mandatory full repository pytest suite completed successfully",
    )
    repository_tests.add_argument(
        "--repository-tests-failed",
        dest="repository_pytest_status",
        action="store_const",
        const="failed",
        help="record a failed mandatory full repository pytest suite",
    )
    repository_tests.add_argument(
        "--repository-tests-unavailable",
        dest="repository_pytest_status",
        action="store_const",
        const="unavailable",
        help="record that a mandatory full repository pytest prerequisite was unavailable",
    )
    parser.add_argument(
        "--provider-profile",
        action="append",
        choices=("cognee", "llm"),
        default=[],
        help="select an optional provider profile; unavailable profiles fail qualification",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    result = run_offline_regression_gate(
        repository_pytest_status=args.repository_pytest_status or "unavailable",
        selected_provider_profiles=tuple(args.provider_profile),
    )
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
    "ProviderProfileStatus",
    "QualificationEvidence",
    "RegressionGateFailure",
    "build_parser",
    "main",
    "run_offline_regression_gate",
]
