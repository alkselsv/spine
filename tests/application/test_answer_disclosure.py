from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.answer_disclosure import (
    AnswerDisclosureGate,
    AnswerDisclosureIdempotencyConflict,
    InMemoryAcceptedAnswerOutputPort,
    InMemoryAnswerDisclosureRepository,
)
from spine.domain.capabilities import (
    AnswerAuthorizationOutcome,
    AnswerDisclosureRequest,
    AnswerOutcomeDecisionInput,
    AnswerQuestionOutcome,
    CitationValidationStatus,
    CurrentDisclosureEligibility,
    DisclosureEligibilityReason,
    DisclosureEligibilityStatus,
    RetrievalOutcome,
)

from tests.domain.capabilities.test_answer_disclosure import _evaluation
from tests.domain.capabilities.test_answer_question_candidate import candidate_output, question


def uid(value: int) -> UUID:
    return UUID(int=value)


def disclosure_request(*, eligible: bool = True) -> AnswerDisclosureRequest:
    candidate = candidate_output()
    return AnswerDisclosureRequest(
        request_id=uid(901),
        decision=AnswerOutcomeDecisionInput(
            question=question(),
            retrieval_outcome=RetrievalOutcome.EVIDENCE_AVAILABLE,
            authorization=AnswerAuthorizationOutcome.ALLOWED,
            candidate=candidate,
            citation_validation={
                "status": CitationValidationStatus.VALID,
                "reason_code": "valid",
                "validation_reference": uid(902),
                "citation_ids": (candidate.answer.citations[0].citation_id,),
            },
            semantic_support={
                "status": "supported",
                "validation_reference": uid(903),
                "claim_ids": (candidate.answer.claims[0].claim_id,),
            },
            evaluation=_evaluation(candidate),
        ),
        current_eligibility=CurrentDisclosureEligibility(
            status=(DisclosureEligibilityStatus.ELIGIBLE if eligible else DisclosureEligibilityStatus.INELIGIBLE),
            validation_reference=uid(904),
            reason=(
                DisclosureEligibilityReason.CURRENT_AUTHORITY_CONFIRMED
                if eligible
                else DisclosureEligibilityReason.CURRENT_AUTHORITY_REJECTED
            ),
        ),
    )


@pytest.mark.asyncio
async def test_gate_publishes_and_persists_only_accepted_grounded_answer() -> None:
    sink = InMemoryAcceptedAnswerOutputPort(reference_factory=lambda: uid(905))
    result = await AnswerDisclosureGate(
        repository=InMemoryAnswerDisclosureRepository(), accepted_output=sink,
        clock=lambda: datetime(2026, 10, 16, tzinfo=timezone.utc),
    ).disclose(disclosure_request())

    assert result.record.outcome is AnswerQuestionOutcome.ANSWERED
    assert result.accepted_answer is not None
    assert result.safe_output is None
    assert result.output_reference == uid(905)
    assert len(sink.published) == 1


@pytest.mark.asyncio
async def test_gate_persists_safe_non_content_result_without_publishing_candidate() -> None:
    sink = InMemoryAcceptedAnswerOutputPort()
    result = await AnswerDisclosureGate(
        repository=InMemoryAnswerDisclosureRepository(), accepted_output=sink
    ).disclose(disclosure_request(eligible=False))

    assert result.record.outcome is AnswerQuestionOutcome.ABSTAINED
    assert result.accepted_answer is None
    assert result.safe_output is not None and result.safe_output.answer is None
    assert "The policy applies" not in result.record.model_dump_json()
    assert sink.published == []


@pytest.mark.asyncio
async def test_gate_replays_accepted_result_without_duplicate_event() -> None:
    repository = InMemoryAnswerDisclosureRepository()
    sink = InMemoryAcceptedAnswerOutputPort()
    gate = AnswerDisclosureGate(repository=repository, accepted_output=sink)
    first = await gate.disclose(disclosure_request())
    second = await gate.disclose(disclosure_request())

    assert first.record == second.record
    assert second.replay is True
    assert len(sink.published) == 1


@pytest.mark.asyncio
async def test_gate_rejects_idempotency_reuse_with_changed_facts() -> None:
    repository = InMemoryAnswerDisclosureRepository()
    gate = AnswerDisclosureGate(repository=repository, accepted_output=InMemoryAcceptedAnswerOutputPort())
    await gate.disclose(disclosure_request())
    changed = disclosure_request(eligible=False)

    with pytest.raises(AnswerDisclosureIdempotencyConflict):
        await gate.disclose(changed)
