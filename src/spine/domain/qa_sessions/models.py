"""Immutable, tenant-scoped Q&A session and message records.

Session history is navigation state.  It is intentionally not an evidence
contract and does not contain any retrieval or answer-authority semantics.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from spine.domain.capabilities.answer_question import AnswerQuestionOutcome
from spine.domain.common import ValidatedDefinitionModel


_MAX_TEXT_LENGTH = 32_768


def _nonzero(value: UUID, field_name: str) -> UUID:
    if value.int == 0:
        raise ValueError(f"{field_name} must be non-zero")
    return value


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


class QASessionStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class QAMessageState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    TERMINAL = "terminal"


class QAMessageDisclosureEligibility(str, Enum):
    UNKNOWN = "unknown"
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"


class _QASessionModel(ValidatedDefinitionModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )


class QASession(_QASessionModel):
    """One immutable version of a tenant-scoped Q&A navigation session."""

    schema_version: Literal[1] = 1
    session_id: UUID
    workspace_id: UUID
    environment_id: UUID
    subject_id: UUID
    version: int = Field(ge=1)
    status: QASessionStatus
    created_at: datetime
    updated_at: datetime

    @field_validator("session_id", "workspace_id", "environment_id", "subject_id")
    @classmethod
    def validate_id(cls, value: UUID, info: object) -> UUID:
        return _nonzero(value, getattr(info, "field_name", "identifier"))

    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_time(cls, value: datetime, info: object) -> datetime:
        return _aware(value, getattr(info, "field_name", "timestamp"))

    @model_validator(mode="after")
    def validate_ordering(self) -> "QASession":
        if self.updated_at < self.created_at:
            raise ValueError("session updated_at cannot precede created_at")
        return self


class QAMessage(_QASessionModel):
    """One immutable version of a question/answer turn.

    ``question`` and ``answer`` are protected content.  Readers must obtain a
    fresh disclosure decision before either field is returned.
    """

    schema_version: Literal[1] = 1
    message_id: UUID
    session_id: UUID
    workspace_id: UUID
    environment_id: UUID
    version: int = Field(ge=1)
    ordinal: int = Field(ge=1)
    trace_id: UUID
    run_id: UUID | None = None
    question: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)
    answer: str | None = Field(default=None, max_length=_MAX_TEXT_LENGTH)
    state: QAMessageState
    terminal_outcome: AnswerQuestionOutcome | None = None
    cancelled: bool = False
    disclosure_dependency_ids: tuple[UUID, ...] = ()
    disclosure_eligibility: QAMessageDisclosureEligibility = (
        QAMessageDisclosureEligibility.UNKNOWN
    )
    created_at: datetime
    updated_at: datetime

    @field_validator(
        "message_id",
        "session_id",
        "workspace_id",
        "environment_id",
        "trace_id",
        "run_id",
    )
    @classmethod
    def validate_id(cls, value: UUID | None, info: object) -> UUID | None:
        if value is None:
            return None
        return _nonzero(value, getattr(info, "field_name", "identifier"))

    @field_validator("question", "answer")
    @classmethod
    def validate_text(cls, value: str | None, info: object) -> str | None:
        if value is not None and not value.strip():
            raise ValueError(f"{getattr(info, 'field_name', 'content')} must not be blank")
        return value

    @field_validator("disclosure_dependency_ids")
    @classmethod
    def validate_dependencies(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if any(item.int == 0 for item in value):
            raise ValueError("disclosure dependency IDs must be non-zero")
        if len(set(value)) != len(value):
            raise ValueError("disclosure dependency IDs must be unique")
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_time(cls, value: datetime, info: object) -> datetime:
        return _aware(value, getattr(info, "field_name", "timestamp"))

    @model_validator(mode="after")
    def validate_state(self) -> "QAMessage":
        if self.updated_at < self.created_at:
            raise ValueError("message updated_at cannot precede created_at")
        if self.state is QAMessageState.TERMINAL:
            if self.terminal_outcome is None:
                raise ValueError("terminal messages require an outcome")
            if self.cancelled is not (self.terminal_outcome is AnswerQuestionOutcome.CANCELLED):
                raise ValueError("cancelled must match the terminal cancelled outcome")
        elif self.terminal_outcome is not None or self.cancelled or self.answer is not None:
            raise ValueError("non-terminal messages cannot contain terminal content")
        if self.terminal_outcome is AnswerQuestionOutcome.ANSWERED and not self.answer:
            raise ValueError("answered messages require answer content")
        if self.terminal_outcome is not AnswerQuestionOutcome.ANSWERED and self.answer is not None:
            raise ValueError("only answered messages may contain answer content")
        return self


__all__ = [
    "QAMessage",
    "QAMessageDisclosureEligibility",
    "QAMessageState",
    "QASession",
    "QASessionStatus",
]
