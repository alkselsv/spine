"""Trusted composition boundary for one pinned ``answer_question`` execution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol
from uuid import UUID

from pydantic import ConfigDict, field_validator

from spine.agents.catalog import (
    AgentBindingResolver,
    BindingResolutionError,
    BindingResolutionRequest,
    ResolvedAgentBinding,
)
from spine.agents.runtime import AgentHandler, AgentRuntime
from spine.agents.runtime.artifacts import PersistedAgentInvocationResult
from spine.domain.agents import AgentInvocation, AgentResult, AgentVersion
from spine.domain.capabilities.answer_question_runs import AgentResultRef
from spine.domain.capabilities import CapabilityDefinition
from spine.domain.capabilities.answer_question_execution import (
    AnswerQuestionExecutionPin,
    PinnedAgentVersion,
    PinnedConfigurationVersion,
    PinnedContextProfile,
    PinnedProjection,
    TrustedAnswerQuestionConfiguration,
    agent_version_digest,
)
from spine.domain.common import EnvironmentKind, ValidatedDefinitionModel


class AnswerQuestionExecutionErrorCode(str, Enum):
    INVALID_REQUEST = "invalid_request"
    BINDING_UNAVAILABLE = "binding_unavailable"
    DEPLOYMENT_UNAPPROVED = "deployment_unapproved"
    HANDLER_UNAVAILABLE = "handler_unavailable"
    SCHEMA_INCOMPATIBLE = "schema_incompatible"
    CONFIGURATION_UNAVAILABLE = "configuration_unavailable"
    CONFIGURATION_INVALID = "configuration_invalid"
    CONFIGURATION_UNAPPROVED = "configuration_unapproved"
    SCOPE_MISMATCH = "scope_mismatch"
    GENERATION_MISMATCH = "generation_mismatch"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"


class AnswerQuestionExecutionError(ValueError):
    """Disclosure-safe typed failure at the Q&A execution boundary."""

    def __init__(self, code: AnswerQuestionExecutionErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class AnswerQuestionExecutionIntent(ValidatedDefinitionModel):
    """The bounded caller intent allowed beside the question input artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    allow_partial_answer: bool | None = None

    def runtime_constraints(self) -> dict[str, bool]:
        if self.allow_partial_answer is not True:
            return {}
        return {"allow_partial_answer": True}


class AnswerQuestionExecutionRequest(ValidatedDefinitionModel):
    """Closed server-facing request; authoritative version selectors are absent."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    workspace_id: UUID
    environment_id: UUID
    environment: EnvironmentKind
    intent: AnswerQuestionExecutionIntent = AnswerQuestionExecutionIntent()

    @field_validator("workspace_id", "environment_id")
    @classmethod
    def validate_id(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("Q&A execution scope is invalid")
        return value


class AnswerQuestionConfigurationCatalog(Protocol):
    def resolve_for_agent_version(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        environment: EnvironmentKind,
        capability_key: str,
        agent_version_id: UUID,
    ) -> TrustedAnswerQuestionConfiguration | None: ...

    def verify_configuration(
        self,
        configuration: TrustedAnswerQuestionConfiguration,
    ) -> bool: ...


class AnswerQuestionRuntimeProvider(Protocol):
    """Composition seam returning the existing admitted local AgentRuntime."""

    def runtime_for(
        self,
        *,
        resolved_binding: ResolvedAgentBinding,
        agent_version: AgentVersion,
        handler: AgentHandler[Any, Any],
        implementation_key: str,
        pin: AnswerQuestionExecutionPin,
    ) -> AgentRuntime: ...


@dataclass(frozen=True, slots=True)
class AnswerQuestionExecutionResult:
    pin: AnswerQuestionExecutionPin
    result: AgentResult
    agent_result_ref: AgentResultRef | None = None


class AnswerQuestionExecutionBoundary:
    """Resolve trusted identities, retain their pin, and call the existing runtime."""

    def __init__(
        self,
        *,
        capability: CapabilityDefinition,
        binding_resolver: AgentBindingResolver,
        configuration_catalog: AnswerQuestionConfigurationCatalog,
        runtime_provider: AnswerQuestionRuntimeProvider,
    ) -> None:
        if capability.key != "answer_question":
            raise ValueError("answer_question capability is required")
        self._capability = capability
        self._binding_resolver = binding_resolver
        self._configuration_catalog = configuration_catalog
        self._runtime_provider = runtime_provider

    def resolve(
        self, request: AnswerQuestionExecutionRequest
    ) -> tuple[ResolvedAgentBinding, AnswerQuestionExecutionPin]:
        if type(request) is not AnswerQuestionExecutionRequest:
            raise AnswerQuestionExecutionError(
                AnswerQuestionExecutionErrorCode.INVALID_REQUEST
            )
        try:
            resolved = self._binding_resolver.resolve(
                BindingResolutionRequest(
                    workspace_id=request.workspace_id,
                    environment=request.environment,
                    capability=self._capability,
                )
            )
        except BindingResolutionError as exc:
            raise AnswerQuestionExecutionError(_binding_error_code(str(exc))) from None

        try:
            selected = self._configuration_catalog.resolve_for_agent_version(
                workspace_id=request.workspace_id,
                environment_id=request.environment_id,
                environment=request.environment,
                capability_key=self._capability.key,
                agent_version_id=resolved.agent_version.id,
            )
        except Exception:
            raise AnswerQuestionExecutionError(
                AnswerQuestionExecutionErrorCode.CONFIGURATION_UNAVAILABLE
            ) from None
        if type(selected) is not TrustedAnswerQuestionConfiguration:
            raise AnswerQuestionExecutionError(
                AnswerQuestionExecutionErrorCode.CONFIGURATION_INVALID
            )
        try:
            selected = TrustedAnswerQuestionConfiguration.model_validate(
                selected.model_dump(mode="python"), strict=True
            )
        except Exception:
            raise AnswerQuestionExecutionError(
                AnswerQuestionExecutionErrorCode.CONFIGURATION_INVALID
            ) from None
        try:
            trusted = self._configuration_catalog.verify_configuration(selected)
        except Exception:
            trusted = False
        if trusted is not True:
            raise AnswerQuestionExecutionError(
                AnswerQuestionExecutionErrorCode.CONFIGURATION_INVALID
            )
        self._validate_configuration(request, resolved, selected)
        try:
            pin = AnswerQuestionExecutionPin.create(
                workspace_id=request.workspace_id,
                environment_id=request.environment_id,
                environment=request.environment,
                configuration=selected,
            )
        except Exception:
            raise AnswerQuestionExecutionError(
                AnswerQuestionExecutionErrorCode.CONFIGURATION_INVALID
            ) from None
        return resolved, pin

    async def execute(
        self,
        request: AnswerQuestionExecutionRequest,
        invocation: AgentInvocation,
    ) -> AnswerQuestionExecutionResult:
        if type(invocation) is not AgentInvocation:
            raise AnswerQuestionExecutionError(
                AnswerQuestionExecutionErrorCode.INVALID_REQUEST
            )
        if (
            invocation.capability != self._capability.key
            or invocation.context_request
            or invocation.execution_configuration_digest is not None
            or invocation.constraints != request.intent.runtime_constraints()
        ):
            raise AnswerQuestionExecutionError(
                AnswerQuestionExecutionErrorCode.INVALID_REQUEST
            )
        resolved, pin = self.resolve(request)
        runtime_invocation = invocation.model_copy(
            update={"execution_configuration_digest": pin.pin_digest}
        )
        try:
            runtime = self._runtime_provider.runtime_for(
                resolved_binding=resolved,
                agent_version=resolved.agent_version,
                handler=resolved.handler,
                implementation_key=resolved.implementation_key,
                pin=pin,
            )
            if not callable(getattr(runtime, "invoke", None)) or not callable(
                getattr(runtime, "cancel", None)
            ):
                raise TypeError
            persisted_invoke = getattr(runtime, "invoke_with_reference", None)
            persisted = (
                await persisted_invoke(resolved.agent_version, runtime_invocation)
                if callable(persisted_invoke)
                else None
            )
            if persisted is not None:
                if type(persisted) is not PersistedAgentInvocationResult:
                    raise TypeError
                result = persisted.result
                agent_result_ref = AgentResultRef(
                    result_id=persisted.reference.result_id,
                    result_type=persisted.reference.result_type,
                    schema_version=persisted.reference.schema_version,
                )
            else:
                result = await runtime.invoke(resolved.agent_version, runtime_invocation)
                agent_result_ref = None
        except AnswerQuestionExecutionError:
            raise
        except Exception:
            raise AnswerQuestionExecutionError(
                AnswerQuestionExecutionErrorCode.RUNTIME_UNAVAILABLE
            ) from None
        if type(result) is not AgentResult:
            raise AnswerQuestionExecutionError(
                AnswerQuestionExecutionErrorCode.RUNTIME_UNAVAILABLE
            )
        return AnswerQuestionExecutionResult(
            pin=pin, result=result, agent_result_ref=agent_result_ref
        )

    def _validate_configuration(
        self,
        request: AnswerQuestionExecutionRequest,
        resolved: ResolvedAgentBinding,
        selected: TrustedAnswerQuestionConfiguration,
    ) -> None:
        if any(
            item.workspace_id != request.workspace_id
            or item.environment_id != request.environment_id
            for item in selected.identities
        ):
            raise AnswerQuestionExecutionError(
                AnswerQuestionExecutionErrorCode.SCOPE_MISMATCH
            )
        if any(
            item.configuration_generation != selected.configuration_generation
            for item in selected.identities
        ):
            raise AnswerQuestionExecutionError(
                AnswerQuestionExecutionErrorCode.GENERATION_MISMATCH
            )
        if not selected.approved:
            raise AnswerQuestionExecutionError(
                AnswerQuestionExecutionErrorCode.CONFIGURATION_UNAPPROVED
            )
        schema_identity = resolved.schema_identity
        if (
            selected.capability_id != schema_identity.capability_id
            or selected.input_schema != schema_identity.input_schema
            or selected.output_schema != schema_identity.output_schema
        ):
            raise AnswerQuestionExecutionError(
                AnswerQuestionExecutionErrorCode.SCHEMA_INCOMPATIBLE
            )
        version = resolved.agent_version
        agent_identity = selected.agent_version
        if (
            agent_identity.agent_version_id != version.id
            or agent_identity.agent_id != version.agent_id
            or agent_identity.version != version.version
            or selected.prompt_version.binding_key != version.prompt_version
            or selected.model_profile.binding_key != version.model_profile
            or selected.context_profile.binding_key != version.context_profile
        ):
            raise AnswerQuestionExecutionError(
                AnswerQuestionExecutionErrorCode.GENERATION_MISMATCH
            )
        try:
            expected_agent_digest = agent_version_digest(version)
        except Exception:
            raise AnswerQuestionExecutionError(
                AnswerQuestionExecutionErrorCode.CONFIGURATION_INVALID
            ) from None
        if agent_identity.digest != expected_agent_digest:
            raise AnswerQuestionExecutionError(
                AnswerQuestionExecutionErrorCode.CONFIGURATION_INVALID
            )


def _binding_error_code(message: str) -> AnswerQuestionExecutionErrorCode:
    if "deployment is not approved" in message:
        return AnswerQuestionExecutionErrorCode.DEPLOYMENT_UNAPPROVED
    if "implementation is unavailable" in message:
        return AnswerQuestionExecutionErrorCode.HANDLER_UNAVAILABLE
    if "schema is incompatible" in message:
        return AnswerQuestionExecutionErrorCode.SCHEMA_INCOMPATIBLE
    return AnswerQuestionExecutionErrorCode.BINDING_UNAVAILABLE


__all__ = [
    "AnswerQuestionConfigurationCatalog",
    "AnswerQuestionExecutionBoundary",
    "AnswerQuestionExecutionError",
    "AnswerQuestionExecutionErrorCode",
    "AnswerQuestionExecutionIntent",
    "AnswerQuestionExecutionPin",
    "AnswerQuestionExecutionRequest",
    "AnswerQuestionExecutionResult",
    "AnswerQuestionRuntimeProvider",
    "PinnedAgentVersion",
    "PinnedConfigurationVersion",
    "PinnedContextProfile",
    "PinnedProjection",
    "TrustedAnswerQuestionConfiguration",
]
