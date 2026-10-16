"""Versioned, transport-neutral Q&A event and disclosure contracts.

The event envelope is deliberately independent of FastAPI and an SSE client.
It is safe to persist and replay, but it is not a receipt that a client saw an
event.  Protected event payloads carry an opaque receipt issued by the grounded
answer disclosure boundary; candidate output can never satisfy that contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Annotated, Any, Literal, TypeAlias
from uuid import UUID

from pydantic import ConfigDict, Field, StringConstraints, field_validator, model_validator

from spine.domain.common import ValidatedDefinitionModel


QA_EVENT_SCHEMA = "qa-event:v1"
QA_DISCLOSURE_RECEIPT_SCHEMA = "qa-disclosure-receipt:v1"
QA_CITATION_SCHEMA = "qa-event-citation:v1"
QA_CITATION_LOCATOR_SCHEMA = "qa-event-citation-locator:v1"
QA_EVENT_PROTOCOL_VERSION = 1
_MAX_TEXT_LENGTH = 32_768
_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_.:-]{0,127}$"
StageIdentifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=_IDENTIFIER_PATTERN,
    ),
]


class QAEventContractError(ValueError):
    """Raised when a serialized Q&A event cannot be accepted safely."""


class QAEventType(str, Enum):
    RUN_STARTED = "run_started"
    STAGE_CHANGED = "stage_changed"
    ANSWER_DELTA = "answer_delta"
    CITATION_ADDED = "citation_added"
    CLARIFICATION_REQUIRED = "clarification_required"
    ABSTAINED = "abstained"
    DENIED = "denied"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    HEARTBEAT = "heartbeat"


QA_TERMINAL_EVENT_TYPES = frozenset(
    {
        QAEventType.ABSTAINED,
        QAEventType.DENIED,
        QAEventType.COMPLETED,
        QAEventType.FAILED,
        QAEventType.CANCELLED,
    }
)
QA_PROTECTED_EVENT_TYPES = frozenset(
    {QAEventType.ANSWER_DELTA, QAEventType.CITATION_ADDED}
)


def _non_zero(value: UUID, field_name: str) -> UUID:
    if value.int == 0:
        raise ValueError(f"{field_name} must be non-zero")
    return value


class _QAEventModel(ValidatedDefinitionModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )


class DisclosureReceipt(_QAEventModel):
    """Opaque proof that protected output passed the disclosure boundary.

    ``accepted_output_id`` is an identity only.  The accepted answer, candidate
    text and validation evidence remain behind their owning authorization port.
    """

    schema_version: Literal[QA_DISCLOSURE_RECEIPT_SCHEMA] = QA_DISCLOSURE_RECEIPT_SCHEMA
    receipt_id: UUID
    status: Literal["authorized"] = "authorized"
    accepted_output_id: UUID
    validation_reference: UUID

    @field_validator("receipt_id", "accepted_output_id", "validation_reference")
    @classmethod
    def validate_identity(cls, value: UUID, info: Any) -> UUID:
        return _non_zero(value, info.field_name)


class QACitationLocator(_QAEventModel):
    """Content-free, stable locator used by the event wire contract."""

    schema_version: Literal[QA_CITATION_LOCATOR_SCHEMA] = QA_CITATION_LOCATOR_SCHEMA
    kind: Literal[
        "page",
        "sheet_row",
        "message",
        "characters",
        "character_range",
        "record_field",
        "custom",
    ]
    value: str = Field(min_length=1, max_length=512)


class QACitation(_QAEventModel):
    """Safe citation identity without excerpts, titles, URLs or source text."""

    schema_version: Literal[QA_CITATION_SCHEMA] = QA_CITATION_SCHEMA
    citation_id: UUID
    claim_id: UUID
    answer_id: UUID
    source_revision_id: UUID
    locator: QACitationLocator

    @field_validator(
        "citation_id",
        "claim_id",
        "answer_id",
        "source_revision_id",
    )
    @classmethod
    def validate_identity(cls, value: UUID, info: Any) -> UUID:
        return _non_zero(value, info.field_name)


class RunStartedPayload(_QAEventModel):
    stage: StageIdentifier


class StageChangedPayload(_QAEventModel):
    stage: StageIdentifier


class AnswerDeltaPayload(_QAEventModel):
    delta: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)
    disclosure: DisclosureReceipt

    @field_validator("delta")
    @classmethod
    def validate_delta(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("answer delta must not be blank")
        return value


class CitationAddedPayload(_QAEventModel):
    citation: QACitation
    disclosure: DisclosureReceipt

    @model_validator(mode="after")
    def validate_answer_receipt(self) -> CitationAddedPayload:
        if self.citation.answer_id != self.disclosure.accepted_output_id:
            raise ValueError("citation disclosure receipt does not match accepted output")
        return self


class ClarificationRequiredPayload(_QAEventModel):
    prompt: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("clarification prompt must not be blank")
        return value


class AbstainedPayload(_QAEventModel):
    reason: Literal[
        "no_usable_evidence",
        "insufficient_evidence",
        "scope_incomplete",
        "clarification_required",
        "insufficient_support",
        "unresolved_conflict",
        "incomplete_authorized_coverage",
        "unmet_freshness",
        "evidence_invalidated",
        "rejected_candidate",
    ]


class DeniedPayload(_QAEventModel):
    reason: Literal["authorization_denied"]


class CompletedPayload(_QAEventModel):
    outcome: Literal["answered", "clarification_required"]


class FailedPayload(_QAEventModel):
    reason: Literal[
        "validation_failed",
        "stream_timeout",
        "stream_unavailable",
        "protocol_error",
        "internal_failure",
        "mandatory_processing_failed",
    ]


class CancelledPayload(_QAEventModel):
    reason: Literal["request_cancelled"]


class HeartbeatPayload(_QAEventModel):
    """An intentionally empty payload that carries no client content."""

    pass


QAEventPayload: TypeAlias = (
    RunStartedPayload
    | StageChangedPayload
    | AnswerDeltaPayload
    | CitationAddedPayload
    | ClarificationRequiredPayload
    | AbstainedPayload
    | DeniedPayload
    | CompletedPayload
    | FailedPayload
    | CancelledPayload
    | HeartbeatPayload
)
_PAYLOAD_TYPES: dict[QAEventType, type[_QAEventModel]] = {
    QAEventType.RUN_STARTED: RunStartedPayload,
    QAEventType.STAGE_CHANGED: StageChangedPayload,
    QAEventType.ANSWER_DELTA: AnswerDeltaPayload,
    QAEventType.CITATION_ADDED: CitationAddedPayload,
    QAEventType.CLARIFICATION_REQUIRED: ClarificationRequiredPayload,
    QAEventType.ABSTAINED: AbstainedPayload,
    QAEventType.DENIED: DeniedPayload,
    QAEventType.COMPLETED: CompletedPayload,
    QAEventType.FAILED: FailedPayload,
    QAEventType.CANCELLED: CancelledPayload,
    QAEventType.HEARTBEAT: HeartbeatPayload,
}

_FORBIDDEN_KEYS = frozenset(
    {
        "candidateoutput",
        "chainofthought",
        "credentials",
        "providererror",
        "rawproviderpayload",
    }
)


def _reject_forbidden_fields(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).lower().replace("-", "").replace("_", "")
            if normalized in _FORBIDDEN_KEYS:
                raise QAEventContractError(
                    "Q&A event contains undisclosable provider content"
                )
            _reject_forbidden_fields(nested)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_forbidden_fields(item)


class QAEvent(_QAEventModel):
    """One immutable, safe emission in a resumable Q&A event stream."""

    schema_version: Literal[QA_EVENT_SCHEMA] = QA_EVENT_SCHEMA
    protocol_version: Literal[QA_EVENT_PROTOCOL_VERSION] = QA_EVENT_PROTOCOL_VERSION
    event_id: UUID
    sequence: int = Field(gt=0)
    type: QAEventType
    session_id: UUID
    workspace_id: UUID
    environment_id: UUID
    message_id: UUID | None = None
    run_id: UUID | None = None
    trace_id: UUID
    payload: QAEventPayload

    @model_validator(mode="before")
    @classmethod
    def validate_wire_payload(cls, values: Any) -> Any:
        if not isinstance(values, Mapping):
            return values
        _reject_forbidden_fields(values)
        data = dict(values)
        event_type = data.get("type")
        try:
            event_kind = QAEventType(event_type)
        except (TypeError, ValueError):
            return data
        payload_type = _PAYLOAD_TYPES[event_kind]
        if not isinstance(data.get("payload"), payload_type):
            data["payload"] = payload_type.model_validate(data.get("payload"))
        return data

    @field_validator(
        "event_id",
        "session_id",
        "workspace_id",
        "environment_id",
        "message_id",
        "run_id",
        "trace_id",
    )
    @classmethod
    def validate_identity(cls, value: UUID | None, info: Any) -> UUID | None:
        if value is not None:
            return _non_zero(value, info.field_name)
        return value

    @model_validator(mode="after")
    def validate_payload_type(self) -> QAEvent:
        expected = _PAYLOAD_TYPES[self.type]
        if type(self.payload) is not expected:
            raise ValueError(f"payload does not match event type {self.type.value}")
        if self.type is QAEventType.CITATION_ADDED:
            payload = self.payload
            assert isinstance(payload, CitationAddedPayload)
            if payload.citation.answer_id != payload.disclosure.accepted_output_id:
                raise ValueError("citation disclosure receipt does not match accepted output")
        return self

    @property
    def is_terminal(self) -> bool:
        return self.type in QA_TERMINAL_EVENT_TYPES

    @property
    def is_protected(self) -> bool:
        return self.type in QA_PROTECTED_EVENT_TYPES


def parse_qa_event(value: Mapping[str, Any] | QAEvent | str) -> QAEvent:
    """Parse one event at an external wire boundary without leaking details."""

    try:
        if isinstance(value, str):
            return QAEvent.model_validate_json(value)
        return QAEvent.model_validate(value)
    except QAEventContractError:
        raise
    except (TypeError, ValueError) as exc:
        raise QAEventContractError(
            "Q&A event is not a compatible versioned event contract"
        ) from exc


__all__ = [
    "AbstainedPayload",
    "AnswerDeltaPayload",
    "CancelledPayload",
    "CitationAddedPayload",
    "CompletedPayload",
    "DeniedPayload",
    "DisclosureReceipt",
    "FailedPayload",
    "HeartbeatPayload",
    "QA_CITATION_LOCATOR_SCHEMA",
    "QA_CITATION_SCHEMA",
    "QA_DISCLOSURE_RECEIPT_SCHEMA",
    "QA_EVENT_PROTOCOL_VERSION",
    "QA_EVENT_SCHEMA",
    "QA_PROTECTED_EVENT_TYPES",
    "QA_TERMINAL_EVENT_TYPES",
    "QAEvent",
    "QAEventContractError",
    "QAEventPayload",
    "QAEventType",
    "QACitation",
    "QACitationLocator",
    "ClarificationRequiredPayload",
    "RunStartedPayload",
    "StageChangedPayload",
    "parse_qa_event",
]
