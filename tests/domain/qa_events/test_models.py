from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.qa_events import (
    AnswerDeltaPayload,
    AbstainedPayload,
    CancelledPayload,
    CitationAddedPayload,
    CompletedPayload,
    DeniedPayload,
    DisclosureReceipt,
    FailedPayload,
    HeartbeatPayload,
    QAEvent,
    QAEventContractError,
    QAEventType,
    QACitation,
    QACitationLocator,
    ClarificationRequiredPayload,
    RunStartedPayload,
    StageChangedPayload,
    parse_qa_event,
)


def uid(value: int) -> UUID:
    return UUID(int=value)


def receipt() -> DisclosureReceipt:
    return DisclosureReceipt(
        receipt_id=uid(70),
        accepted_output_id=uid(71),
        validation_reference=uid(72),
    )


def citation() -> QACitation:
    return QACitation(
        citation_id=uid(73),
        claim_id=uid(74),
        answer_id=uid(71),
        source_revision_id=uid(75),
        locator=QACitationLocator(kind="page", value="4"),
    )


def event(event_type: QAEventType, payload: object, sequence: int = 1) -> QAEvent:
    return QAEvent(
        event_id=uid(100 + sequence),
        sequence=sequence,
        session_id=uid(1),
        workspace_id=uid(2),
        environment_id=uid(3),
        message_id=uid(4),
        run_id=uid(5),
        trace_id=uid(6),
        type=event_type,
        payload=payload,
    )


@pytest.mark.parametrize(
    ("event_type", "payload"),
    (
        (QAEventType.RUN_STARTED, RunStartedPayload(stage="accepted")),
        (QAEventType.STAGE_CHANGED, StageChangedPayload(stage="grounding")),
        (
            QAEventType.ANSWER_DELTA,
            AnswerDeltaPayload(delta="accepted fragment", disclosure=receipt()),
        ),
        (
            QAEventType.CITATION_ADDED,
            CitationAddedPayload(citation=citation(), disclosure=receipt()),
        ),
        (
            QAEventType.CLARIFICATION_REQUIRED,
            ClarificationRequiredPayload(prompt="Уточните период."),
        ),
        (QAEventType.ABSTAINED, AbstainedPayload(reason="insufficient_evidence")),
        (QAEventType.DENIED, DeniedPayload(reason="authorization_denied")),
        (QAEventType.COMPLETED, CompletedPayload(outcome="answered")),
        (QAEventType.FAILED, FailedPayload(reason="internal_failure")),
        (QAEventType.CANCELLED, CancelledPayload(reason="request_cancelled")),
        (QAEventType.HEARTBEAT, HeartbeatPayload()),
    ),
)
def test_every_event_type_has_a_versioned_closed_wire_contract(
    event_type: QAEventType, payload: object
) -> None:
    original = event(event_type, payload)

    decoded = parse_qa_event(original.model_dump(mode="json"))

    assert decoded == original
    assert decoded.schema_version == "qa-event:v1"
    assert decoded.protocol_version == 1


def test_event_rejects_payload_for_a_different_event_type() -> None:
    with pytest.raises(ValidationError):
        event(QAEventType.COMPLETED, RunStartedPayload(stage="accepted"))


def test_protected_event_requires_an_authorized_accepted_output_receipt() -> None:
    with pytest.raises(ValidationError):
        AnswerDeltaPayload(
            delta="candidate fragment",
            disclosure={"receipt_id": uid(70), "status": "authorized"},
        )

    with pytest.raises(ValidationError, match="authorized"):
        AnswerDeltaPayload(
            delta="candidate fragment",
            disclosure={
                "receipt_id": uid(70),
                "accepted_output_id": uid(71),
                "validation_reference": uid(72),
                "status": "pending",
            },
        )


def test_citation_event_requires_receipt_for_the_same_accepted_answer() -> None:
    mismatched = citation().model_copy(update={"answer_id": uid(999)})

    with pytest.raises(ValidationError, match="accepted output"):
        CitationAddedPayload(citation=mismatched, disclosure=receipt())


def test_parser_rejects_candidate_and_provider_fields_without_echoing_content() -> None:
    value = event(QAEventType.HEARTBEAT, HeartbeatPayload()).model_dump(mode="json")
    value["payload"] = {"candidate_output": "secret candidate answer"}

    with pytest.raises(QAEventContractError) as error:
        parse_qa_event(value)

    assert "secret candidate answer" not in str(error.value)


def test_event_rejects_citation_with_a_different_accepted_output() -> None:
    value = citation().model_copy(update={"answer_id": uid(999)})
    with pytest.raises(ValidationError):
        event(
            QAEventType.CITATION_ADDED,
            CitationAddedPayload(citation=value, disclosure=receipt()),
        )
