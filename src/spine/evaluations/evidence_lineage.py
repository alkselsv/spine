"""Deterministic evaluation for provider-neutral evidence-lineage decisions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from spine.application.evidence_lineage_verification import EvidenceVerificationOutcome
from spine.domain.common import DefinitionModel
from spine.evals.evidence_lineage_v1 import EvidenceLineageDataset


EVIDENCE_LINEAGE_EVALUATOR_VERSION = "v1"


class EvidenceLineageEvaluationReport(DefinitionModel):
    """Aggregate exact-decision and disclosure-safety metrics."""

    evaluator_version: Literal["v1"] = EVIDENCE_LINEAGE_EVALUATOR_VERSION
    dataset_id: str
    dataset_version: str
    case_count: int
    rejected_case_count: int
    exact_decision_rate: float
    safe_rejection_rate: float
    baseline_exact_decision_rate: float | None
    exact_decision_delta: float | None
    comparison_status: Literal["compared", "new_gate_no_baseline"]
    passed: bool


def evaluate_evidence_lineage_outcomes(
    outcomes: Mapping[str, EvidenceVerificationOutcome],
    *,
    dataset: EvidenceLineageDataset,
    baseline_exact_decision_rate: float | None = None,
) -> EvidenceLineageEvaluationReport:
    """Score exact decisions and ensure rejected output leaks no protected marker."""

    expected_ids = {case.case_id for case in dataset.cases}
    if set(outcomes) != expected_ids:
        raise ValueError("evaluation outcomes must match the dataset cases exactly")

    exact_count = 0
    safe_rejection_count = 0
    rejected_count = 0
    for case in dataset.cases:
        outcome = outcomes[case.case_id]
        reason_code = outcome.reason_code.value
        if outcome.status == case.expected_status and reason_code == case.expected_reason_code:
            exact_count += 1
        if case.expected_status == "rejected":
            rejected_count += 1
            serialized = outcome.model_dump_json()
            if all(marker not in serialized for marker in case.protected_markers):
                safe_rejection_count += 1

    case_count = len(dataset.cases)
    exact_rate = exact_count / case_count
    safe_rate = safe_rejection_count / rejected_count if rejected_count else 1.0
    delta = (
        exact_rate - baseline_exact_decision_rate
        if baseline_exact_decision_rate is not None
        else None
    )
    return EvidenceLineageEvaluationReport(
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.version,
        case_count=case_count,
        rejected_case_count=rejected_count,
        exact_decision_rate=exact_rate,
        safe_rejection_rate=safe_rate,
        baseline_exact_decision_rate=baseline_exact_decision_rate,
        exact_decision_delta=delta,
        comparison_status=(
            "compared"
            if baseline_exact_decision_rate is not None
            else "new_gate_no_baseline"
        ),
        passed=exact_rate == 1.0 and safe_rate == 1.0,
    )
