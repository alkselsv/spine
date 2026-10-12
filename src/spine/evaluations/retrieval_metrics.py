"""Deterministic offline metrics for ranked retrieval results.

The evaluators consume the versioned synthetic Q&A case contract and the
provider-neutral retrieval result.  They measure the offline fixture contract;
they do not qualify a production index or create a release threshold.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final
from uuid import UUID, uuid5

from spine.domain.evaluations import EvaluationResult, EvaluationVerdict
from spine.evals.qa_r1 import QACase, RelevanceLabel
from spine.memory.contracts import ContextReference, RetrievalResult


RECALL_AT_K_EVALUATOR_KEY: Final[str] = "retrieval.recall_at_k"
MRR_EVALUATOR_KEY: Final[str] = "retrieval.mrr"
RETRIEVAL_METRICS_EVALUATOR_VERSION: Final[str] = "v1"
_SUBJECT_NAMESPACE: Final[UUID] = UUID("8fd8c9cb-7dbf-4df6-a9af-0d3d35a5a8a1")


def evaluate_recall_at_k(
    result: RetrievalResult,
    case: QACase,
    *,
    k: int,
    subject_id: UUID | None = None,
) -> EvaluationResult:
    """Evaluate the fraction of relevant references found in the top ``k``.

    Repeated result references and repeated identical labels represent one
    logical evidence item.  References outside the case workspace and
    environment are ignored and counted, without copying their excerpts into
    the evaluation result.  Non-answer cases are explicitly not applicable:
    their absence of retrievable evidence is not a zero-quality retrieval score.
    """

    _validate_k(k)
    identity = _evaluation_identity(result, case, RECALL_AT_K_EVALUATOR_KEY, str(k))
    base = _base_result(
        evaluator_key=RECALL_AT_K_EVALUATOR_KEY,
        case=case,
        subject_id=subject_id,
        result_id=identity,
    )

    if case.expected_outcome != "answered":
        return base.model_copy(
            update={
                "verdict": EvaluationVerdict.REVIEW,
                "score": None,
                "details": {
                    "k": k,
                    "reason": case.expected_reason or "case_not_answered",
                    "status": "not_applicable",
                },
            }
        )

    relevant_ids = _relevant_ids(case.relevance_labels)
    if not relevant_ids:
        return base.model_copy(
            update={
                "verdict": EvaluationVerdict.REVIEW,
                "score": None,
                "details": {
                    "k": k,
                    "reason": "no_relevant_evidence_labels",
                    "status": "not_applicable",
                },
            }
        )

    ranked_ids, scope_filtered_count = _ranked_ids_in_case_scope(result, case)
    matched_ids = set(ranked_ids[:k]).intersection(relevant_ids)
    score = len(matched_ids) / len(relevant_ids)
    details = {
        "k": k,
        "matched_relevant_count": len(matched_ids),
        "missing_relevant_count": len(relevant_ids) - len(matched_ids),
        "relevant_count": len(relevant_ids),
        "scope_filtered_count": scope_filtered_count,
        "status": "measured",
    }
    return base.model_copy(
        update={
            "verdict": _verdict_for_score(score),
            "score": score,
            "details": details,
        }
    )


def evaluate_mrr(
    result: RetrievalResult,
    case: QACase,
    *,
    subject_id: UUID | None = None,
) -> EvaluationResult:
    """Evaluate reciprocal rank of the first relevant reference.

    The order in ``RetrievalResult.references`` is the ranking.  Duplicate
    evidence IDs are collapsed while preserving their first occurrence, so a
    retry or duplicate ingestion cannot improve or worsen the rank.
    """

    identity = _evaluation_identity(result, case, MRR_EVALUATOR_KEY, "all")
    base = _base_result(
        evaluator_key=MRR_EVALUATOR_KEY,
        case=case,
        subject_id=subject_id,
        result_id=identity,
    )

    if case.expected_outcome != "answered":
        return base.model_copy(
            update={
                "verdict": EvaluationVerdict.REVIEW,
                "score": None,
                "details": {
                    "reason": case.expected_reason or "case_not_answered",
                    "status": "not_applicable",
                },
            }
        )

    relevant_ids = _relevant_ids(case.relevance_labels)
    if not relevant_ids:
        return base.model_copy(
            update={
                "verdict": EvaluationVerdict.REVIEW,
                "score": None,
                "details": {
                    "reason": "no_relevant_evidence_labels",
                    "status": "not_applicable",
                },
            }
        )

    ranked_ids, scope_filtered_count = _ranked_ids_in_case_scope(result, case)
    relevant_set = set(relevant_ids)
    first_rank = next(
        (rank for rank, evidence_id in enumerate(ranked_ids, start=1) if evidence_id in relevant_set),
        None,
    )
    score = 0.0 if first_rank is None else 1.0 / first_rank
    details = {
        "first_relevant_rank": first_rank,
        "missing_relevant_count": len(relevant_set - set(ranked_ids)),
        "relevant_count": len(relevant_ids),
        "scope_filtered_count": scope_filtered_count,
        "status": "measured",
    }
    return base.model_copy(
        update={
            "verdict": _verdict_for_score(score),
            "score": score,
            "details": details,
        }
    )


def _base_result(
    *,
    evaluator_key: str,
    case: QACase,
    subject_id: UUID | None,
    result_id: UUID,
) -> EvaluationResult:
    return EvaluationResult(
        id=result_id,
        evaluator_key=evaluator_key,
        evaluator_version=RETRIEVAL_METRICS_EVALUATOR_VERSION,
        subject_type="qa_case",
        subject_id=subject_id or uuid5(_SUBJECT_NAMESPACE, case.case_id),
        verdict=EvaluationVerdict.REVIEW,
    )


def _evaluation_identity(result: RetrievalResult, case: QACase, evaluator_key: str, parameter: str) -> UUID:
    reference_signature = ";".join(
        f"{reference.evidence_id}:{reference.workspace_id}:{reference.environment_id}"
        for reference in result.references
    )
    label_signature = ";".join(
        f"{label.evidence_id}:{label.label}"
        for label in sorted(case.relevance_labels, key=lambda item: item.evidence_id)
    )
    return uuid5(
        _SUBJECT_NAMESPACE,
        f"{evaluator_key}:{RETRIEVAL_METRICS_EVALUATOR_VERSION}:{case.case_id}:"
        f"{parameter}:{label_signature}:{reference_signature}",
    )


def _ranked_ids_in_case_scope(result: RetrievalResult, case: QACase) -> tuple[tuple[str, ...], int]:
    ranked_ids: list[str] = []
    seen: set[str] = set()
    scope_filtered_count = 0
    for reference in result.references:
        if not _is_in_case_scope(reference, case):
            scope_filtered_count += 1
            continue
        if reference.evidence_id not in seen:
            seen.add(reference.evidence_id)
            ranked_ids.append(reference.evidence_id)
    return tuple(ranked_ids), scope_filtered_count


def _is_in_case_scope(reference: ContextReference, case: QACase) -> bool:
    return reference.workspace_id == case.workspace_id and reference.environment_id == case.environment_id


def _relevant_ids(labels: Iterable[RelevanceLabel]) -> tuple[str, ...]:
    labels_by_id: dict[str, str] = {}
    for label in labels:
        evidence_id = label.evidence_id
        value = label.label
        previous = labels_by_id.get(evidence_id)
        if previous is not None and previous != value:
            raise ValueError(f"conflicting relevance labels for evidence {evidence_id!r}")
        labels_by_id[evidence_id] = value
    return tuple(sorted(evidence_id for evidence_id, value in labels_by_id.items() if value == "relevant"))


def _verdict_for_score(score: float) -> EvaluationVerdict:
    return EvaluationVerdict.PASS if score == 1.0 else EvaluationVerdict.FAIL


def _validate_k(k: int) -> None:
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError("k must be a positive integer")


__all__ = [
    "MRR_EVALUATOR_KEY",
    "RECALL_AT_K_EVALUATOR_KEY",
    "RETRIEVAL_METRICS_EVALUATOR_VERSION",
    "evaluate_mrr",
    "evaluate_recall_at_k",
]
