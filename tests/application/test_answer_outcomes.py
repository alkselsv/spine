from __future__ import annotations

from uuid import UUID

from spine.application.answer_outcomes import AnswerOutcomeFacts, resolve_answer_outcome
from spine.application.citation_validation import (
    CitationValidationReasonCode,
    RejectedCitation,
)
from spine.domain.capabilities import (
    AnswerAuthorizationOutcome,
    AnswerQuestionOutcome,
    AnswerQuestionCandidateOutput,
    RetrievalOutcome,
)

from tests.domain.capabilities.test_answer_question_candidate import (
    candidate_output,
    question,
)


def test_application_resolver_maps_typed_validator_fake_without_diagnostic_detail() -> None:
    candidate: AnswerQuestionCandidateOutput = candidate_output()
    result = resolve_answer_outcome(
        AnswerOutcomeFacts(
            question=question(),
            retrieval_outcome=RetrievalOutcome.EVIDENCE_AVAILABLE,
            authorization=AnswerAuthorizationOutcome.ALLOWED,
            candidate=candidate,
            citation_results=(
                RejectedCitation(
                    reason_code=CitationValidationReasonCode.EVIDENCE_INVALIDATED,
                    validation_reference=UUID(int=900),
                ),
            ),
        )
    )

    assert result.outcome is AnswerQuestionOutcome.ABSTAINED
    assert result.output is not None
    assert result.output.reason is not None
    assert result.output.reason.code.value == "evidence_invalidated"
    assert "provider" not in result.output.model_dump_json()
