"""Framework-independent outbox to Temporal start dispatch for a Workflow Run."""

from __future__ import annotations

from enum import Enum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from spine.application.diagnostics.context import DiagnosticContext
from spine.application.outbox.dispatch import (
    OutboxConsumerSuccess,
    RetryableOutboxConsumerFailure,
    TerminalOutboxConsumerFailure,
)
from spine.application.persistence.outbox import OutboxIntent
from spine.application.persistence.workflow_runs import (
    START_WORKFLOW_EVENT_SCHEMA_VERSION,
    START_WORKFLOW_EVENT_TYPE,
    StartWorkflowDispatchPayload,
)

WORKFLOW_RUN_START_TYPE = "spine.workflow.run-start:v1"


def workflow_id_for_run(run_id: UUID) -> str:
    if not isinstance(run_id, UUID) or run_id.int == 0:
        raise ValueError("run_id must be a non-zero UUID")
    return str(run_id)


class WorkflowRunStartInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workspace_id: UUID
    environment_id: UUID
    command_id: UUID
    run_id: UUID
    work_item_id: UUID
    workflow_key: str = Field(min_length=1, max_length=128)
    workflow_version_id: UUID
    workflow_version: str = Field(min_length=1, max_length=128)
    trace_id: UUID
    correlation_id: UUID | None = None
    causation_id: UUID | None = None

    @field_validator(
        "workspace_id", "environment_id", "command_id", "run_id", "work_item_id",
        "workflow_version_id", "trace_id", "correlation_id", "causation_id",
    )
    @classmethod
    def reject_zero_identity(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("workflow start identities must be non-zero")
        return value

    @property
    def workflow_id(self) -> str:
        return workflow_id_for_run(self.run_id)


class WorkflowRunStartHandle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workflow_id: str = Field(min_length=1, max_length=255)
    run_id: UUID


class TemporalWorkflowAlreadyStarted(Exception):
    def __init__(self, workflow_id: str) -> None:
        self.workflow_id = workflow_id
        super().__init__(workflow_id)


class WorkflowRunStartClient(Protocol):
    async def start_workflow(
        self, *, workflow_type: str, workflow_id: str, input: WorkflowRunStartInput
    ) -> WorkflowRunStartHandle: ...

    async def get_workflow(self, *, workflow_id: str) -> WorkflowRunStartHandle: ...


class WorkflowRunStartLauncher:
    def __init__(self, client: WorkflowRunStartClient) -> None:
        self._client = client

    async def start_or_resume(
        self, workflow_input: WorkflowRunStartInput
    ) -> WorkflowRunStartHandle:
        workflow_id = workflow_input.workflow_id
        try:
            handle = await self._client.start_workflow(
                workflow_type=WORKFLOW_RUN_START_TYPE,
                workflow_id=workflow_id,
                input=workflow_input,
            )
        except TemporalWorkflowAlreadyStarted as error:
            if error.workflow_id != workflow_id:
                raise ValueError("Temporal duplicate-start identity is invalid") from None
            handle = await self._client.get_workflow(workflow_id=workflow_id)
        if handle.workflow_id != workflow_id or handle.run_id != workflow_input.run_id:
            raise ValueError("Temporal workflow handle identity is invalid")
        return handle


class WorkflowRunStartDispatchConsumer:
    def __init__(self, launcher: WorkflowRunStartLauncher) -> None:
        self._launcher = launcher

    async def consume(
        self, intent: OutboxIntent, diagnostic_context: DiagnosticContext
    ) -> OutboxConsumerSuccess:
        payload = intent.payload
        if (
            intent.event_type != START_WORKFLOW_EVENT_TYPE
            or intent.schema_version != START_WORKFLOW_EVENT_SCHEMA_VERSION
            or type(payload) is not StartWorkflowDispatchPayload
            or intent.environment_id is None
            or intent.aggregate is None
            or intent.aggregate.object_type != "workflow_run"
            or intent.aggregate.object_id != payload.run_id
        ):
            raise TerminalOutboxConsumerFailure("workflow.start.invalid_intent")
        expected_context = DiagnosticContext(
            trace_id=intent.trace_id,
            correlation_id=intent.correlation_id,
            causation_id=intent.causation_id or intent.event_id,
        )
        if diagnostic_context != expected_context:
            raise TerminalOutboxConsumerFailure("workflow.start.invalid_intent")
        workflow_input = WorkflowRunStartInput(
            workspace_id=intent.workspace_id,
            environment_id=intent.environment_id,
            command_id=payload.command_id,
            run_id=payload.run_id,
            work_item_id=payload.work_item_id,
            workflow_key=payload.workflow_key,
            workflow_version_id=payload.workflow_version_id,
            workflow_version=payload.workflow_version,
            trace_id=intent.trace_id,
            correlation_id=intent.correlation_id,
            causation_id=intent.causation_id or intent.event_id,
        )
        try:
            await self._launcher.start_or_resume(workflow_input)
        except (ValueError, TypeError):
            raise TerminalOutboxConsumerFailure("workflow.start.invalid_intent") from None
        except Exception:
            raise RetryableOutboxConsumerFailure("workflow.start.temporal_unavailable") from None
        return OutboxConsumerSuccess(diagnostic_context=expected_context)


__all__ = [
    "TemporalWorkflowAlreadyStarted",
    "WORKFLOW_RUN_START_TYPE",
    "WorkflowRunStartClient",
    "WorkflowRunStartDispatchConsumer",
    "WorkflowRunStartHandle",
    "WorkflowRunStartInput",
    "WorkflowRunStartLauncher",
    "workflow_id_for_run",
]
