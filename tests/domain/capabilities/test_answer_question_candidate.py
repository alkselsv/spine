from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.capabilities import (
    AnswerClaim,
    AnswerClaimLocation,
    AnswerContent,
    AnswerQuestionCandidateInput,
    AnswerQuestionCandidateOutput,
    AnswerQuestionInput,
    AnswerQuestionScope,
    ClaimEvidenceRelationship,
    ClaimEvidenceRelationshipKind,
    QuestionSubject,
    RunAttemptCorrelation,
)
from spine.domain.context_bundles import ContextBundleOutcome

from tests.application.test_citation_validation import (
    ATTEMPT,
    ENVIRONMENT,
    RUN,
    WORKSPACE,
    _bundle,
    _reference,
)
from spine.domain.capabilities.answer_question import Citation


ANSWER_ID = UUID(int=700)
CLAIM_ID = UUID(int=701)
CITATION_ID = UUID(int=702)
RELATIONSHIP_ID = UUID(int=703)
TRACE_ID = UUID(int=704)


def question() -> AnswerQuestionInput:
    return AnswerQuestionInput(
        subject=QuestionSubject(text="Which policy applies?"),
        scope=AnswerQuestionScope(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
        ),
        correlation=RunAttemptCorrelation(
            run_id=RUN,
            attempt_id=ATTEMPT,
            trace_id=TRACE_ID,
        ),
    )


def candidate_input() -> AnswerQuestionCandidateInput:
    return AnswerQuestionCandidateInput(
        question=question(),
        context_bundle=_bundle(_reference()),
    )


def candidate_output() -> AnswerQuestionCandidateOutput:
    value = candidate_input()
    bundle = value.context_bundle
    reference = bundle.evidence_references[0]
    citation = Citation(
        citation_id=CITATION_ID,
        answer_id=ANSWER_ID,
        context_bundle=bundle.lineage_reference,
        evidence_reference=reference,
    )
    text = "The policy applies to this customer."
    answer = AnswerContent(
        answer_id=ANSWER_ID,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        run_id=RUN,
        attempt_id=ATTEMPT,
        text=text,
        claims=(
            AnswerClaim(
                claim_id=CLAIM_ID,
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                run_id=RUN,
                attempt_id=ATTEMPT,
                text=text,
                location=AnswerClaimLocation(
                    answer_id=ANSWER_ID,
                    start_offset=0,
                    end_offset=len(text),
                ),
            ),
        ),
        citations=(citation,),
        relationships=(
            ClaimEvidenceRelationship(
                relationship_id=RELATIONSHIP_ID,
                answer_id=ANSWER_ID,
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                run_id=RUN,
                attempt_id=ATTEMPT,
                claim_id=CLAIM_ID,
                kind=ClaimEvidenceRelationshipKind.INDEPENDENT,
                citation_ids=(CITATION_ID,),
            ),
        ),
    )
    return AnswerQuestionCandidateOutput(
        question=question(),
        context_bundle=bundle.lineage_reference,
        answer=answer,
    )


def test_candidate_input_requires_one_authorized_context_bundle() -> None:
    result = candidate_input()

    assert result.context_bundle.outcome is ContextBundleOutcome.ALLOWED
    assert len(result.context_bundle.evidence_references) == 1
    assert result.question.subject.text == "Which policy applies?"


@pytest.mark.parametrize(
    "update",
    (
        {"scope": {"workspace_id": UUID(int=999), "environment_id": ENVIRONMENT}},
        {"correlation": {"run_id": UUID(int=999), "attempt_id": ATTEMPT, "trace_id": TRACE_ID}},
    ),
)
def test_candidate_input_rejects_context_bundle_outside_question_lineage(
    update: dict[str, object],
) -> None:
    invalid_question = question().model_copy(update=update)

    with pytest.raises(ValidationError, match="context bundle"):
        AnswerQuestionCandidateInput(
            question=invalid_question,
            context_bundle=_bundle(_reference()),
        )


def test_candidate_input_rejects_non_authorized_bundle_outcome() -> None:
    original = _bundle(_reference())
    values = original.model_dump(mode="python")
    values.update(
        outcome=ContextBundleOutcome.INSUFFICIENT,
        reason_code="insufficient",
    )
    bundle = type(original).create(**values)

    with pytest.raises(ValidationError, match="authorized"):
        AnswerQuestionCandidateInput(question=question(), context_bundle=bundle)


def test_candidate_output_keeps_answer_claims_and_citations_internal() -> None:
    result = candidate_output()

    assert result.answer.claims[0].text == result.answer.text
    assert result.answer.citations[0].evidence_id == "evidence-1"
    assert result.context_bundle == result.answer.citations[0].context_bundle
