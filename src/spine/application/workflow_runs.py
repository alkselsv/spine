"""Application boundary for starting and reading canonical workflow runs."""

from __future__ import annotations

from typing import Literal, Protocol
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator

from spine.domain.common import ActorRef, EnvironmentKind, SpineModel
from spine.domain.workflows import (
    ClientRequestV1,
    WorkflowCommandReceipt,
    WorkflowEventPage,
    WorkflowRun,
    WorkflowRunSnapshot,
)


class StartWorkflowCommand(SpineModel):
    """Trusted, typed intent to create one WorkItem and WorkflowRun."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    command_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    workspace_id: UUID
    environment_id: UUID
    actor: ActorRef
    environment: EnvironmentKind | None = None
    workflow_key: str = Field(min_length=1, max_length=128)
    workflow_version_id: UUID
    workflow_version: str = Field(min_length=1, max_length=128)
    work_item_key: str = Field(min_length=1, max_length=255)
    client_request: ClientRequestV1
    trace_id: UUID
    correlation_id: UUID
    causation_id: UUID

    @field_validator(
        "command_id", "workspace_id", "environment_id", "trace_id",
        "correlation_id", "causation_id", "workflow_version_id",
    )
    @classmethod
    def reject_zero_identity(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("workflow command identities must be non-zero")
        return value

    @field_validator("idempotency_key", "workflow_key", "workflow_version", "work_item_key")
    @classmethod
    def reject_blank_keys(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("workflow command keys must not be blank")
        return value


class AddContextCommandV1(SpineModel):
    """Protected conversational context added to one existing run."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    message: str = Field(min_length=1, max_length=32_768)

    @field_validator("message")
    @classmethod
    def reject_blank_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("workflow context message must not be blank")
        return value


class WorkflowArtifactRef(SpineModel):
    """An exact immutable artifact version consumed by a command."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact_id: UUID
    schema_name: str = Field(min_length=1, max_length=255)
    schema_version: str = Field(min_length=1, max_length=128)
    artifact_version: int = Field(ge=1, strict=True)

    @field_validator("artifact_id")
    @classmethod
    def reject_zero_identity(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("artifact identity must be non-zero")
        return value


class WorkflowCommand(SpineModel):
    """Versioned trusted command envelope for an existing Workflow Run."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    command_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    run_id: UUID
    workspace_id: UUID
    environment_id: UUID
    actor: ActorRef
    command_type: Literal["add_context"]
    command_schema_version: Literal[1]
    expected_run_version: int = Field(ge=1, strict=True)
    payload: AddContextCommandV1
    artifact_refs: tuple[WorkflowArtifactRef, ...] = ()
    trace_id: UUID
    correlation_id: UUID
    causation_id: UUID

    @field_validator(
        "command_id", "run_id", "workspace_id", "environment_id", "trace_id",
        "correlation_id", "causation_id",
    )
    @classmethod
    def reject_zero_identity(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("workflow command identities must be non-zero")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def reject_blank_idempotency_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("workflow command idempotency key must not be blank")
        return value

class PinnedWorkflowVersion(SpineModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workflow_key: str = Field(min_length=1, max_length=128)
    workflow_version_id: UUID
    version: str = Field(min_length=1, max_length=128)


class WorkflowVersionResolver(Protocol):
    async def resolve(
        self, *, workspace_id: UUID, environment_id: UUID, workflow_key: str
    ) -> PinnedWorkflowVersion: ...


class WorkflowVersionUnavailableError(LookupError):
    """The server has no published workflow version for the requested key."""


class ConfiguredWorkflowVersionResolver:
    """Resolve immutable versions from the deployment's published registry."""

    def __init__(self, versions: tuple[PinnedWorkflowVersion, ...]) -> None:
        by_key = {version.workflow_key: version for version in versions}
        if len(by_key) != len(versions):
            raise ValueError("workflow version registry contains duplicate keys")
        self._versions = by_key

    async def resolve(
        self, *, workspace_id: UUID, environment_id: UUID, workflow_key: str
    ) -> PinnedWorkflowVersion:
        del workspace_id, environment_id
        try:
            return self._versions[workflow_key]
        except KeyError:
            raise WorkflowVersionUnavailableError(workflow_key) from None


class WorkflowRunAdmissionPort(Protocol):
    async def admit(self, command: StartWorkflowCommand) -> WorkflowCommandReceipt: ...

    async def get_run(
        self, *, workspace_id: UUID, environment_id: UUID, run_id: UUID
    ) -> WorkflowRun | None: ...

    async def dispatch(self, command: WorkflowCommand) -> WorkflowCommandReceipt: ...

    async def list_events(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        run_id: UUID,
        after_sequence: int | None,
        limit: int,
    ) -> WorkflowEventPage: ...


class WorkflowRunAdmissionService:
    """Use case coordinating admission and scope-bound run readback."""

    def __init__(self, port: WorkflowRunAdmissionPort) -> None:
        self._port = port

    async def start(self, command: StartWorkflowCommand) -> WorkflowCommandReceipt:
        return await self._port.admit(command)

    async def dispatch(self, command: WorkflowCommand) -> WorkflowCommandReceipt:
        return await self._port.dispatch(command)

    async def get_run(
        self, *, workspace_id: UUID, environment_id: UUID, run_id: UUID
    ) -> WorkflowRun | None:
        return await self._port.get_run(
            workspace_id=workspace_id,
            environment_id=environment_id,
            run_id=run_id,
        )

    async def list_events(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        run_id: UUID,
        after_sequence: int | None,
        limit: int,
    ) -> WorkflowEventPage:
        return await self._port.list_events(
            workspace_id=workspace_id,
            environment_id=environment_id,
            run_id=run_id,
            after_sequence=after_sequence,
            limit=limit,
        )


def workflow_run_snapshot(run: WorkflowRun) -> WorkflowRunSnapshot:
    """Project protected canonical state into the shared safe read model."""

    allowed_commands: tuple[Literal["add_context"], ...] = (
        ("add_context",)
        if run.state.value not in {"completed", "failed", "cancelled"}
        else ()
    )
    return WorkflowRunSnapshot(
        run_id=run.run_id,
        work_item_id=run.work_item_id,
        workspace_id=run.workspace_id,
        environment_id=run.environment_id,
        workflow_key=run.workflow_key,
        workflow_version_id=run.workflow_version_id,
        workflow_version=run.workflow_version,
        state=run.state,
        run_version=run.run_version,
        allowed_commands=allowed_commands,
        created_at=run.created_at,
    )


__all__ = [
    "AddContextCommandV1",
    "StartWorkflowCommand",
    "WorkflowArtifactRef",
    "WorkflowCommand",
    "WorkflowRunAdmissionPort",
    "WorkflowRunAdmissionService",
    "PinnedWorkflowVersion",
    "WorkflowVersionResolver",
    "WorkflowVersionUnavailableError",
    "ConfiguredWorkflowVersionResolver",
    "workflow_run_snapshot",
]
