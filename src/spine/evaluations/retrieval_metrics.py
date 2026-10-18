"""Deterministic offline metrics for ranked retrieval results.

The evaluators consume the versioned synthetic Q&A case and dataset contracts
plus the provider-neutral retrieval result.  The dataset is required to
validate evidence-to-revision provenance.  These evaluators measure the
offline fixture contract; they do not qualify a production index or create a
release threshold.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final
from uuid import UUID, uuid5

from spine.domain.evaluations import EvaluationResult, EvaluationVerdict
from spine.evals.qa_r1 import QACase, QADataset, RelevanceLabel
from spine.memory.contracts import ContextReference, RetrievalResult


RECALL_AT_K_EVALUATOR_KEY: Final[str] = "retrieval.recall_at_k"
MRR_EVALUATOR_KEY: Final[str] = "retrieval.mrr"
RETRIEVAL_METRICS_EVALUATOR_VERSION: Final[str] = "v1"
_SUBJECT_NAMESPACE: Final[UUID] = UUID("8fd8c9cb-7dbf-4df6-a9af-0d3d35a5a8a1")


def score_recall_at_k(
    ranked_ids: Iterable[str], relevant_ids: Iterable[str], *, k: int
) -> tuple[float | None, int, int]:
    """Apply the v1 recall formula to already-authorized evidence identities."""

    _validate_k(k)
    relevant = set(relevant_ids)
    if not relevant:
        return None, 0, 0
    ranked = tuple(dict.fromkeys(ranked_ids))
    matched = len(set(ranked[:k]).intersection(relevant))
    return matched / len(relevant), matched, len(relevant)


def score_mrr(
    ranked_ids: Iterable[str], relevant_ids: Iterable[str]
) -> tuple[float | None, int | None]:
    """Apply the v1 reciprocal-rank formula to authorized evidence identities."""

    relevant = set(relevant_ids)
    if not relevant:
        return None, None
    rank = next(
        (
            position
            for position, evidence_id in enumerate(dict.fromkeys(ranked_ids), start=1)
            if evidence_id in relevant
        ),
        None,
    )
    return (0.0 if rank is None else 1.0 / rank), rank


def evaluate_recall_at_k(
    result: RetrievalResult,
    case: QACase,
    *,
    k: int,
    dataset: QADataset,
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
    identity = _evaluation_identity(result, case, RECALL_AT_K_EVALUATOR_KEY, str(k), dataset)
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

    ranked_ids, scope_filtered_count, ineligible_count = _ranked_ids_in_case_scope(result, case, dataset)
    if result.references and not ranked_ids:
        return base.model_copy(
            update={
                "verdict": EvaluationVerdict.REVIEW,
                "score": None,
                "details": {
                    "index_version": result.index_version,
                    "k": k,
                    "reason": "inaccessible_evidence",
                    "retrieval_strategy": result.retrieval_strategy,
                    "scope_filtered_count": scope_filtered_count,
                    "status": "not_applicable",
                    "ineligible_reference_count": ineligible_count,
                },
            }
        )
    score, matched_count, relevant_count = score_recall_at_k(
        ranked_ids, relevant_ids, k=k
    )
    details = {
        "index_version": result.index_version,
        "k": k,
        "matched_relevant_count": matched_count,
        "missing_relevant_count": relevant_count - matched_count,
        "relevant_count": relevant_count,
        "retrieval_strategy": result.retrieval_strategy,
        "scope_filtered_count": scope_filtered_count,
        "status": "measured",
        "ineligible_reference_count": ineligible_count,
    }
    return base.model_copy(
        update={
            "verdict": EvaluationVerdict.REVIEW,
            "score": score,
            "details": details,
        }
    )


def evaluate_mrr(
    result: RetrievalResult,
    case: QACase,
    *,
    dataset: QADataset,
    subject_id: UUID | None = None,
) -> EvaluationResult:
    """Evaluate reciprocal rank of the first relevant reference.

    The order in ``RetrievalResult.references`` is the ranking and the stable
    tie-break supplied by the retrieval contract.  Duplicate evidence IDs are
    collapsed while preserving their first occurrence, so a retry or duplicate
    ingestion cannot improve or worsen the rank.
    """

    identity = _evaluation_identity(result, case, MRR_EVALUATOR_KEY, "all", dataset)
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

    ranked_ids, scope_filtered_count, ineligible_count = _ranked_ids_in_case_scope(result, case, dataset)
    if result.references and not ranked_ids:
        return base.model_copy(
            update={
                "verdict": EvaluationVerdict.REVIEW,
                "score": None,
                "details": {
                    "index_version": result.index_version,
                    "reason": "inaccessible_evidence",
                    "retrieval_strategy": result.retrieval_strategy,
                    "scope_filtered_count": scope_filtered_count,
                    "status": "not_applicable",
                    "ineligible_reference_count": ineligible_count,
                },
            }
        )
    relevant_set = set(relevant_ids)
    score, first_rank = score_mrr(ranked_ids, relevant_ids)
    details = {
        "index_version": result.index_version,
        "first_relevant_rank": first_rank,
        "missing_relevant_count": len(relevant_set - set(ranked_ids)),
        "relevant_count": len(relevant_ids),
        "retrieval_strategy": result.retrieval_strategy,
        "scope_filtered_count": scope_filtered_count,
        "status": "measured",
        "ineligible_reference_count": ineligible_count,
    }
    return base.model_copy(
        update={
            "verdict": EvaluationVerdict.REVIEW,
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
        workspace_id=case.workspace_id,
        environment_id=case.environment_id,
        verdict=EvaluationVerdict.REVIEW,
    )


def _evaluation_identity(
    result: RetrievalResult,
    case: QACase,
    evaluator_key: str,
    parameter: str,
    dataset: QADataset,
) -> UUID:
    reference_signature = ";".join(
        f"{reference.evidence_id}:{reference.source_revision_id}:{reference.workspace_id}:{reference.environment_id}"
        for reference in result.references
    )
    label_signature = ";".join(
        f"{label.evidence_id}:{label.label}"
        for label in sorted(case.relevance_labels, key=lambda item: item.evidence_id)
    )
    return uuid5(
        _SUBJECT_NAMESPACE,
        f"{evaluator_key}:{RETRIEVAL_METRICS_EVALUATOR_VERSION}:{case.case_id}:"
        f"{parameter}:{result.retrieval_strategy}:{result.index_version}:"
        f"{dataset.dataset_digest if dataset is not None else 'no-dataset'}:"
        f"{label_signature}:{reference_signature}",
    )


def _ranked_ids_in_case_scope(
    result: RetrievalResult,
    case: QACase,
    dataset: QADataset,
) -> tuple[tuple[str, ...], int, int]:
    ranked_ids: list[str] = []
    seen: set[str] = set()
    scope_filtered_count = 0
    ineligible_count = 0
    evidence_by_id = _evidence_by_id(dataset)
    for reference in result.references:
        if not _is_in_case_scope(reference, case):
            scope_filtered_count += 1
            continue
        if reference.source_revision_id not in case.available_revision_ids:
            ineligible_count += 1
            continue
        expected_revision = evidence_by_id.get(reference.evidence_id)
        if expected_revision is None or expected_revision != reference.source_revision_id:
            ineligible_count += 1
            continue
        if reference.evidence_id not in seen:
            seen.add(reference.evidence_id)
            ranked_ids.append(reference.evidence_id)
    return tuple(ranked_ids), scope_filtered_count, ineligible_count


def _evidence_by_id(dataset: QADataset) -> dict[str, UUID]:
    return {
        item.evidence_id: revision.revision_id
        for revision in dataset.source_revisions
        for item in revision.evidence
    }


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
