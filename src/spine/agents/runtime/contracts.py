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

from pydantic import ConfigDict, Field, PrivateAttr, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from spine.application.diagnostics import DiagnosticContext, StructuredError
from spine.domain.agents import AgentInvocation, AgentResult, AgentRunStatus, AgentVersion
from spine.domain.capabilities import CapabilityDefinition
from spine.domain.common import ActorRef, EnvironmentKind, FrozenDict, SchemaRef, SpineModel

from spine.agents.runtime.ports import (
    AuthorizationContextView,
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


class CapabilitySchemaIdentity(_ValidatedRuntimeModel):
    """Immutable identity of a capability and its declared schema versions.

    This is an identity reference, not JSON Schema validation. Registered
    compatibility and artifact validation remain owned by later runtime seams.
    """

    capability_id: UUID
    capability_key: str = Field(min_length=1, max_length=256)
    input_schema: SchemaRef
    output_schema: SchemaRef

    @classmethod
    def from_capability(cls, capability: CapabilityDefinition) -> CapabilitySchemaIdentity:
        return cls(
            capability_id=capability.id,
            capability_key=capability.key,
            input_schema=capability.input_schema,
            output_schema=capability.output_schema,
        )

    @field_validator("capability_id")
    @classmethod
    def reject_zero_capability_id(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("capability identity must be non-zero")
        return value

    @model_validator(mode="after")
    def validate_schema_identity(self) -> CapabilitySchemaIdentity:
        _validate_schema_ref(self.input_schema, "input")
        _validate_schema_ref(self.output_schema, "output")
        if not self.capability_key.strip():
            raise ValueError("capability identity is invalid")
        return self


class AgentRequest(_ValidatedRuntimeModel, Generic[InputT]):
    """A typed handler input for one already-resolved Capability contract."""

    capability: CapabilityDefinition
    schema_identity: CapabilitySchemaIdentity
    input: InputT
    constraints: Mapping[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def default_schema_identity(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and "schema_identity" not in value:
            capability = value.get("capability")
            if isinstance(capability, CapabilityDefinition):
                values = dict(value)
                values["schema_identity"] = CapabilitySchemaIdentity.from_capability(capability)
                return values
        return value

    @field_validator("capability")
    @classmethod
    def validate_capability_schema(cls, value: CapabilityDefinition) -> CapabilityDefinition:
        _validate_capability_schema(value)
        return value

    @field_validator("constraints", mode="after")
    @classmethod
    def freeze_constraints(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return FrozenDict(value)

    @model_validator(mode="after")
    def validate_schema_identity(self) -> AgentRequest[InputT]:
        _validate_capability_schema(self.capability)
        _require_matching_schema_identity(self.capability, self.schema_identity)
        return self


class AgentResponse(_ValidatedRuntimeModel, Generic[OutputT]):
    """Typed handler output with the shared safe error contract."""

    schema_identity: CapabilitySchemaIdentity
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


class ExecutionIdentitySnapshot(_ValidatedRuntimeModel):
    """Detached identity data copied from an authorization-boundary view.

    This model is data, not proof of authorization. Direct construction is
    intentionally possible for deterministic contract tests, but an actual
    runtime must obtain and verify the source view through Issue #69/#108.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="never",
    )

    workspace_id: UUID
    environment_id: UUID
    acting_subject_id: UUID | None = None
    service_principal_id: UUID | None = None
    authorization_generation: int
    _authorization_port_issued: bool = PrivateAttr(default=False)

    @classmethod
    def from_authorization_context(
        cls,
        value: AuthorizationContextView,
    ) -> ExecutionIdentitySnapshot:
        snapshot = cls(
            workspace_id=value.workspace_id,
            environment_id=value.environment_id,
            acting_subject_id=value.acting_subject_id,
            service_principal_id=value.service_principal_id,
            authorization_generation=value.authorization_generation,
        )
        object.__setattr__(snapshot, "_authorization_port_issued", True)
        return snapshot

    @field_validator("workspace_id", "environment_id", "acting_subject_id", "service_principal_id")
    @classmethod
    def reject_zero_identity(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("trusted identity identifiers must be non-zero")
        return value

    @model_validator(mode="after")
    def require_authority_subject(self) -> ExecutionIdentitySnapshot:
        if self.acting_subject_id is None and self.service_principal_id is None:
            raise ValueError("trusted identity must contain an acting subject or service principal")
        if self.authorization_generation < 0:
            raise ValueError("authorization generation must be non-negative")
        return self


# Compatibility name for the first local contract draft. The value remains a
# detached data snapshot and never constitutes an authorization issuer.
TrustedExecutionIdentitySnapshot = ExecutionIdentitySnapshot


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
    trusted_identity: ExecutionIdentitySnapshot
    work_item_id: UUID
    workflow_run_id: UUID
    step_run_id: UUID
    agent_run_id: UUID
    attempt_id: UUID | None = None
    capability: CapabilityDefinition
    schema_identity: CapabilitySchemaIdentity
    agent_version: AgentVersion
    idempotency_key: str = Field(min_length=1, max_length=256)
    diagnostic_context: DiagnosticContext
    input_artifact_ids: tuple[UUID, ...] = ()
    deadline: datetime | None = None
    context_profile: str | None = Field(default=None, min_length=1, max_length=128)
    permitted_tool_capabilities: tuple[str, ...] = ()
    cancellation: SkipJsonSchema[CancellationToken] = Field(exclude=True, repr=False)
    event_sink: SkipJsonSchema[EventSink] = Field(exclude=True, repr=False)

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
    ) -> ExecutionIdentitySnapshot:
        if isinstance(value, ExecutionIdentitySnapshot):
            if not value._authorization_port_issued:
                raise ValueError("identity snapshots are data, not authorization provenance")
            return value
        if not isinstance(value, AuthorizationContextView):
            raise ValueError("trusted_identity must be supplied by the authorization context port")
        return ExecutionIdentitySnapshot.from_authorization_context(value)

    @field_validator("cancellation")
    @classmethod
    def validate_cancellation_port(cls, value: CancellationToken) -> CancellationToken:
        if not isinstance(value, CancellationToken):
            raise ValueError("cancellation must satisfy the cancellation port")
        return value

    @field_validator("event_sink")
    @classmethod
    def validate_event_sink_port(cls, value: EventSink) -> EventSink:
        if not isinstance(value, EventSink):
            raise ValueError("event_sink must satisfy the EventSink port")
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
        _require_matching_schema_identity(self.capability, self.schema_identity)
        if (
            self.agent_version.id.int == 0
            or self.agent_version.agent_id.int == 0
            or not self.agent_version.version.strip()
        ):
            raise ValueError("agent version identity is invalid")
        return self


def typed_request_from_invocation(
    invocation: AgentInvocation,
    *,
    capability: CapabilityDefinition,
    input: InputT,
) -> AgentRequest[InputT]:
    """Project only typed payload data from a legacy invocation.

    Work-item and step identity, acting identity, artifact lineage, context
    requests and idempotency remain in ``invocation_context_from_invocation``;
    this function is deliberately not a complete invocation conversion.
    """

    if invocation.capability != capability.key:
        raise ValueError("invocation capability does not match capability definition")
    return AgentRequest(
        capability=capability,
        schema_identity=CapabilitySchemaIdentity.from_capability(capability),
        input=input,
        constraints=invocation.constraints,
    )


class AgentInvocationContext(_ValidatedRuntimeModel):
    """The non-payload portion preserved when adapting a legacy invocation."""

    work_item_id: UUID
    step_run_id: UUID
    acting_on_behalf_of: ActorRef
    input_artifact_ids: tuple[UUID, ...] = ()
    context_request: Mapping[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1)

    @field_validator("work_item_id", "step_run_id")
    @classmethod
    def reject_zero_invocation_ids(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("invocation identifiers must be non-zero")
        return value

    @field_validator("input_artifact_ids")
    @classmethod
    def reject_zero_artifact_ids(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        _validate_artifact_ids(value)
        return value

    @field_validator("context_request", mode="after")
    @classmethod
    def freeze_context_request(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return FrozenDict(value)


def invocation_context_from_invocation(invocation: AgentInvocation) -> AgentInvocationContext:
    """Preserve legacy invocation identity and lineage as a typed context."""

    return AgentInvocationContext(
        work_item_id=invocation.work_item_id,
        step_run_id=invocation.step_run_id,
        acting_on_behalf_of=invocation.acting_on_behalf_of,
        input_artifact_ids=invocation.input_artifact_ids,
        context_request=invocation.context_request,
        idempotency_key=invocation.idempotency_key,
    )


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
    _validate_artifact_ids(output_artifact_ids)
    if result_status is AgentRunStatus.SUCCEEDED and response.error is not None:
        raise ValueError("successful result cannot contain a structured error")
    if result_status is AgentRunStatus.SUCCEEDED and not output_artifact_ids:
        raise ValueError("successful response requires persisted output artifact identifiers")
    if response.error is not None and output_artifact_ids:
        raise ValueError("error result cannot contain output artifacts")
    if result_status in {AgentRunStatus.FAILED, AgentRunStatus.CANCELLED}:
        if response.error is None:
            raise ValueError("failed or cancelled result requires a structured error")
        if response.output is not None:
            raise ValueError("failed or cancelled result cannot contain output")
    if result_status is AgentRunStatus.NEEDS_REVIEW and response.error is None and response.output is None:
        raise ValueError("review result requires output or a structured error")
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
        _validate_schema_ref(schema, label)


def _validate_schema_ref(schema: SchemaRef, label: str) -> None:
    if not schema.name.strip() or not schema.version.strip():
        raise ValueError(f"capability {label} schema identity is invalid")


def _require_matching_schema_identity(
    capability: CapabilityDefinition,
    identity: CapabilitySchemaIdentity,
) -> None:
    expected = CapabilitySchemaIdentity.from_capability(capability)
    if identity != expected:
        raise ValueError("schema identity does not match capability declaration")


def _validate_artifact_ids(artifact_ids: tuple[UUID, ...]) -> None:
    if any(identifier.int == 0 for identifier in artifact_ids):
        raise ValueError("artifact identifiers must be non-zero")
