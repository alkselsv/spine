"""Typed contracts for one framework-independent local agent invocation.

This module defines data and ports only. Resolution, handler construction,
artifact I/O, cancellation execution, retries, and durable recovery belong to
later runtime tickets and their owning boundaries.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from typing import Any, Generic, TypeVar
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from spine.application.diagnostics import DiagnosticContext, StructuredError
from spine.domain.agents import AgentInvocation, AgentResult, AgentRunStatus, AgentVersion
from spine.domain.capabilities import CapabilityDefinition
from spine.domain.common import EnvironmentKind, FrozenDict, SpineModel

from spine.agents.runtime.ports import (
    CancellationToken,
    EventSink,
    TrustedExecutionIdentity,
)


InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")


class _ValidatedRuntimeModel(SpineModel):
    """Closed runtime model that keeps public copy paths validated."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        validate_default=True,
    )

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> _ValidatedRuntimeModel:
        values = deepcopy(self.__dict__) if deep else dict(self.__dict__)
        if update:
            values.update(update)
        return type(self).model_validate(values)

    def copy(
        self,
        *,
        include: Any = None,
        exclude: Any = None,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> _ValidatedRuntimeModel:
        """Keep Pydantic's deprecated copy path on the validated path."""

        values = deepcopy(self.__dict__) if deep else dict(self.__dict__)
        if include is not None:
            values = {key: value for key, value in values.items() if key in include}
        if exclude is not None:
            values = {key: value for key, value in values.items() if key not in exclude}
        if update:
            values.update(update)
        return type(self).model_validate(values)

    @classmethod
    def model_construct(cls, _fields_set: set[str] | None = None, **values: Any) -> Any:
        """Prevent explicitly unvalidated public construction."""

        raise TypeError(f"{cls.__name__}.model_construct is not supported")


class AgentRequest(_ValidatedRuntimeModel, Generic[InputT]):
    """A typed handler input for one already-resolved Capability contract."""

    capability: CapabilityDefinition
    input: InputT
    constraints: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("capability")
    @classmethod
    def validate_capability_schema(cls, value: CapabilityDefinition) -> CapabilityDefinition:
        _validate_capability_schema(value)
        return value

    @field_validator("constraints", mode="after")
    @classmethod
    def freeze_constraints(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return FrozenDict(value)


class AgentResponse(_ValidatedRuntimeModel, Generic[OutputT]):
    """Typed handler output with the shared safe error contract."""

    output: OutputT | None = None
    evidence_refs: tuple[str, ...] = ()
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    structured_metrics: Mapping[str, float] = Field(default_factory=dict)
    proposed_action_ids: tuple[UUID, ...] = ()
    error: StructuredError | None = None

    @model_validator(mode="after")
    def require_one_outcome(self) -> AgentResponse[OutputT]:
        if (self.output is None) == (self.error is None):
            raise ValueError("agent response must contain exactly one of output or error")
        return self

    @field_validator("proposed_action_ids")
    @classmethod
    def reject_zero_action_ids(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if any(identifier.int == 0 for identifier in value):
            raise ValueError("proposed action identifiers must be non-zero")
        return value

    @field_validator("structured_metrics", mode="after")
    @classmethod
    def freeze_metrics(cls, value: Mapping[str, float]) -> Mapping[str, float]:
        return FrozenDict(value)


class TrustedExecutionIdentitySnapshot(_ValidatedRuntimeModel):
    """Detached immutable runtime view of the trusted authorization identity."""

    workspace_id: UUID
    environment_id: UUID
    acting_subject_id: UUID | None = None
    service_principal_id: UUID | None = None
    authorization_generation: int

    @field_validator("workspace_id", "environment_id", "acting_subject_id", "service_principal_id")
    @classmethod
    def reject_zero_identity(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("trusted identity identifiers must be non-zero")
        return value

    @model_validator(mode="after")
    def require_authority_subject(self) -> TrustedExecutionIdentitySnapshot:
        if self.acting_subject_id is None and self.service_principal_id is None:
            raise ValueError("trusted identity must contain an acting subject or service principal")
        if self.authorization_generation < 0:
            raise ValueError("authorization generation must be non-negative")
        return self


class AgentExecutionContext(_ValidatedRuntimeModel):
    """Immutable, detached execution metadata supplied to a local handler.

    ``trusted_identity`` is a structural view issued by the authorization
    boundary. This model does not mint or authenticate it. Event and
    cancellation ports are dependencies for the current call, not mutable
    workflow state or bearer authority.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        arbitrary_types_allowed=True,
    )

    workspace_id: UUID
    environment_id: UUID
    environment: EnvironmentKind
    trusted_identity: TrustedExecutionIdentitySnapshot
    work_item_id: UUID
    workflow_run_id: UUID
    step_run_id: UUID
    agent_run_id: UUID
    attempt_id: UUID | None = None
    capability: CapabilityDefinition
    agent_version: AgentVersion
    idempotency_key: str = Field(min_length=1, max_length=256)
    diagnostic_context: DiagnosticContext
    input_artifact_ids: tuple[UUID, ...] = ()
    deadline: datetime | None = None
    context_profile: str | None = Field(default=None, min_length=1, max_length=128)
    permitted_tool_capabilities: tuple[str, ...] = ()
    cancellation: CancellationToken
    event_sink: EventSink

    @field_validator(
        "workspace_id",
        "environment_id",
        "work_item_id",
        "workflow_run_id",
        "step_run_id",
        "agent_run_id",
        "attempt_id",
        "input_artifact_ids",
    )
    @classmethod
    def reject_zero_identifiers(cls, value: UUID | tuple[UUID, ...] | None) -> UUID | tuple[UUID, ...] | None:
        if isinstance(value, tuple):
            if any(identifier.int == 0 for identifier in value):
                raise ValueError("execution identifiers must be non-zero")
        elif value is not None and value.int == 0:
            raise ValueError("execution identifiers must be non-zero")
        return value

    @field_validator("trusted_identity", mode="before")
    @classmethod
    def snapshot_trusted_identity(
        cls,
        value: TrustedExecutionIdentity | TrustedExecutionIdentitySnapshot,
    ) -> TrustedExecutionIdentitySnapshot:
        if not isinstance(value, TrustedExecutionIdentity):
            raise TypeError("trusted_identity must satisfy the trusted execution contract")
        return TrustedExecutionIdentitySnapshot(
            workspace_id=value.workspace_id,
            environment_id=value.environment_id,
            acting_subject_id=value.acting_subject_id,
            service_principal_id=value.service_principal_id,
            authorization_generation=value.authorization_generation,
        )

    @field_validator("cancellation")
    @classmethod
    def validate_cancellation_port(cls, value: CancellationToken) -> CancellationToken:
        if not isinstance(value, CancellationToken):
            raise TypeError("cancellation must satisfy the cancellation port")
        return value

    @field_validator("event_sink")
    @classmethod
    def validate_event_sink_port(cls, value: EventSink) -> EventSink:
        if not isinstance(value, EventSink):
            raise TypeError("event_sink must satisfy the EventSink port")
        return value

    @model_validator(mode="after")
    def validate_scope_and_version(self) -> AgentExecutionContext:
        if self.trusted_identity.workspace_id != self.workspace_id:
            raise ValueError("trusted identity Workspace does not match execution scope")
        if self.trusted_identity.environment_id != self.environment_id:
            raise ValueError("trusted identity Environment does not match execution scope")
        if self.capability.key not in self.agent_version.capabilities:
            raise ValueError("agent version does not support capability")
        _validate_capability_schema(self.capability)
        if (
            self.agent_version.id.int == 0
            or self.agent_version.agent_id.int == 0
            or not self.agent_version.version.strip()
        ):
            raise ValueError("agent version identity is invalid")
        return self


def agent_request_from_invocation(
    invocation: AgentInvocation,
    *,
    capability: CapabilityDefinition,
    input: InputT,
) -> AgentRequest[InputT]:
    """Map a legacy invocation envelope into the typed handler request seam."""

    if invocation.capability != capability.key:
        raise ValueError("invocation capability does not match capability definition")
    return AgentRequest(capability=capability, input=input, constraints=invocation.constraints)


def agent_result_from_response(
    response: AgentResponse[OutputT],
    *,
    output_artifact_ids: tuple[UUID, ...] = (),
    trace_ref: str | None = None,
    status: AgentRunStatus | None = None,
) -> AgentResult:
    """Map typed response metadata to the existing legacy result envelope.

    Artifact creation and persistence remain outside this pure compatibility
    mapping. A structured error is reduced to its already-safe public message
    for legacy consumers; its source contract remains ``StructuredError``.
    """

    result_status = status or (
        AgentRunStatus.FAILED if response.error is not None else AgentRunStatus.SUCCEEDED
    )
    if result_status is AgentRunStatus.SUCCEEDED and response.error is not None:
        raise ValueError("successful result cannot contain a structured error")
    if result_status is AgentRunStatus.SUCCEEDED and not output_artifact_ids:
        raise ValueError("successful response requires persisted output artifact identifiers")
    if response.error is not None:
        return AgentResult(
            status=result_status,
            evidence_refs=response.evidence_refs,
            confidence=response.confidence,
            structured_metrics=response.structured_metrics,
            proposed_action_ids=response.proposed_action_ids,
            trace_ref=trace_ref,
            error=response.error.safe_message,
        )
    return AgentResult(
        status=result_status,
        output_artifact_ids=output_artifact_ids,
        evidence_refs=response.evidence_refs,
        confidence=response.confidence,
        structured_metrics=response.structured_metrics,
        proposed_action_ids=response.proposed_action_ids,
        trace_ref=trace_ref,
    )


def _validate_capability_schema(capability: CapabilityDefinition) -> None:
    if capability.id.int == 0 or not capability.key.strip():
        raise ValueError("capability identity is invalid")
    for label, schema in (
        ("input", capability.input_schema),
        ("output", capability.output_schema),
    ):
        if not schema.name.strip() or not schema.version.strip():
            raise ValueError(f"capability {label} schema identity is invalid")
