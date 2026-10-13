from __future__ import annotations

from uuid import UUID

import pytest

from spine.domain.evaluations import EvaluationVerdict
from spine.evals.qa_r1 import ACLFixture, QACase, load_r1_dataset
from spine.evaluations.citation_metrics import (
    AnswerClaim,
    AnswerSubmission,
    Citation,
    ContextBundle,
    evaluate_answer_outcome,
    evaluate_citation_precision,
    evaluate_citation_recall,
    evaluate_citations,
    evaluate_groundedness,
    evaluate_locator_validity,
)
from spine.memory.contracts import ContextReference


DATASET = load_r1_dataset()
OTHER_WORKSPACE = UUID("22222222-2222-4222-8222-222222222111")


def _case(case_id: str) -> QACase:
    return next(case for case in DATASET.cases if case.case_id == case_id)


def _evidence(evidence_id: str):
    for revision in DATASET.source_revisions:
        for evidence in revision.evidence:
            if evidence.evidence_id == evidence_id:
                return revision, evidence
    raise AssertionError(f"unknown fixture evidence {evidence_id}")


def _bundle(case: QACase, *evidence_ids: str) -> ContextBundle:
    references = []
    for evidence_id in evidence_ids:
        revision, evidence = _evidence(evidence_id)
        references.append(
            ContextReference(
                source_id=str(revision.source_object_id),
                source_revision_id=revision.revision_id,
                workspace_id=case.workspace_id,
                environment_id=case.environment_id,
                evidence_id=evidence.evidence_id,
                locator=evidence.locator.model_dump(mode="json"),
                excerpt=evidence.excerpt,
            )
        )
    return ContextBundle(
        bundle_id="bundle-171",
        workspace_id=case.workspace_id,
        environment_id=case.environment_id,
        references=tuple(references),
    )


def _citation(evidence_id: str, *, citation_id: str | None = None, **updates: object) -> Citation:
    revision, evidence = _evidence(evidence_id)
    values: dict[str, object] = {
        "citation_id": citation_id or f"citation-{evidence_id}",
        "evidence_id": evidence_id,
        "source_object_id": revision.source_object_id,
        "source_revision_id": revision.revision_id,
        "workspace_id": revision.workspace_id,
        "environment_id": revision.environment_id,
        "source_revision_digest": revision.revision_digest,
        "locator": evidence.locator.model_dump(mode="json"),
        "context_bundle_id": "bundle-171",
    }
    values.update(updates)
    return Citation.model_validate(values)


def _answer(case: QACase, *evidence_ids: str, outcome: str = "answered") -> AnswerSubmission:
    citation_ids = tuple(f"citation-{evidence_id}" for evidence_id in evidence_ids)
    claims = tuple(
        AnswerClaim(
            claim_id=claim.claim_id,
            text=claim.text,
            citation_ids=tuple(f"citation-{evidence_id}" for evidence_id in claim.evidence_ids),
        )
        for claim in case.expected_claims
    )
    return AnswerSubmission(
        outcome=outcome,
        claims=claims,
        citations=tuple(_citation(evidence_id) for evidence_id in evidence_ids),
        semantic_score=1.0,
    )


@pytest.mark.parametrize(
    ("case_id", "evidence_id"),
    [("pdf-single-page", "ev-policy-current"), ("russian-xlsx", "ev-price-row")],
)
def test_valid_pdf_and_xlsx_citations_have_complete_deterministic_metrics(
    case_id: str,
    evidence_id: str,
) -> None:
    case = _case(case_id)
    answer = _answer(case, evidence_id)
    bundle = _bundle(case, evidence_id)

    citations = evaluate_citations(answer, case, dataset=DATASET, context_bundle=bundle)
    precision = evaluate_citation_precision(answer, case, dataset=DATASET, context_bundle=bundle)
    recall = evaluate_citation_recall(answer, case, dataset=DATASET, context_bundle=bundle)
    locator = evaluate_locator_validity(answer, case, dataset=DATASET, context_bundle=bundle)
    groundedness = evaluate_groundedness(answer, case, dataset=DATASET, context_bundle=bundle)
    outcome = evaluate_answer_outcome(answer, case, dataset=DATASET, context_bundle=bundle)

    assert citations.verdict is EvaluationVerdict.PASS
    assert citations.score == 1.0
    assert citations.details["citation_precision"] == 1.0
    assert citations.details["citation_recall"] == 1.0
    assert citations.details["locator_validity"] == 1.0
    assert citations.details["citation_identity_validity"] == 1.0
    assert citations.details["failure_counts"] == {}
    assert groundedness.verdict is EvaluationVerdict.PASS
    assert groundedness.score == 1.0
    assert groundedness.details["supported_claim_count"] == 1
    for result in (citations, precision, recall, locator, groundedness, outcome):
        assert result.evaluator_version == "v1"
        assert result.verdict is EvaluationVerdict.PASS


def test_multi_evidence_claim_requires_all_labelled_evidence() -> None:
    case = _case("multi-chunk-synthesis")
    answer = _answer(case, "ev-handbook-intro")
    bundle = _bundle(case, "ev-handbook-intro", "ev-handbook-sla")

    evaluation = evaluate_groundedness(answer, case, dataset=DATASET, context_bundle=bundle)

    assert evaluation.verdict is EvaluationVerdict.FAIL
    assert evaluation.score == 0.5
    assert evaluation.details["unsupported_claim_ids"] == ["urgent-escalation"]
    assert evaluation.details["missing_evidence_by_claim"] == {
        "urgent-escalation": ["ev-handbook-sla"]
    }


def test_non_answer_citation_recall_is_explicitly_not_applicable() -> None:
    case = _case("absent-evidence-abstention")
    answer = AnswerSubmission(outcome="abstained")
    bundle = _bundle(case)

    evaluation = evaluate_citation_recall(
        answer,
        case,
        dataset=DATASET,
        context_bundle=bundle,
    )

    assert evaluation.verdict is EvaluationVerdict.REVIEW
    assert evaluation.score is None
    assert evaluation.details["status"] == "not_applicable"


def test_answer_outcome_evaluation_identity_includes_case() -> None:
    first_case = _case("absent-evidence-abstention")
    second_case = _case("workspace-isolation")

    first = evaluate_answer_outcome(
        AnswerSubmission(outcome="abstained"),
        first_case,
        dataset=DATASET,
        context_bundle=_bundle(first_case),
    )
    second = evaluate_answer_outcome(
        AnswerSubmission(outcome="abstained"),
        second_case,
        dataset=DATASET,
        context_bundle=_bundle(second_case),
    )

    assert first.id != second.id


@pytest.mark.parametrize(
    ("case_id", "citation", "expected_failure"),
    [
        (
            "allowed-acl",
            lambda: _citation(
                "ev-policy-current",
                locator={"kind": "pdf_page", "schema_version": "locator:pdf-page:v1", "page": 0},
            ),
            "malformed",
        ),
        (
            "allowed-acl",
            lambda: _citation("ev-policy-current").model_copy(
                update={"citation_id": "citation-fabricated", "evidence_id": "ev-not-in-dataset"}
            ),
            "fabricated",
        ),
        (
            "changed-revision",
            lambda: _citation("ev-policy-v1"),
            "stale",
        ),
        (
            "tombstone",
            lambda: _citation("ev-deleted-guide"),
            "tombstoned",
        ),
        (
            "denied-acl",
            lambda: _citation("ev-policy-current"),
            "unauthorized",
        ),
        (
            "allowed-acl",
            lambda: _citation("ev-price-row"),
            "unauthorized",
        ),
    ],
)
def test_invalid_citations_are_classified_without_protected_text_in_result(
    case_id: str,
    citation,
    expected_failure: str,
) -> None:
    case = _case(case_id)
    answer = AnswerSubmission(
        outcome="answered",
        claims=(
            AnswerClaim(
                claim_id=case.expected_claims[0].claim_id if case.expected_claims else "unexpected",
                text="candidate claim",
                citation_ids=(citation().citation_id,),
            ),
        ),
        citations=(citation(),),
    )
    bundle = _bundle(case, "ev-policy-current")

    evaluation = evaluate_citations(answer, case, dataset=DATASET, context_bundle=bundle)

    assert evaluation.verdict is EvaluationVerdict.FAIL
    assert evaluation.details["failure_counts"][expected_failure] == 1
    assert "Synthetic" not in evaluation.model_dump_json()
    assert "protected" not in evaluation.model_dump_json()


def test_known_evidence_outside_exact_context_bundle_is_unresolvable() -> None:
    case = _case("allowed-acl")
    answer = _answer(case, "ev-policy-current")
    answer = answer.model_copy(
        update={
            "citations": (
                _citation("ev-policy-current", context_bundle_id="different-bundle"),
            )
        }
    )

    evaluation = evaluate_citations(answer, case, dataset=DATASET, context_bundle=_bundle(case))

    assert evaluation.details["failure_counts"] == {"unresolvable": 1}


def test_authorized_evidence_not_labelled_for_the_case_is_unrelated() -> None:
    case = _case("allowed-acl")
    pricing_revision, _ = _evidence("ev-price-row")
    expanded_case = case.model_copy(
        update={
            "available_revision_ids": (*case.available_revision_ids, pricing_revision.revision_id),
            "acl": (
                *case.acl,
                ACLFixture(
                    source_object_id=pricing_revision.source_object_id,
                    principal_id=case.principal_id,
                    read_content=True,
                ),
            ),
        }
    )
    citation = _citation("ev-price-row")

    evaluation = evaluate_citations(
        AnswerSubmission(
            outcome="answered",
            claims=(AnswerClaim(claim_id="retention-period", text="claim", citation_ids=(citation.citation_id,)),),
            citations=(citation,),
        ),
        expanded_case,
        dataset=DATASET,
        context_bundle=_bundle(expanded_case, "ev-policy-current", "ev-price-row"),
    )

    assert evaluation.details["failure_counts"] == {"unrelated": 1}


def test_empty_citation_identity_is_classified_as_malformed() -> None:
    case = _case("allowed-acl")
    citation = Citation(
        citation_id="",
        evidence_id="ev-policy-current",
        locator=_evidence("ev-policy-current")[1].locator.model_dump(mode="json"),
    )

    evaluation = evaluate_citations(
        AnswerSubmission(outcome="answered", citations=(citation,)),
        case,
        dataset=DATASET,
        context_bundle=_bundle(case, "ev-policy-current"),
    )

    assert evaluation.details["failure_counts"] == {"malformed": 1}


def test_cross_workspace_reference_is_unauthorized_and_does_not_leak() -> None:
    case = _case("allowed-acl")
    citation = _citation(
        "ev-policy-current",
        workspace_id=OTHER_WORKSPACE,
    )
    answer = AnswerSubmission(
        outcome="answered",
        claims=(AnswerClaim(claim_id="retention-period", text="claim", citation_ids=(citation.citation_id,)),),
        citations=(citation,),
    )

    evaluation = evaluate_citations(
        answer,
        case,
        dataset=DATASET,
        context_bundle=_bundle(case, "ev-policy-current"),
    )

    assert evaluation.details["failure_counts"] == {"unauthorized": 1}
    assert "protected excerpt" not in evaluation.model_dump_json()


@pytest.mark.parametrize("expected_outcome", ["abstained", "denied"])
def test_abstention_and_denial_are_distinct_successful_outcomes(expected_outcome: str) -> None:
    case_id = "absent-evidence-abstention" if expected_outcome == "abstained" else "denied-acl"
    case = _case(case_id)
    answer = AnswerSubmission(outcome=expected_outcome, semantic_score=1.0)

    evaluation = evaluate_citations(answer, case, dataset=DATASET, context_bundle=_bundle(case))

    assert evaluation.verdict is EvaluationVerdict.PASS
    assert evaluation.score == 1.0
    assert evaluation.details["outcome"] == expected_outcome
    assert evaluation.details["expected_outcome"] == expected_outcome


def test_failed_deterministic_safety_check_cannot_be_overridden_by_semantic_score() -> None:
    case = _case("allowed-acl")
    answer = _answer(case, "ev-policy-current").model_copy(
        update={
            "citations": (_citation("ev-policy-current", source_revision_digest="0" * 64),),
            "semantic_score": 1.0,
        }
    )

    evaluation = evaluate_citations(
        answer,
        case,
        dataset=DATASET,
        context_bundle=_bundle(case, "ev-policy-current"),
    )

    assert evaluation.verdict is EvaluationVerdict.FAIL
    assert evaluation.details["semantic_score"] == 1.0
    assert evaluation.details["deterministic_gate"] == "failed"

