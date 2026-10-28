"""Application boundary for starting and reading canonical workflow runs."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator

from spine.domain.common import ActorRef, EnvironmentKind, SpineModel
from spine.domain.workflows import (
    ClientRequestV1,
    WorkflowCommandReceipt,
    WorkflowRun,
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


class WorkflowRunAdmissionService:
    """Use case coordinating admission and scope-bound run readback."""

    def __init__(self, port: WorkflowRunAdmissionPort) -> None:
        self._port = port

    async def start(self, command: StartWorkflowCommand) -> WorkflowCommandReceipt:
        return await self._port.admit(command)

    async def get_run(
        self, *, workspace_id: UUID, environment_id: UUID, run_id: UUID
    ) -> WorkflowRun | None:
        return await self._port.get_run(
            workspace_id=workspace_id,
            environment_id=environment_id,
            run_id=run_id,
        )


__all__ = [
    "StartWorkflowCommand",
    "WorkflowRunAdmissionPort",
    "WorkflowRunAdmissionService",
    "PinnedWorkflowVersion",
    "WorkflowVersionResolver",
    "WorkflowVersionUnavailableError",
    "ConfiguredWorkflowVersionResolver",
]
