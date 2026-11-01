"""Persistence contracts for atomic admission of canonical workflow runs."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from spine.application.persistence.command_digest import CommandDigest, digest_command
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import InvalidPersistenceContextError
from spine.application.workflow_runs import StartWorkflowCommand, WorkflowCommand
from spine.domain.workflows import (
    RequirementsArtifact,
    WorkflowCommandReceipt,
    WorkflowEventPage,
    WorkflowRun,
)


START_WORKFLOW_OPERATION = PersistenceOperation("workflow.run.start")
START_WORKFLOW_SCHEMA_VERSION = 1
START_WORKFLOW_RESULT_TYPE = "workflow_run_admission"
START_WORKFLOW_EVENT_TYPE = "workflow.run-started"
START_WORKFLOW_EVENT_SCHEMA_VERSION = 1
COMMAND_WORKFLOW_OPERATION = PersistenceOperation("workflow.run.command")
COMMAND_WORKFLOW_SCHEMA_VERSION = 1
COMMAND_WORKFLOW_RESULT_TYPE = "workflow_command_receipt"
WORKFLOW_SIGNAL_EVENT_SCHEMA_VERSION = 1
CLARIFICATION_REQUESTED_EVENT_TYPE = "workflow.clarification-requested"
CLARIFICATION_ANSWERED_EVENT_TYPE = "workflow.clarification-answered"
WORKFLOW_CANCELLED_EVENT_TYPE = "workflow.run-cancelled"
REQUIREMENTS_ACCEPTED_EVENT_TYPE = "workflow.requirements-accepted"


class StartWorkflowDispatchPayload(BaseModel):
    """Content-safe Temporal start intent with stable canonical identities."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    command_id: UUID
    run_id: UUID
    work_item_id: UUID
    workflow_key: str = Field(min_length=1, max_length=128)
    workflow_version_id: UUID
    workflow_version: str = Field(min_length=1, max_length=128)

    @field_validator("command_id", "run_id", "work_item_id", "workflow_version_id")
    @classmethod
    def reject_zero_identity(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("workflow dispatch identities must be non-zero")
        return value


class ClarificationRequestedDispatchPayload(BaseModel):
    """Content-free signal that one canonical run entered human wait."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: UUID
    clarification_id: UUID
    artifact_id: UUID
    artifact_version: int = Field(ge=1, strict=True)
    workflow_version_id: UUID

    @field_validator("run_id", "clarification_id", "artifact_id", "workflow_version_id")
    @classmethod
    def reject_zero_identity(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("workflow clarification signal identities must be non-zero")
        return value


class ClarificationAnsweredDispatchPayload(BaseModel):
    """Content-free signal admitted after the immutable successor commits."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    command_id: UUID
    run_id: UUID
    clarification_id: UUID
    artifact_id: UUID
    artifact_version: int = Field(ge=2, strict=True)
    next_clarification_id: UUID | None = None
    workflow_version_id: UUID

    @field_validator(
        "command_id",
        "run_id",
        "clarification_id",
        "artifact_id",
        "next_clarification_id",
        "workflow_version_id",
    )
    @classmethod
    def reject_zero_identity(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("workflow clarification answer signal identities must be non-zero")
        return value


class WorkflowCancelledDispatchPayload(BaseModel):
    """Content-free cancellation signal for the existing durable run."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    command_id: UUID
    run_id: UUID
    workflow_version_id: UUID

    @field_validator("command_id", "run_id", "workflow_version_id")
    @classmethod
    def reject_zero_identity(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("workflow cancellation signal identities must be non-zero")
        return value


class RequirementsAcceptedDispatchPayload(BaseModel):
    """Content-free terminal signal for one exact accepted artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    command_id: UUID
    run_id: UUID
    artifact_id: UUID
    artifact_version: int = Field(ge=1, strict=True)
    artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    workflow_version_id: UUID

    @field_validator("command_id", "run_id", "artifact_id", "workflow_version_id")
    @classmethod
    def reject_zero_identity(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("requirements acceptance signal identities must be non-zero")
        return value


class WorkflowRunAdmissionRepository(Protocol):
    async def admit(self, command: StartWorkflowCommand) -> WorkflowCommandReceipt: ...

    async def get_run(
        self, *, workspace_id: UUID, environment_id: UUID, run_id: UUID
    ) -> WorkflowRun | None: ...

    async def dispatch(
        self,
        command: WorkflowCommand,
        *,
        clarification_successor: RequirementsArtifact | None = None,
    ) -> WorkflowCommandReceipt: ...

    async def wait_for_clarification(
        self, artifact: RequirementsArtifact
    ) -> WorkflowRun: ...

    async def list_events(
        self, *, workspace_id: UUID, environment_id: UUID, run_id: UUID,
        after_sequence: int | None, limit: int,
    ) -> WorkflowEventPage: ...


def workflow_command_digest(command: WorkflowCommand) -> CommandDigest:
    """Return the canonical digest shared by replay and persistence."""

    return digest_command(
        operation=COMMAND_WORKFLOW_OPERATION,
        operation_schema_version=COMMAND_WORKFLOW_SCHEMA_VERSION,
        payload={
            "command_id": command.command_id,
            "run_id": command.run_id,
            "workspace_id": command.workspace_id,
            "environment_id": command.environment_id,
            "actor": command.actor.model_dump(mode="json"),
            "command_type": command.command_type,
            "command_schema_version": command.command_schema_version,
            "expected_run_version": command.expected_run_version,
            "payload": command.payload.model_dump(mode="json"),
            "artifact_refs": [item.model_dump(mode="json") for item in command.artifact_refs],
            "correlation_id": command.correlation_id,
            "causation_id": command.causation_id,
        },
    )


def validate_workflow_run_context(
    context: TrustedPersistenceContext,
    command: StartWorkflowCommand,
) -> None:
    """Ensure command scope and actor match the server-issued authorization context."""

    actor_id = context.acting_subject_id or context.service_principal_id
    if (
        not isinstance(context.scope, EnvironmentScope)
        or context.operation != START_WORKFLOW_OPERATION
        or actor_id != command.actor.id
        or context.scope.workspace_id != command.workspace_id
        or context.scope.environment_id != command.environment_id
        or context.trace_id != command.trace_id
    ):
        raise InvalidPersistenceContextError("Workflow start context is invalid.")


def validate_workflow_command_context(
    context: TrustedPersistenceContext,
    command: WorkflowCommand,
) -> None:
    """Reject any command whose authority differs from server-issued context."""

    actor_id = context.acting_subject_id or context.service_principal_id
    if (
        not isinstance(context.scope, EnvironmentScope)
        or context.operation != COMMAND_WORKFLOW_OPERATION
        or actor_id != command.actor.id
        or context.scope.workspace_id != command.workspace_id
        or context.scope.environment_id != command.environment_id
        or context.trace_id != command.trace_id
    ):
        raise InvalidPersistenceContextError("Workflow command context is invalid.")


def register_workflow_run_outbox_event(registry: object) -> None:
    register = getattr(registry, "register")
    register(
        event_type=START_WORKFLOW_EVENT_TYPE,
        schema_version=START_WORKFLOW_EVENT_SCHEMA_VERSION,
        payload_type=StartWorkflowDispatchPayload,
    )
    register(
        event_type=CLARIFICATION_REQUESTED_EVENT_TYPE,
        schema_version=WORKFLOW_SIGNAL_EVENT_SCHEMA_VERSION,
        payload_type=ClarificationRequestedDispatchPayload,
    )
    register(
        event_type=CLARIFICATION_ANSWERED_EVENT_TYPE,
        schema_version=WORKFLOW_SIGNAL_EVENT_SCHEMA_VERSION,
        payload_type=ClarificationAnsweredDispatchPayload,
    )
    register(
        event_type=WORKFLOW_CANCELLED_EVENT_TYPE,
        schema_version=WORKFLOW_SIGNAL_EVENT_SCHEMA_VERSION,
        payload_type=WorkflowCancelledDispatchPayload,
    )
    register(
        event_type=REQUIREMENTS_ACCEPTED_EVENT_TYPE,
        schema_version=WORKFLOW_SIGNAL_EVENT_SCHEMA_VERSION,
        payload_type=RequirementsAcceptedDispatchPayload,
    )


__all__ = [
    "COMMAND_WORKFLOW_OPERATION",
    "COMMAND_WORKFLOW_RESULT_TYPE",
    "COMMAND_WORKFLOW_SCHEMA_VERSION",
    "CLARIFICATION_ANSWERED_EVENT_TYPE",
    "CLARIFICATION_REQUESTED_EVENT_TYPE",
    "REQUIREMENTS_ACCEPTED_EVENT_TYPE",
    "START_WORKFLOW_EVENT_SCHEMA_VERSION",
    "START_WORKFLOW_EVENT_TYPE",
    "START_WORKFLOW_OPERATION",
    "START_WORKFLOW_RESULT_TYPE",
    "START_WORKFLOW_SCHEMA_VERSION",
    "StartWorkflowDispatchPayload",
    "ClarificationAnsweredDispatchPayload",
    "ClarificationRequestedDispatchPayload",
    "WORKFLOW_CANCELLED_EVENT_TYPE",
    "WORKFLOW_SIGNAL_EVENT_SCHEMA_VERSION",
    "WorkflowCancelledDispatchPayload",
    "RequirementsAcceptedDispatchPayload",
    "WorkflowRunAdmissionRepository",
    "register_workflow_run_outbox_event",
    "validate_workflow_run_context",
    "validate_workflow_command_context",
    "workflow_command_digest",
]
