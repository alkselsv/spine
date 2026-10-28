"""Persistence contracts for atomic admission of canonical workflow runs."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import InvalidPersistenceContextError
from spine.application.workflow_runs import StartWorkflowCommand, WorkflowCommand
from spine.domain.workflows import WorkflowCommandReceipt, WorkflowEventPage, WorkflowRun


START_WORKFLOW_OPERATION = PersistenceOperation("workflow.run.start")
START_WORKFLOW_SCHEMA_VERSION = 1
START_WORKFLOW_RESULT_TYPE = "workflow_run_admission"
START_WORKFLOW_EVENT_TYPE = "workflow.run-started"
START_WORKFLOW_EVENT_SCHEMA_VERSION = 1
COMMAND_WORKFLOW_OPERATION = PersistenceOperation("workflow.run.command")
COMMAND_WORKFLOW_SCHEMA_VERSION = 1
COMMAND_WORKFLOW_RESULT_TYPE = "workflow_command_receipt"


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


class WorkflowRunAdmissionRepository(Protocol):
    async def admit(self, command: StartWorkflowCommand) -> WorkflowCommandReceipt: ...

    async def get_run(
        self, *, workspace_id: UUID, environment_id: UUID, run_id: UUID
    ) -> WorkflowRun | None: ...

    async def dispatch(self, command: WorkflowCommand) -> WorkflowCommandReceipt: ...

    async def list_events(
        self, *, workspace_id: UUID, environment_id: UUID, run_id: UUID,
        after_sequence: int | None, limit: int,
    ) -> WorkflowEventPage: ...


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


__all__ = [
    "COMMAND_WORKFLOW_OPERATION",
    "COMMAND_WORKFLOW_RESULT_TYPE",
    "COMMAND_WORKFLOW_SCHEMA_VERSION",
    "START_WORKFLOW_EVENT_SCHEMA_VERSION",
    "START_WORKFLOW_EVENT_TYPE",
    "START_WORKFLOW_OPERATION",
    "START_WORKFLOW_RESULT_TYPE",
    "START_WORKFLOW_SCHEMA_VERSION",
    "StartWorkflowDispatchPayload",
    "WorkflowRunAdmissionRepository",
    "register_workflow_run_outbox_event",
    "validate_workflow_run_context",
    "validate_workflow_command_context",
]
