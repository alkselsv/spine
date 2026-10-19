from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.answer_disclosure import (
    AnswerDisclosureFacts,
    AnswerDisclosureGate,
    AnswerDisclosureIdempotencyConflict,
    InMemoryAcceptedAnswerOutputPort,
    InMemoryAnswerDisclosureRepository,
)
from spine.application.answer_outcomes import AnswerOutcomeFacts
from spine.application.qa_sessions import _accepted_answer_presentation
from spine.domain.capabilities import (
    AnswerAuthorizationOutcome,
    AnswerDisclosureRequest,
    AnswerOutcomeDecisionInput,
    AnswerQuestionOutcome,
    AnswerQuestionOutput,
    AnswerQuestionSafeReason,
    AnswerQuestionSafeReasonCode,
    CitationValidationStatus,
    CurrentDisclosureEligibility,
    DisclosureEligibilityReason,
    DisclosureEligibilityStatus,
    RetrievalOutcome,
)
from spine.domain.capabilities.answer_disclosure import (
    AnswerDisclosureRecordV2,
    AnswerExecutionCheckpoint,
)
from spine.domain.capabilities.answer_question_runs import AgentResultRef

from tests.domain.capabilities.test_answer_disclosure import _evaluation
from tests.domain.capabilities.test_answer_question_candidate import candidate_output, question


def uid(value: int) -> UUID:
    return UUID(int=value)


class FlakyAcceptedAnswerOutputPort(InMemoryAcceptedAnswerOutputPort):
    def __init__(self) -> None:
        super().__init__(reference_factory=lambda: uid(906))
        self.failures_remaining = 1

    async def publish(self, answer, *, delivery_id):
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("publication unavailable")
        return await super().publish(answer, delivery_id=delivery_id)


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
            workspace_id=question().scope.workspace_id,
            environment_id=question().scope.environment_id,
            run_id=question().correlation.run_id,
            attempt_id=question().correlation.attempt_id,
            validation_fact_ids=(uid(907),) if eligible else (),
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
async def test_gate_persists_accepted_disclosure_without_publishing_before_terminal_commit() -> None:
    sink = InMemoryAcceptedAnswerOutputPort(reference_factory=lambda: uid(905))
    gate = AnswerDisclosureGate(
        repository=InMemoryAnswerDisclosureRepository(), accepted_output=sink,
        clock=lambda: datetime(2026, 10, 16, tzinfo=timezone.utc),
    )

    result = await gate.prepare(disclosure_request())

    assert result.record.outcome is AnswerQuestionOutcome.ANSWERED
    assert result.record.accepted_answer is not None
    assert result.output_reference is None
    assert sink.published == []


@pytest.mark.asyncio
async def test_gate_persists_and_replays_immutable_execution_checkpoint() -> None:
    repository = InMemoryAnswerDisclosureRepository()
    gate = AnswerDisclosureGate(
        repository=repository,
        accepted_output=InMemoryAcceptedAnswerOutputPort(),
    )
    candidate = disclosure_request().decision.candidate
    assert candidate is not None
    checkpoint = AnswerExecutionCheckpoint(
        execution_pin_digest="a" * 64,
        context_bundle_ref=candidate.context_bundle.bundle,
        candidate_input_artifact_id=uid(910),
        agent_result_ref=AgentResultRef(result_id=uid(911)),
        output_artifact_ids=(uid(912),),
    )

    first = await gate.prepare(
        disclosure_request(), execution_checkpoint=checkpoint
    )
    replay = await gate.prepare(
        disclosure_request(), execution_checkpoint=checkpoint
    )

    assert isinstance(first.record, AnswerDisclosureRecordV2)
    assert replay.record == first.record
    assert replay.replay is True
    with pytest.raises(AnswerDisclosureIdempotencyConflict):
        await gate.prepare(
            disclosure_request(),
            execution_checkpoint=checkpoint.model_copy(
                update={"candidate_input_artifact_id": uid(913)}
            ),
        )


@pytest.mark.asyncio
async def test_committed_delivery_requires_matching_authorized_presentation() -> None:
    repository = InMemoryAnswerDisclosureRepository()
    sink = InMemoryAcceptedAnswerOutputPort(reference_factory=lambda: uid(906))
    gate = AnswerDisclosureGate(repository=repository, accepted_output=sink)
    prepared = await gate.prepare(disclosure_request())
    accepted = prepared.record.accepted_answer
    assert accepted is not None
    answer = accepted.answer
    presentation = _accepted_answer_presentation(
        SimpleNamespace(message_id=uid(908), run_id=answer.run_id), answer
    )

    reference = await gate.deliver_committed(prepared.record, presentation)

    assert reference == uid(906)
    assert sink.published == [accepted]
    assert await gate.deliver_committed(prepared.record, presentation) == uid(906)
    assert len(sink.published) == 1


@pytest.mark.asyncio
async def test_committed_delivery_rejects_conflicting_presentation() -> None:
    repository = InMemoryAnswerDisclosureRepository()
    sink = InMemoryAcceptedAnswerOutputPort()
    gate = AnswerDisclosureGate(repository=repository, accepted_output=sink)
    prepared = await gate.prepare(disclosure_request())
    answer = prepared.record.accepted_answer.answer
    presentation = _accepted_answer_presentation(
        SimpleNamespace(message_id=uid(908), run_id=answer.run_id), answer
    ).model_copy(update={"attempt_id": uid(909)})

    with pytest.raises(AnswerDisclosureIdempotencyConflict):
        await gate.deliver_committed(prepared.record, presentation)
    assert sink.published == []


@pytest.mark.asyncio
async def test_gate_persists_safe_non_content_result_without_publishing_candidate() -> None:
    sink = InMemoryAcceptedAnswerOutputPort()
    result = await AnswerDisclosureGate(
        repository=InMemoryAnswerDisclosureRepository(), accepted_output=sink
    ).disclose(disclosure_request(eligible=False))

    assert result.record.outcome is AnswerQuestionOutcome.ABSTAINED
    assert result.accepted_answer is None
    assert result.safe_output is not None and result.safe_output.answer is None
    assert result.record.validation is not None
    assert "The policy applies" not in result.record.model_dump_json()
    assert sink.published == []


@pytest.mark.asyncio
async def test_gate_persists_explicit_safe_validation_failure_with_actual_retrieval_facts() -> None:
    repository = InMemoryAnswerDisclosureRepository()
    gate = AnswerDisclosureGate(
        repository=repository,
        accepted_output=InMemoryAcceptedAnswerOutputPort(),
    )
    q = question()
    output = AnswerQuestionOutput(
        correlation=q.correlation,
        scope=q.scope,
        outcome=AnswerQuestionOutcome.FAILED,
        reason=AnswerQuestionSafeReason(
            code=AnswerQuestionSafeReasonCode.MANDATORY_PROCESSING_FAILED
        ),
    )
    facts = AnswerDisclosureFacts(
        request_id=uid(914),
        outcome_facts=AnswerOutcomeFacts(
            question=q,
            retrieval_outcome=RetrievalOutcome.EVIDENCE_AVAILABLE,
            authorization=AnswerAuthorizationOutcome.ALLOWED,
        ),
        safe_terminal_output=output,
    )

    result = await gate.prepare_facts(
        facts,
        execution_checkpoint=AnswerExecutionCheckpoint(
            execution_pin_digest="a" * 64,
        ),
    )

    assert isinstance(result.record, AnswerDisclosureRecordV2)
    assert result.record.outcome is AnswerQuestionOutcome.FAILED
    assert result.record.output == output
    assert result.record.execution_checkpoint.context_bundle_ref is None
    assert result.accepted_answer is None


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
async def test_gate_retries_publication_after_record_was_persisted() -> None:
    repository = InMemoryAnswerDisclosureRepository()
    sink = FlakyAcceptedAnswerOutputPort()
    gate = AnswerDisclosureGate(repository=repository, accepted_output=sink)

    with pytest.raises(RuntimeError):
        await gate.disclose(disclosure_request())

    result = await gate.disclose(disclosure_request())

    assert result.replay is True
    assert result.output_reference == uid(906)
    assert len(sink.published) == 1


@pytest.mark.asyncio
async def test_gate_rejects_idempotency_reuse_with_changed_facts() -> None:
    repository = InMemoryAnswerDisclosureRepository()
    gate = AnswerDisclosureGate(repository=repository, accepted_output=InMemoryAcceptedAnswerOutputPort())
    await gate.disclose(disclosure_request())
    changed = disclosure_request().model_copy(
        update={
            "current_eligibility": disclosure_request().current_eligibility.model_copy(
                update={"validation_reference": uid(999)}
            )
        }
    )

    with pytest.raises(AnswerDisclosureIdempotencyConflict):
        await gate.disclose(changed)


@pytest.mark.asyncio
async def test_gate_suppresses_replay_after_current_authority_revocation() -> None:
    repository = InMemoryAnswerDisclosureRepository()
    sink = InMemoryAcceptedAnswerOutputPort()
    gate = AnswerDisclosureGate(repository=repository, accepted_output=sink)
    await gate.disclose(disclosure_request())

    revoked = disclosure_request(eligible=False)
    result = await gate.disclose(revoked)

    assert result.replay is True
    assert result.accepted_answer is None
    assert result.safe_output is not None
    assert result.safe_output.answer is None
    assert len(sink.published) == 1
