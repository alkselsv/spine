from __future__ import annotations

from uuid import UUID

import pytest

from spine.domain.evaluations import EvaluationVerdict
from spine.evals.qa_r1 import QACase, RelevanceLabel, load_r1_dataset
from spine.evaluations.retrieval_metrics import (
    MRR_EVALUATOR_KEY,
    RECALL_AT_K_EVALUATOR_KEY,
    evaluate_mrr,
    evaluate_recall_at_k,
)
from spine.memory.contracts import ContextReference, RetrievalResult


_REVISION_ID = UUID("11111111-1111-4111-8111-111111111132")


def _case(case_id: str) -> QACase:
    return next(case for case in load_r1_dataset().cases if case.case_id == case_id)


def _result(case: QACase, *evidence_ids: str, workspace_id: UUID | None = None) -> RetrievalResult:
    evidence_by_id = {
        item.evidence_id: item
        for revision in load_r1_dataset().source_revisions
        for item in revision.evidence
    }
    return RetrievalResult(
        context=tuple(f"protected excerpt for {evidence_id}" for evidence_id in evidence_ids),
        references=tuple(
            ContextReference(
                source_id="synthetic-source",
                source_revision_id=(
                    evidence_by_id[evidence_id].source_revision_id
                    if evidence_id in evidence_by_id
                    else _REVISION_ID
                ),
                workspace_id=case.workspace_id if workspace_id is None else workspace_id,
                environment_id=case.environment_id,
                evidence_id=evidence_id,
                locator={},
                excerpt=f"protected excerpt for {evidence_id}",
            )
            for evidence_id in evidence_ids
        ),
        retrieval_strategy=case.retrieval_strategy,
        index_version="qa-r1-replay:v1",
    )


def test_recall_at_k_counts_each_required_reference_once() -> None:
    case = _case("multi-document-multi-hop")
    result = _result(case, "ev-price-row", "ev-policy-current", "ev-policy-current")

    at_one = evaluate_recall_at_k(result, case, k=1)
    at_two = evaluate_recall_at_k(result, case, k=2)

    assert at_one.evaluator_key == RECALL_AT_K_EVALUATOR_KEY
    assert at_one.evaluator_version == "v1"
    assert at_one.score == 0.5
    assert at_one.workspace_id == case.workspace_id
    assert at_one.environment_id == case.environment_id
    assert at_one.details == {
        "index_version": "qa-r1-replay:v1",
        "k": 1,
        "ineligible_reference_count": 0,
        "matched_relevant_count": 1,
        "missing_relevant_count": 1,
        "relevant_count": 2,
        "retrieval_strategy": "graph-hybrid:v1",
        "scope_filtered_count": 0,
        "status": "measured",
    }
    assert at_two.score == 1.0
    assert at_two.verdict is EvaluationVerdict.REVIEW


def test_mrr_uses_retrieval_order_and_ignores_duplicate_references() -> None:
    case = _case("multi-document-multi-hop")
    result = _result(case, "unrelated-result", "ev-price-row", "ev-price-row", "ev-policy-current")

    evaluation = evaluate_mrr(result, case)

    assert evaluation.evaluator_key == MRR_EVALUATOR_KEY
    assert evaluation.score == 0.5
    assert evaluation.details == {
        "first_relevant_rank": 2,
        "index_version": "qa-r1-replay:v1",
        "ineligible_reference_count": 0,
        "missing_relevant_count": 0,
        "relevant_count": 2,
        "retrieval_strategy": "graph-hybrid:v1",
        "scope_filtered_count": 0,
        "status": "measured",
    }
    assert evaluation.verdict is EvaluationVerdict.REVIEW


def test_non_answer_case_is_explicitly_not_applicable_without_disclosing_content() -> None:
    case = _case("denied-acl")
    evaluation = evaluate_recall_at_k(_result(case, "ev-policy-current"), case, k=1)

    assert evaluation.score is None
    assert evaluation.verdict is EvaluationVerdict.REVIEW
    assert evaluation.details == {
        "k": 1,
        "reason": "access_not_permitted",
        "status": "not_applicable",
    }
    assert "protected excerpt" not in evaluation.model_dump_json()


def test_cross_workspace_reference_is_excluded_without_leaking_protected_content() -> None:
    case = _case("allowed-acl")
    other_workspace = UUID("22222222-2222-4222-8222-222222222111")

    evaluation = evaluate_recall_at_k(
        _result(case, "ev-policy-current", workspace_id=other_workspace), case, k=1
    )

    assert evaluation.score is None
    assert evaluation.verdict is EvaluationVerdict.REVIEW
    assert evaluation.details["status"] == "not_applicable"
    assert evaluation.details["reason"] == "inaccessible_evidence"
    assert evaluation.details["scope_filtered_count"] == 1
    assert "protected excerpt" not in evaluation.model_dump_json()


def test_duplicate_relevance_labels_are_deduplicated_and_results_are_stable() -> None:
    case = _case("allowed-acl").model_copy(
        update={
            "relevance_labels": (
                RelevanceLabel(evidence_id="ev-policy-current", label="relevant"),
                RelevanceLabel(evidence_id="ev-policy-current", label="relevant"),
            )
        }
    )
    result = _result(case, "ev-policy-current")

    first = evaluate_recall_at_k(result, case, k=1)
    second = evaluate_recall_at_k(result, case, k=1)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.model_dump(mode="json")["evaluator_version"] == "v1"
    assert first.model_dump(mode="json")["evaluator_key"] == RECALL_AT_K_EVALUATOR_KEY
    assert first.details["relevant_count"] == 1


@pytest.mark.parametrize("k", [0, -1])
def test_recall_at_k_rejects_non_positive_k(k: int) -> None:
    with pytest.raises(ValueError, match="k must be a positive integer"):
        evaluate_recall_at_k(
            _result(_case("allowed-acl"), "ev-policy-current"),
            _case("allowed-acl"),
            k=k,
        )


def test_russian_case_uses_the_same_reference_metric_contract() -> None:
    case = _case("russian-xlsx")

    evaluation = evaluate_mrr(_result(case, "ev-price-row"), case)

    assert evaluation.score == 1.0
    assert evaluation.subject_type == "qa_case"
    assert evaluation.verdict is EvaluationVerdict.REVIEW


@pytest.mark.parametrize(
    ("case_id", "metric"),
    [
        ("allowed-acl", "recall"),
        ("duplicate-ingestion", "recall"),
        ("multi-chunk-synthesis", "mrr"),
        ("multi-document-multi-hop", "recall"),
        ("pdf-single-page", "mrr"),
        ("russian-xlsx", "recall"),
    ],
)
def test_answered_dataset_strata_produce_complete_scores(case_id: str, metric: str) -> None:
    case = _case(case_id)
    result = _result(case, *case.expected_evidence_ids)

    evaluation = (
        evaluate_recall_at_k(result, case, k=len(case.expected_evidence_ids))
        if metric == "recall"
        else evaluate_mrr(result, case)
    )

    assert evaluation.score == 1.0
    assert evaluation.verdict is EvaluationVerdict.REVIEW


def test_missing_relevant_evidence_is_measured_as_zero_with_explicit_missing_count() -> None:
    case = _case("allowed-acl")

    recall = evaluate_recall_at_k(_result(case), case, k=1)
    mrr = evaluate_mrr(_result(case), case)

    assert recall.score == 0.0
    assert recall.details["missing_relevant_count"] == 1
    assert mrr.score == 0.0
    assert mrr.details["first_relevant_rank"] is None


def test_mixed_language_denied_case_is_not_applicable_without_disclosure() -> None:
    case = _case("workspace-isolation")

    evaluation = evaluate_mrr(_result(case, "ev-catalog-row"), case)

    assert evaluation.score is None
    assert evaluation.verdict is EvaluationVerdict.REVIEW
    assert evaluation.details == {
        "reason": "no_authorized_evidence",
        "status": "not_applicable",
    }
    assert "protected excerpt" not in evaluation.model_dump_json()


def test_stale_or_fabricated_revision_is_excluded_when_dataset_is_supplied() -> None:
    dataset = load_r1_dataset()
    case = _case("changed-revision")
    original = _result(case, "ev-policy-current")
    stale_reference = original.model_copy(
        update={
            "references": (
                original.references[0].model_copy(
                    update={"source_revision_id": UUID("11111111-1111-4111-8111-111111111131")}
                ),
            )
        }
    )

    evaluation = evaluate_recall_at_k(stale_reference, case, k=1, dataset=dataset)

    assert evaluation.score is None
    assert evaluation.details["status"] == "not_applicable"
    assert evaluation.details["ineligible_reference_count"] == 1


def test_same_ranked_result_is_stable_for_equal_relevance_ties() -> None:
    case = _case("multi-document-multi-hop")
    result = _result(case, "ev-policy-current", "ev-price-row")

    first = evaluate_mrr(result, case)
    second = evaluate_mrr(result, case)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.details["first_relevant_rank"] == 1
