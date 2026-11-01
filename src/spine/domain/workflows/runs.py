"""Canonical identities and trigger contracts for durable workflow runs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, field_validator

from spine.domain.common import ActorRef, DefinitionModel


class AcceptedSourceRevisionRef(DefinitionModel):
    """A tenant-local reference to a source revision already accepted by Spine."""

    source_object_id: UUID
    source_revision_id: UUID
    provenance_id: UUID

    @field_validator("source_object_id", "source_revision_id", "provenance_id")
    @classmethod
    def reject_zero_identity(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("source reference identities must be non-zero")
        return value


class ClientRequestV1(DefinitionModel):
    """The first conversational message and its accepted source lineage."""

    schema_version: Literal[1] = 1
    message: str = Field(min_length=1, max_length=32_768)
    accepted_sources: tuple[AcceptedSourceRevisionRef, ...] = ()

    @field_validator("message")
    @classmethod
    def reject_blank_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("client request message must not be blank")
        return value


class WorkflowRunState(str, Enum):
    ADMITTED = "admitted"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowRun(DefinitionModel):
    """Canonical, version-pinned execution identity owned by PostgreSQL."""

    run_id: UUID = Field(default_factory=uuid4)
    command_id: UUID
    work_item_id: UUID
    workspace_id: UUID
    environment_id: UUID
    workflow_key: str = Field(min_length=1, max_length=128)
    workflow_version_id: UUID
    workflow_version: str = Field(min_length=1, max_length=128)
    state: WorkflowRunState = WorkflowRunState.ADMITTED
    run_version: int = Field(default=1, ge=1)
    started_by: ActorRef
    client_request: ClientRequestV1
    trace_id: UUID
    correlation_id: UUID
    causation_id: UUID
    created_at: datetime

    @field_validator(
        "run_id", "command_id", "work_item_id", "workspace_id", "environment_id",
        "workflow_version_id", "trace_id", "correlation_id", "causation_id",
    )
    @classmethod
    def reject_zero_identity(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("workflow run identities must be non-zero")
        return value

    @field_validator("created_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("workflow run timestamps must be aware UTC")
        return value.astimezone(timezone.utc)


class WorkflowCommandReceipt(DefinitionModel):
    """Stable confirmation that one start command was admitted."""

    schema_version: Literal[1] = 1
    command_id: UUID
    work_item_id: UUID
    run_id: UUID
    workflow_version_id: UUID
    state: WorkflowRunState
    run_version: int = Field(default=1, ge=1)
    admission: Literal["accepted", "replayed"]


class WorkflowEvent(DefinitionModel):
    """Disclosure-safe canonical event used to reconstruct a run feed."""

    event_id: UUID
    run_id: UUID
    sequence: int = Field(ge=1)
    event_type: Literal[
        "workflow.run-started",
        "workflow.context-added",
        "workflow.requirement-edited",
        "workflow.requirements-reviewed",
        "workflow.requirements-rework-requested",
        "workflow.clarification-requested",
        "workflow.clarification-answered",
        "workflow.run-cancelled",
    ]
    event_schema_version: Literal[1] = 1
    run_version: int = Field(ge=1)
    summary: str = Field(min_length=1, max_length=255)
    correlation_id: UUID
    causation_id: UUID
    occurred_at: datetime

    @field_validator("event_id", "run_id", "correlation_id", "causation_id")
    @classmethod
    def reject_zero_event_identity(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("workflow event identities must be non-zero")
        return value

    @field_validator("occurred_at")
    @classmethod
    def normalize_event_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("workflow event timestamps must be aware UTC")
        return value.astimezone(timezone.utc)


class WorkflowEventPage(DefinitionModel):
    """One stable sequence-ordered page from the canonical event log."""

    items: tuple[WorkflowEvent, ...]
    next_sequence: int | None = Field(default=None, ge=1)


class WorkflowRunSnapshot(DefinitionModel):
    """Disclosure-safe canonical run read model for product clients."""

    schema_version: Literal[1] = 1
    run_id: UUID
    work_item_id: UUID
    workspace_id: UUID
    environment_id: UUID
    workflow_key: str
    workflow_version_id: UUID
    workflow_version: str
    state: WorkflowRunState
    run_version: int = Field(ge=1)
    allowed_commands: tuple[
        Literal[
            "add_context",
            "edit_requirement",
            "reclassify_requirement",
            "save_review",
            "request_rework",
            "accept_requirements",
            "answer_clarification",
            "cancel",
        ],
        ...,
    ]
    created_at: datetime


__all__ = [
    "AcceptedSourceRevisionRef",
    "ClientRequestV1",
    "WorkflowCommandReceipt",
    "WorkflowEvent",
    "WorkflowEventPage",
    "WorkflowRun",
    "WorkflowRunSnapshot",
    "WorkflowRunState",
]
