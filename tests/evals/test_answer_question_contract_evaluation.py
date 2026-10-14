from __future__ import annotations

import json
from pathlib import Path

from spine.domain.capabilities import (
    AnswerQuestionOutcome,
    RetrievalOutcome,
    map_retrieval_outcome,
)


FIXTURE = (
    Path(__file__).parents[2]
    / "src"
    / "spine"
    / "evals"
    / "data"
    / "answer_question_contract_v1.json"
)


def test_answer_question_contract_evaluation_fixture_covers_closed_outcome_sets() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert fixture["schema_version"] == "answer-question.contract-evaluation:v1"
    assert fixture["capability"] == "answer_question"
    assert tuple(fixture["terminal_outcomes"]) == tuple(
        outcome.value for outcome in AnswerQuestionOutcome
    )
    assert tuple(fixture["retrieval_outcomes"]) == tuple(
        outcome.value for outcome in RetrievalOutcome
    )
    assert {"evidence_available", "insufficient_evidence", "unavailable"}.isdisjoint(
        fixture["terminal_outcomes"]
    )
    assert RetrievalOutcome.FAILED.value == AnswerQuestionOutcome.FAILED.value
    assert fixture["accepted_answer_invariants"] == [
        "answer",
        "claim_citation_ids",
        "citation_source_revision_id",
        "citation_original_sha256",
        "citation_locator_contract_version",
        "context_bundle_lineage",
    ]
    assert fixture["terminal_reason_invariants"] == ["reason", "no_answer_content"]
    assert fixture["retrieval_mapping"] == {
        "evidence_available_without_acceptance": map_retrieval_outcome(
            RetrievalOutcome.EVIDENCE_AVAILABLE,
            accepted=False,
        ).value,
        "evidence_available_after_acceptance": map_retrieval_outcome(
            RetrievalOutcome.EVIDENCE_AVAILABLE,
            accepted=True,
        ).value,
        "insufficient_evidence": map_retrieval_outcome(
            RetrievalOutcome.INSUFFICIENT_EVIDENCE
        ).value,
        "unavailable": map_retrieval_outcome(RetrievalOutcome.UNAVAILABLE).value,
        "failed": map_retrieval_outcome(RetrievalOutcome.FAILED).value,
    }
