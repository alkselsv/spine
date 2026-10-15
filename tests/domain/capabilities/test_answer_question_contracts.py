from __future__ import annotations

import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.capabilities import (
    ANSWER_QUESTION_INPUT_SCHEMA,
    ANSWER_QUESTION_OUTPUT_SCHEMA,
    AnswerClaim,
    AnswerClaimLocation,
    AnswerContent,
    AnswerQuestionContractError,
    AnswerQuestionInput,
    AnswerQuestionOutcome,
    AnswerQuestionOutput,
    AnswerQuestionScope,
    AnswerQuestionSafeReason,
    AnswerQuestionSafeReasonCode,
    CitationReference,
    ClaimEvidenceRelationship,
    ClaimEvidenceRelationshipKind,
    ClarificationRequest,
    LineageReference,
    QuestionSubject,
    RetrievalOutcome,
    RunAttemptCorrelation,
    map_retrieval_outcome,
    parse_answer_question_input,
    parse_answer_question_output,
)
from spine.domain.context_bundles import (
    ContextBundleEvidenceReference,
    ContextBundleLineageRef,
    ContextBundleRef,
)


WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000101")
ENVIRONMENT_ID = UUID("00000000-0000-0000-0000-000000000102")
RUN_ID = UUID("00000000-0000-0000-0000-000000000103")
ATTEMPT_ID = UUID("00000000-0000-0000-0000-000000000104")
TRACE_ID = UUID("00000000-0000-0000-0000-000000000105")
CONTEXT_BUNDLE_ID = UUID("00000000-0000-0000-0000-000000000106")
SOURCE_OBJECT_ID = UUID("00000000-0000-0000-0000-000000000107")
SOURCE_REVISION_ID = UUID("00000000-0000-0000-0000-000000000108")
CITATION_ID = UUID("00000000-0000-0000-0000-000000000109")
CLAIM_ID = UUID("00000000-0000-0000-0000-000000000110")
ANSWER_ID = UUID("00000000-0000-0000-0000-000000000111")


def scope() -> AnswerQuestionScope:
    return AnswerQuestionScope(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
    )


def correlation() -> RunAttemptCorrelation:
    return RunAttemptCorrelation(
        run_id=RUN_ID,
        attempt_id=ATTEMPT_ID,
        trace_id=TRACE_ID,
    )


def citation() -> CitationReference:
    return CitationReference(
        citation_id=CITATION_ID,
        answer_id=ANSWER_ID,
        context_bundle=ContextBundleLineageRef(
            bundle=ContextBundleRef(
                bundle_id=CONTEXT_BUNDLE_ID,
                bundle_version=1,
                bundle_digest="c" * 64,
            ),
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            run_id=RUN_ID,
            attempt_id=ATTEMPT_ID,
        ),
        evidence_reference=ContextBundleEvidenceReference(
            evidence_id="evidence-1",
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            projection_snapshot_id=UUID(int=120),
            activation_generation=1,
            source_object_id=SOURCE_OBJECT_ID,
            source_revision_id=SOURCE_REVISION_ID,
            revision_digest="b" * 64,
            locator_contract_version="stable-locator:v1",
            locator={"kind": "page", "value": "1"},
            evidence_digest="a" * 64,
        ),
    )


def answer() -> AnswerContent:
    return AnswerContent(
        answer_id=ANSWER_ID,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        run_id=RUN_ID,
        attempt_id=ATTEMPT_ID,
        text="The answer is grounded in the approved document.",
        claims=(
            AnswerClaim(
                claim_id=CLAIM_ID,
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
                run_id=RUN_ID,
                attempt_id=ATTEMPT_ID,
                text="The answer is grounded in the approved document.",
                location=AnswerClaimLocation(
                    answer_id=ANSWER_ID,
                    start_offset=0,
                    end_offset=48,
                ),
            ),
        ),
        citations=(citation(),),
        relationships=(
            ClaimEvidenceRelationship(
                relationship_id=UUID(int=200),
                answer_id=ANSWER_ID,
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
                run_id=RUN_ID,
                attempt_id=ATTEMPT_ID,
                claim_id=CLAIM_ID,
                kind=ClaimEvidenceRelationshipKind.INDEPENDENT,
                citation_ids=(CITATION_ID,),
            ),
        ),
    )


def output(**overrides: object) -> AnswerQuestionOutput:
    values: dict[str, object] = {
        "correlation": correlation(),
        "scope": scope(),
        "outcome": AnswerQuestionOutcome.ANSWERED,
        "answer": answer(),
        "lineage": (
            LineageReference(
                kind="context_bundle",
                identifier=CONTEXT_BUNDLE_ID,
                schema_version="context-bundle:v1",
            ),
        ),
    }
    values.update(overrides)
    return AnswerQuestionOutput(**values)


def test_answer_question_contract_round_trips_deterministically() -> None:
    request = AnswerQuestionInput(
        subject=QuestionSubject(text="Which policy applies to this customer?"),
        scope=scope(),
        correlation=correlation(),
    )

    serialized = request.canonical_json()
    assert json.loads(serialized) == {
        "capability": "answer_question",
        "correlation": {
            "attempt_id": str(ATTEMPT_ID),
            "causation_id": None,
            "correlation_id": None,
            "run_id": str(RUN_ID),
            "trace_id": str(TRACE_ID),
        },
        "schema_version": ANSWER_QUESTION_INPUT_SCHEMA,
        "scope": {
            "environment_id": str(ENVIRONMENT_ID),
            "workspace_id": str(WORKSPACE_ID),
        },
        "subject": {"text": "Which policy applies to this customer?"},
    }
    assert parse_answer_question_input(serialized) == request
    assert parse_answer_question_input(serialized).canonical_json() == serialized


def test_input_and_output_contracts_are_immutable_and_outcomes_are_closed() -> None:
    request = AnswerQuestionInput(
        subject=QuestionSubject(text="Question"),
        scope=scope(),
        correlation=correlation(),
    )

    with pytest.raises(ValidationError):
        request.scope.workspace_id = ENVIRONMENT_ID  # type: ignore[misc]
    with pytest.raises(ValidationError):
        request.subject.text = "Changed"  # type: ignore[misc]

    assert {outcome.value for outcome in AnswerQuestionOutcome} == {
        "answered",
        "clarification_required",
        "abstained",
        "denied",
        "failed",
        "cancelled",
    }


def test_answer_question_output_accepts_structured_claim_lineage_and_citations() -> None:
    result = output()

    assert result.schema_version == ANSWER_QUESTION_OUTPUT_SCHEMA
    assert result.answer is not None
    assert result.answer.relationships[0].citation_ids == (CITATION_ID,)
    assert parse_answer_question_output(result.canonical_json()) == result


class FakeAnswerQuestionHandler:
    """Test-only adapter: it returns a prepared contract and calls no provider."""

    def __init__(self, result: AnswerQuestionOutput) -> None:
        self._result = result

    def execute(self, request: AnswerQuestionInput) -> AnswerQuestionOutput:
        assert request.correlation == self._result.correlation
        assert request.scope == self._result.scope
        return parse_answer_question_output(self._result.canonical_json())


def test_fake_handler_covers_every_terminal_outcome_without_a_provider() -> None:
    cases = [
        output(),
        AnswerQuestionOutput(
            correlation=correlation(),
            scope=scope(),
            outcome=AnswerQuestionOutcome.CLARIFICATION_REQUIRED,
            clarification=ClarificationRequest(prompt="Which policy should I use?"),
            reason=AnswerQuestionSafeReason(
                code=AnswerQuestionSafeReasonCode.AMBIGUOUS_QUESTION
            ),
        ),
        AnswerQuestionOutput(
            correlation=correlation(),
            scope=scope(),
            outcome=AnswerQuestionOutcome.ABSTAINED,
            reason=AnswerQuestionSafeReason(
                code=AnswerQuestionSafeReasonCode.INSUFFICIENT_SUPPORT
            ),
        ),
        AnswerQuestionOutput(
            correlation=correlation(),
            scope=scope(),
            outcome=AnswerQuestionOutcome.DENIED,
            reason=AnswerQuestionSafeReason(
                code=AnswerQuestionSafeReasonCode.AUTHORIZATION_DENIED
            ),
        ),
        AnswerQuestionOutput(
            correlation=correlation(),
            scope=scope(),
            outcome=AnswerQuestionOutcome.FAILED,
            reason=AnswerQuestionSafeReason(
                code=AnswerQuestionSafeReasonCode.MANDATORY_PROCESSING_FAILED
            ),
        ),
        AnswerQuestionOutput(
            correlation=correlation(),
            scope=scope(),
            outcome=AnswerQuestionOutcome.CANCELLED,
            reason=AnswerQuestionSafeReason(code=AnswerQuestionSafeReasonCode.CANCELLED),
        ),
    ]
    request = AnswerQuestionInput(
        subject=QuestionSubject(text="Question"),
        scope=scope(),
        correlation=correlation(),
    )

    observed = tuple(FakeAnswerQuestionHandler(case).execute(request).outcome for case in cases)

    assert observed == tuple(AnswerQuestionOutcome)


@pytest.mark.parametrize(
    ("outcome", "reason_code"),
    (
        (AnswerQuestionOutcome.CLARIFICATION_REQUIRED, AnswerQuestionSafeReasonCode.AMBIGUOUS_QUESTION),
        (AnswerQuestionOutcome.ABSTAINED, AnswerQuestionSafeReasonCode.INSUFFICIENT_SUPPORT),
        (AnswerQuestionOutcome.DENIED, AnswerQuestionSafeReasonCode.AUTHORIZATION_DENIED),
        (AnswerQuestionOutcome.FAILED, AnswerQuestionSafeReasonCode.MANDATORY_PROCESSING_FAILED),
        (AnswerQuestionOutcome.CANCELLED, AnswerQuestionSafeReasonCode.CANCELLED),
    ),
)
def test_each_non_answer_terminal_outcome_requires_a_safe_typed_payload(
    outcome: AnswerQuestionOutcome,
    reason_code: AnswerQuestionSafeReasonCode,
) -> None:
    values: dict[str, object] = {
        "correlation": correlation(),
        "scope": scope(),
        "outcome": outcome,
        "reason": AnswerQuestionSafeReason(code=reason_code),
    }
    if outcome is AnswerQuestionOutcome.CLARIFICATION_REQUIRED:
        values["clarification"] = ClarificationRequest(
            prompt="Which customer and policy should I use?"
        )

    result = AnswerQuestionOutput(**values)

    assert result.outcome is outcome
    assert parse_answer_question_output(result.canonical_json()) == result


def test_retrieval_outcomes_are_not_final_answer_outcomes() -> None:
    with pytest.raises(ValidationError):
        AnswerQuestionOutput(
            correlation=correlation(),
            scope=scope(),
            outcome=RetrievalOutcome.EVIDENCE_AVAILABLE,
        )


def test_retrieval_mapping_is_explicit_and_does_not_claim_evidence_is_an_answer() -> None:
    assert map_retrieval_outcome(RetrievalOutcome.EVIDENCE_AVAILABLE, accepted=True) is AnswerQuestionOutcome.ANSWERED
    assert map_retrieval_outcome(RetrievalOutcome.EVIDENCE_AVAILABLE, accepted=False) is AnswerQuestionOutcome.ABSTAINED
    assert map_retrieval_outcome(RetrievalOutcome.INSUFFICIENT_EVIDENCE) is AnswerQuestionOutcome.ABSTAINED
    assert map_retrieval_outcome(RetrievalOutcome.UNAVAILABLE) is AnswerQuestionOutcome.FAILED
    assert map_retrieval_outcome(RetrievalOutcome.FAILED) is AnswerQuestionOutcome.FAILED


def test_invalid_scope_schema_and_incompatible_result_fail_closed() -> None:
    with pytest.raises(ValidationError, match="non-zero"):
        AnswerQuestionScope(workspace_id=UUID(int=0), environment_id=ENVIRONMENT_ID)

    with pytest.raises(ValidationError, match="schema"):
        AnswerQuestionInput(
            schema_version="answer-question.input:v99",
            subject=QuestionSubject(text="Question"),
            scope=scope(),
            correlation=correlation(),
        )

    with pytest.raises(ValidationError, match="exactly one"):
        output(answer=None, reason=AnswerQuestionSafeReason(code=AnswerQuestionSafeReasonCode.INSUFFICIENT_SUPPORT))

def test_contract_rejects_unknown_fields_and_typed_parser_raises_contract_error() -> None:
    with pytest.raises(ValidationError):
        AnswerQuestionInput(
            subject=QuestionSubject(text="Question"),
            scope=scope(),
            correlation=correlation(),
            provider_payload={"secret": "must not be accepted"},
        )

    with pytest.raises(AnswerQuestionContractError):
        parse_answer_question_output('{"schema_version":"answer-question.output:v99"}')


def test_answer_content_rejects_missing_citations() -> None:
    with pytest.raises(ValidationError, match="citation"):
        AnswerContent.model_validate({**answer().model_dump(), "citations": ()})
