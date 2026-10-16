"""Domain contracts for immutable Q&A feedback observations.

Feedback is an observation of a trusted administrator judgment.  It is never
an update to an answer, citation, run, or evidence lineage record.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from spine.domain.common import ValidatedDefinitionModel


FEEDBACK_SCHEMA = "qa-feedback:v1"
MAX_FEEDBACK_COMMENT_LENGTH = 1_000
MAX_FEEDBACK_REASON_LENGTH = 256


def _require_id(value: UUID, field_name: str) -> UUID:
    if value.int == 0:
        raise ValueError(f"{field_name} must be a non-zero UUID")
    return value


def _require_time(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


class FeedbackTargetKind(str, Enum):
    ANSWER = "answer"
    CITATION = "citation"


class FeedbackOutcome(str, Enum):
    ANSWER_HELPFUL = "answer.helpful"
    ANSWER_INCORRECT = "answer.incorrect"
    CITATION_HELPFUL = "citation.helpful"
    CITATION_INCORRECT = "citation.incorrect"

    @property
    def target_kind(self) -> FeedbackTargetKind:
        return (
            FeedbackTargetKind.CITATION
            if self.value.startswith("citation.")
            else FeedbackTargetKind.ANSWER
        )


class FeedbackRecord(ValidatedDefinitionModel):
    """One immutable, tenant-scoped feedback observation."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )

    schema_version: Literal[FEEDBACK_SCHEMA] = FEEDBACK_SCHEMA
    feedback_id: UUID
    version: Literal[1] = 1
    workspace_id: UUID
    environment_id: UUID
    subject_id: UUID
    trace_id: UUID
    run_id: UUID
    message_id: UUID | None = None
    target_kind: FeedbackTargetKind
    target_id: UUID
    citation_id: UUID | None = None
    outcome: FeedbackOutcome
    comment: str | None = Field(default=None, max_length=MAX_FEEDBACK_COMMENT_LENGTH)
    reason: str | None = Field(default=None, max_length=MAX_FEEDBACK_REASON_LENGTH)
    created_at: datetime

    @field_validator(
        "feedback_id",
        "workspace_id",
        "environment_id",
        "subject_id",
        "trace_id",
        "run_id",
        "message_id",
        "target_id",
        "citation_id",
    )
    @classmethod
    def validate_ids(cls, value: UUID | None, info: Any) -> UUID | None:
        if value is not None:
            _require_id(value, info.field_name)
        return value

    @field_validator("comment", "reason")
    @classmethod
    def validate_optional_text(cls, value: str | None, info: Any) -> str | None:
        if value is not None and not value.strip():
            raise ValueError(f"{info.field_name} must not be blank")
        return value

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return _require_time(value, "created_at")

    @model_validator(mode="after")
    def validate_target(self) -> "FeedbackRecord":
        if self.outcome.target_kind is not self.target_kind:
            raise ValueError("feedback outcome does not match target kind")
        if self.target_kind is FeedbackTargetKind.CITATION:
            if self.citation_id != self.target_id:
                raise ValueError("citation feedback must identify its exact citation")
        return self


__all__ = [
    "FEEDBACK_SCHEMA",
    "MAX_FEEDBACK_COMMENT_LENGTH",
    "MAX_FEEDBACK_REASON_LENGTH",
    "FeedbackOutcome",
    "FeedbackRecord",
    "FeedbackTargetKind",
]
