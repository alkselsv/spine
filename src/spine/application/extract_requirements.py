"""Application boundary for the proposal ``extract_requirements`` capability."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from collections.abc import Mapping
from enum import Enum
from typing import Any, Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, field_validator, model_validator

from spine.agents.catalog import (
    AgentBindingResolver,
    BindingResolutionError,
    BindingResolutionRequest,
    ResolvedAgentBinding,
)
from spine.agents.runtime import (
    AgentExecutionContext,
    AgentHandler,
    AgentRequest,
    AgentRuntime,
    AgentResponse,
    CapabilityArtifactSchema,
    CapabilitySchemaIdentity,
)
from spine.agents.runtime.artifacts import PersistedAgentInvocationResult
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.idempotency import OpaqueResultReference
from spine.domain.agents import (
    AgentInvocation,
    AgentResult,
    AgentRetrySafety,
    AgentRunStatus,
    AgentRuntimeKind,
    AgentVersion,
)
from spine.domain.artifacts import Artifact
from spine.domain.capabilities import (
    ExtractRequirementsInput,
    RequirementsExecutionConfiguration,
    RequirementsExtractionOutcome,
    RequirementsExtractionOutput,
    CapabilityDefinition,
)
from spine.domain.common import ActorRef, EnvironmentKind, SchemaRef, ValidatedDefinitionModel
from spine.domain.workflows import RequirementState, RequirementsArtifact


EXTRACT_REQUIREMENTS_CAPABILITY_ID = uuid5(
    NAMESPACE_URL, "spine:capability:extract-requirements:v1"
)
EXTRACT_REQUIREMENTS_AGENT_ID = uuid5(
    NAMESPACE_URL, "spine:agent:requirements-extractor:v1"
)
EXTRACT_REQUIREMENTS_AGENT_VERSION_ID = uuid5(
    NAMESPACE_URL, "spine:agent-version:requirements-extractor:2026.10"
)
EXTRACT_REQUIREMENTS_IMPLEMENTATION_KEY = "requirements.extract.deterministic"


def extract_requirements_capability() -> CapabilityDefinition:
    """Return the stable capability declaration used by every composition root."""

    return CapabilityDefinition(
        id=EXTRACT_REQUIREMENTS_CAPABILITY_ID,
        key="extract_requirements",
        display_name="Extract proposal requirements",
        input_schema=SchemaRef(
            name="extract-requirements.input", version="v1"
        ),
        output_schema=SchemaRef(
            name="extract-requirements.output", version="v1"
        ),
        description="Produce a reviewable, evidence-aware requirements candidate.",
    )


def extract_requirements_agent_version() -> AgentVersion:
    """Return the immutable synthetic implementation identity for offline execution."""

    return AgentVersion(
        id=EXTRACT_REQUIREMENTS_AGENT_VERSION_ID,
        agent_id=EXTRACT_REQUIREMENTS_AGENT_ID,
        version="2026.10",
        runtime=AgentRuntimeKind.CODE,
        capabilities=("extract_requirements",),
        prompt_version="requirements.extract:none",
        model_profile="requirements.extract:deterministic",
        context_profile="requirements.extract:none",
    )


class RequirementsExecutionErrorCode(str, Enum):
    INVALID_REQUEST = "invalid_request"
    BINDING_UNAVAILABLE = "binding_unavailable"
    DEPLOYMENT_UNAPPROVED = "deployment_unapproved"
    HANDLER_UNAVAILABLE = "handler_unavailable"
    SCHEMA_INCOMPATIBLE = "schema_incompatible"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    INPUT_UNAVAILABLE = "input_unavailable"
    OUTPUT_INVALID = "output_invalid"
    PERSISTENCE_UNAVAILABLE = "persistence_unavailable"


class RequirementsExecutionError(ValueError):
    """Disclosure-safe typed failure at the capability/application boundary."""

    def __init__(self, code: RequirementsExecutionErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class RequirementsExecutionRequest(ValidatedDefinitionModel):
    """Server-created execution intent; no implementation selector is accepted."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workspace_id: UUID
    environment_id: UUID
    environment: EnvironmentKind
    workflow_run_id: UUID
    work_item_id: UUID
    step_run_id: UUID
    agent_run_id: UUID
    input_artifact_id: UUID
    acting_on_behalf_of: ActorRef
    idempotency_key: str = Field(min_length=1, max_length=255)
    execution: RequirementsExecutionConfiguration = RequirementsExecutionConfiguration()

    @field_validator(
        "workspace_id",
        "environment_id",
        "workflow_run_id",
        "work_item_id",
        "step_run_id",
        "agent_run_id",
        "input_artifact_id",
    )
    @classmethod
    def reject_zero_ids(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("requirements execution identities must be non-zero")
        return value


class RequirementsExecutionPin(ValidatedDefinitionModel):
    """The exact catalog and execution identity used by one run."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["requirements-execution-pin:v1"] = "requirements-execution-pin:v1"
    workspace_id: UUID
    environment_id: UUID
    environment: EnvironmentKind
    capability_id: UUID
    input_schema: SchemaRef
    output_schema: SchemaRef
    binding_id: UUID
    deployment_id: UUID
    agent_version_id: UUID
    agent_id: UUID
    agent_version: str
    agent_version_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    implementation_key: str = Field(min_length=1, max_length=128)
    execution_configuration_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    pin_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator(
        "workspace_id",
        "environment_id",
        "capability_id",
        "binding_id",
        "deployment_id",
        "agent_version_id",
        "agent_id",
    )
    @classmethod
    def reject_zero_ids(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("requirements execution pin identities must be non-zero")
        return value

    @model_validator(mode="after")
    def validate_digest(self) -> RequirementsExecutionPin:
        values = self.model_dump(mode="python", exclude={"pin_digest"})
        expected = _digest({key: _json_value(value) for key, value in values.items()})
        if expected != self.pin_digest:
            raise ValueError("requirements execution pin digest is invalid")
        return self

    @classmethod
    def create(
        cls,
        *,
        request: RequirementsExecutionRequest,
        resolved: ResolvedAgentBinding,
    ) -> RequirementsExecutionPin:
        version_digest = _agent_version_digest(resolved.agent_version)
        config_digest = _digest(request.execution.model_dump(mode="json"))
        values: dict[str, Any] = {
            "schema_version": "requirements-execution-pin:v1",
            "workspace_id": request.workspace_id,
            "environment_id": request.environment_id,
            "environment": request.environment,
            "capability_id": resolved.capability.id,
            "input_schema": resolved.capability.input_schema,
            "output_schema": resolved.capability.output_schema,
            "binding_id": resolved.binding.id,
            "deployment_id": resolved.deployment.id,
            "agent_version_id": resolved.agent_version.id,
            "agent_id": resolved.agent_version.agent_id,
            "agent_version": resolved.agent_version.version,
            "agent_version_digest": version_digest,
            "implementation_key": resolved.implementation_key,
            "execution_configuration_digest": config_digest,
        }
        values["pin_digest"] = _digest(
            {key: _json_value(value) for key, value in values.items()}
        )
        return cls.model_validate(values)


class RequirementsArtifactReader(Protocol):
    async def load(
        self,
        artifact_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        work_item_id: UUID,
    ) -> Artifact | None: ...


class RequirementsArtifactCandidatePort(Protocol):
    async def persist_extracted_candidate(
        self,
        *,
        context: TrustedPersistenceContext,
        run_id: UUID,
        extraction: RequirementsExtractionOutput,
        upstream_artifact_ids: tuple[UUID, ...],
    ) -> RequirementsArtifact | None: ...


class RequirementsRuntimeProvider(Protocol):
    """Composition port returning the already-admitted existing Agent Runtime."""

    def runtime_for(
        self,
        *,
        request: RequirementsExecutionRequest,
        resolved_binding: ResolvedAgentBinding,
        agent_version: AgentVersion,
        handler: AgentHandler[Any, Any],
        implementation_key: str,
        pin: RequirementsExecutionPin,
    ) -> AgentRuntime: ...


@dataclass(frozen=True, slots=True)
class RequirementsExecutionResult:
    pin: RequirementsExecutionPin
    invocation: AgentInvocation
    result: AgentResult
    agent_result_ref: OpaqueResultReference | None = None
    extraction: RequirementsExtractionOutput | None = None
    artifact: RequirementsArtifact | None = None


class RequirementsExtractionExecutionBoundary:
    """Resolve, invoke and persist one pinned requirements extraction."""

    def __init__(
        self,
        *,
        capability: CapabilityDefinition,
        binding_resolver: AgentBindingResolver,
        runtime_provider: RequirementsRuntimeProvider,
        artifacts: RequirementsArtifactReader,
        requirements_artifacts: RequirementsArtifactCandidatePort,
    ) -> None:
        if capability.key != "extract_requirements":
            raise ValueError("extract_requirements capability is required")
        self._capability = capability
        self._binding_resolver = binding_resolver
        self._runtime_provider = runtime_provider
        self._artifacts = artifacts
        self._requirements_artifacts = requirements_artifacts

    def resolve(
        self, request: RequirementsExecutionRequest
    ) -> tuple[ResolvedAgentBinding, RequirementsExecutionPin]:
        if type(request) is not RequirementsExecutionRequest:
            raise RequirementsExecutionError(RequirementsExecutionErrorCode.INVALID_REQUEST)
        try:
            resolved = self._binding_resolver.resolve(
                BindingResolutionRequest(
                    workspace_id=request.workspace_id,
                    environment=request.environment,
                    capability=self._capability,
                )
            )
        except BindingResolutionError as error:
            raise RequirementsExecutionError(_binding_error_code(str(error))) from None
        if resolved.workspace_id != request.workspace_id or resolved.environment is not request.environment:
            raise RequirementsExecutionError(RequirementsExecutionErrorCode.BINDING_UNAVAILABLE)
        try:
            pin = RequirementsExecutionPin.create(request=request, resolved=resolved)
        except Exception:
            raise RequirementsExecutionError(RequirementsExecutionErrorCode.SCHEMA_INCOMPATIBLE) from None
        return resolved, pin

    async def execute(
        self,
        *,
        request: RequirementsExecutionRequest,
        persistence_context: TrustedPersistenceContext,
    ) -> RequirementsExecutionResult:
        if type(request) is not RequirementsExecutionRequest:
            raise RequirementsExecutionError(RequirementsExecutionErrorCode.INVALID_REQUEST)
        resolved, pin = self.resolve(request)
        invocation = AgentInvocation(
            work_item_id=request.work_item_id,
            step_run_id=request.step_run_id,
            capability=self._capability.key,
            acting_on_behalf_of=request.acting_on_behalf_of,
            input_artifact_ids=(request.input_artifact_id,),
            constraints=request.execution.runtime_constraints(),
            execution_configuration_digest=pin.pin_digest,
            idempotency_key=request.idempotency_key,
            retry_safety=AgentRetrySafety.SAFE_TO_REPEAT,
        )
        try:
            runtime = self._runtime_provider.runtime_for(
                request=request,
                resolved_binding=resolved,
                agent_version=resolved.agent_version,
                handler=resolved.handler,
                implementation_key=resolved.implementation_key,
                pin=pin,
            )
            invoke_with_reference = getattr(runtime, "invoke_with_reference", None)
            if not callable(invoke_with_reference):
                raise TypeError
            persisted = await invoke_with_reference(resolved.agent_version, invocation)
            if type(persisted) is not PersistedAgentInvocationResult:
                raise TypeError
        except RequirementsExecutionError:
            raise
        except Exception:
            raise RequirementsExecutionError(RequirementsExecutionErrorCode.RUNTIME_UNAVAILABLE) from None

        result = persisted.result
        if type(result) is not AgentResult:
            raise RequirementsExecutionError(RequirementsExecutionErrorCode.RUNTIME_UNAVAILABLE)
        if result.status is not AgentRunStatus.SUCCEEDED:
            return RequirementsExecutionResult(
                pin=pin,
                invocation=invocation,
                result=result,
                agent_result_ref=persisted.reference,
            )
        if len(result.output_artifact_ids) != 1:
            raise RequirementsExecutionError(RequirementsExecutionErrorCode.OUTPUT_INVALID)

        try:
            input_artifact = await self._artifacts.load(
                request.input_artifact_id,
                workspace_id=request.workspace_id,
                environment_id=request.environment_id,
                work_item_id=request.work_item_id,
            )
            output_artifact = await self._artifacts.load(
                result.output_artifact_ids[0],
                workspace_id=request.workspace_id,
                environment_id=request.environment_id,
                work_item_id=request.work_item_id,
            )
            if input_artifact is None or output_artifact is None:
                raise ValueError
            if (
                input_artifact.workspace_id != request.workspace_id
                or input_artifact.environment_id != request.environment_id
                or input_artifact.work_item_id != request.work_item_id
                or input_artifact.artifact_schema != self._capability.input_schema
                or output_artifact.workspace_id != request.workspace_id
                or output_artifact.environment_id != request.environment_id
                or output_artifact.work_item_id != request.work_item_id
                or output_artifact.artifact_schema != self._capability.output_schema
                or output_artifact.produced_by_agent_version_id != resolved.agent_version.id
            ):
                raise ValueError
            normalized_input = ExtractRequirementsInput.model_validate(input_artifact.content)
            extraction = RequirementsExtractionOutput.model_validate(output_artifact.content)
            _validate_handoff(
                capability=self._capability,
                request=normalized_input,
                output=extraction,
            )
        except Exception:
            raise RequirementsExecutionError(RequirementsExecutionErrorCode.OUTPUT_INVALID) from None

        artifact = None
        if extraction.outcome in {
            RequirementsExtractionOutcome.SUCCESS,
            RequirementsExtractionOutcome.PARTIAL,
        }:
            try:
                artifact = await self._requirements_artifacts.persist_extracted_candidate(
                    context=persistence_context,
                    run_id=request.workflow_run_id,
                    extraction=extraction,
                    upstream_artifact_ids=(result.output_artifact_ids[0],),
                )
            except Exception:
                raise RequirementsExecutionError(RequirementsExecutionErrorCode.PERSISTENCE_UNAVAILABLE) from None
        return RequirementsExecutionResult(
            pin=pin,
            invocation=invocation,
            result=result,
            agent_result_ref=persisted.reference,
            extraction=extraction,
            artifact=artifact,
        )


def extract_requirements_artifact_schema(
    capability: CapabilityDefinition,
) -> CapabilityArtifactSchema:
    """Map the capability's typed payloads onto generic runtime Artifacts."""

    if type(capability) is not CapabilityDefinition or capability.key != "extract_requirements":
        raise ValueError("extract_requirements capability is required")
    expected = extract_requirements_capability()
    if (
        capability.input_schema != expected.input_schema
        or capability.output_schema != expected.output_schema
    ):
        raise ValueError("extract_requirements schemas are incompatible")
    return CapabilityArtifactSchema(
        schema_identity=CapabilitySchemaIdentity.from_capability(capability),
        input_model=ExtractRequirementsInput,
        output_model=RequirementsExtractionOutput,
        assemble_input=_assemble_input,
        validate_metadata=_validate_metadata,
        validate_handoff=_validate_runtime_handoff,
    )


build_extract_requirements_artifact_schema = extract_requirements_artifact_schema


async def deterministic_extract_requirements(
    request: AgentRequest[ExtractRequirementsInput],
    context: AgentExecutionContext,
) -> RequirementsExtractionOutput:
    """Small offline handler used for deterministic local qualification.

    It intentionally performs no persistence, binding resolution, retries,
    approvals, pricing or external calls.  Production providers can replace
    this implementation behind the same registered capability contract.
    """

    if not isinstance(request, AgentRequest) or not isinstance(context, AgentExecutionContext):
        raise TypeError("requirements extraction handler received invalid runtime contracts")
    value = request.input
    if not isinstance(value, ExtractRequirementsInput):
        raise TypeError("requirements extraction input is invalid")
    message = value.client_request.message.strip()
    requirement_id = uuid5(value.workflow_run_id, "requirements:1")
    if len(message.split()) < 3:
        from spine.domain.workflows import Requirement, RequirementCategory

        return RequirementsExtractionOutput(
            workspace_id=value.workspace_id,
            environment_id=value.environment_id,
            workflow_run_id=value.workflow_run_id,
            work_item_id=value.work_item_id,
            outcome=RequirementsExtractionOutcome.PARTIAL,
            requirements=(
                Requirement(
                    requirement_id=requirement_id,
                    category=RequirementCategory.UNRESOLVED_INFORMATION,
                    statement=message,
                    state=RequirementState.PARTIAL,
                    uncertainty="The request does not contain enough detail to extract a complete requirement.",
                    missing_markers=("scope", "success_criteria"),
                ),
            ),
        )
    from spine.domain.workflows import Requirement, RequirementCategory, RequirementEvidenceStatus

    return RequirementsExtractionOutput(
        workspace_id=value.workspace_id,
        environment_id=value.environment_id,
        workflow_run_id=value.workflow_run_id,
        work_item_id=value.work_item_id,
        outcome=RequirementsExtractionOutcome.SUCCESS,
        requirements=(
            Requirement(
                requirement_id=requirement_id,
                category=RequirementCategory.FUNCTIONAL,
                statement=message,
                state=RequirementState.CANDIDATE,
                source_refs=value.authorized_source_refs,
                evidence_status=RequirementEvidenceStatus.UNVERIFIED,
            ),
        ),
    )


def register_deterministic_extract_requirements_handler(registry: Any) -> None:
    """Register the offline handler explicitly in a caller-owned registry."""

    from spine.agents.catalog import AgentImplementationRegistration, HandlerRegistry
    from spine.agents.runtime import adapt_async_function

    if type(registry) is not HandlerRegistry:
        raise TypeError("requirements handler registry is invalid")
    capability = extract_requirements_capability()
    version = extract_requirements_agent_version()
    registry.register_handler(
        registration=AgentImplementationRegistration.from_version(
            version=version,
            capability=capability,
            implementation_key=EXTRACT_REQUIREMENTS_IMPLEMENTATION_KEY,
        ),
        handler=adapt_async_function(
            deterministic_extract_requirements,
            input_type=ExtractRequirementsInput,
            output_type=RequirementsExtractionOutput,
        ),
    )


def _assemble_input(artifacts: tuple[Artifact, ...]) -> Any:
    if len(artifacts) != 1:
        raise ValueError("requirements extraction requires exactly one input artifact")
    return artifacts[0].content


def _validate_metadata(response: AgentResponse[Any]) -> None:
    if response.proposed_action_ids:
        raise ValueError("requirements extraction cannot propose external actions")
    if response.error is not None:
        if response.evidence_refs or response.confidence is not None or response.structured_metrics:
            raise ValueError("failed requirements extraction contains provider metadata")
        return
    if any(not reference.strip() for reference in response.evidence_refs):
        raise ValueError("requirements extraction evidence references are invalid")
    if len(set(response.evidence_refs)) != len(response.evidence_refs):
        raise ValueError("requirements extraction evidence references must be unique")


def _validate_runtime_handoff(
    input_model: Any,
    output_model: Any,
    response: AgentResponse[Any],
) -> None:
    if response.error is not None:
        return
    if not isinstance(input_model, ExtractRequirementsInput) or not isinstance(
        output_model, RequirementsExtractionOutput
    ):
        raise ValueError("requirements extraction handoff is invalid")
    _validate_handoff(
        capability=extract_requirements_capability(),
        request=input_model,
        output=output_model,
    )


def _validate_handoff(
    *,
    capability: CapabilityDefinition,
    request: ExtractRequirementsInput,
    output: RequirementsExtractionOutput,
) -> None:
    if capability.key != output.capability:
        raise ValueError("requirements extraction capability identity changed")
    if (
        output.workspace_id != request.workspace_id
        or output.environment_id != request.environment_id
        or output.workflow_run_id != request.workflow_run_id
        or output.work_item_id != request.work_item_id
    ):
        raise ValueError("requirements extraction output scope changed")
    allowed = {
        (item.source_object_id, item.source_revision_id, item.provenance_id)
        for item in request.authorized_source_refs
    }
    for requirement in output.requirements:
        for source in requirement.source_refs:
            key = (source.source_object_id, source.source_revision_id, source.provenance_id)
            if key not in allowed:
                raise ValueError("requirements extraction cited an unauthorized source")
        for evidence in requirement.evidence_refs:
            key = (
                evidence.source_ref.source_object_id,
                evidence.source_ref.source_revision_id,
                evidence.source_ref.provenance_id,
            )
            if key not in allowed:
                raise ValueError("requirements extraction cited unauthorized evidence")


def _agent_version_digest(version: AgentVersion) -> str:
    return _digest({"schema": "agent-version-identity:v1", "payload": version.model_dump(mode="json")})


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, SchemaRef):
        return value.model_dump(mode="json")
    return value.model_dump(mode="json") if isinstance(value, ValidatedDefinitionModel) else value


def _binding_error_code(message: str) -> RequirementsExecutionErrorCode:
    if "deployment is not approved" in message:
        return RequirementsExecutionErrorCode.DEPLOYMENT_UNAPPROVED
    if "implementation is unavailable" in message:
        return RequirementsExecutionErrorCode.HANDLER_UNAVAILABLE
    if "schema is incompatible" in message:
        return RequirementsExecutionErrorCode.SCHEMA_INCOMPATIBLE
    return RequirementsExecutionErrorCode.BINDING_UNAVAILABLE


__all__ = [
    "EXTRACT_REQUIREMENTS_AGENT_ID",
    "EXTRACT_REQUIREMENTS_AGENT_VERSION_ID",
    "EXTRACT_REQUIREMENTS_CAPABILITY_ID",
    "EXTRACT_REQUIREMENTS_IMPLEMENTATION_KEY",
    "RequirementsArtifactCandidatePort",
    "RequirementsArtifactReader",
    "RequirementsExecutionError",
    "RequirementsExecutionErrorCode",
    "RequirementsExecutionPin",
    "RequirementsExecutionRequest",
    "RequirementsExecutionResult",
    "RequirementsExtractionExecutionBoundary",
    "RequirementsRuntimeProvider",
    "build_extract_requirements_artifact_schema",
    "deterministic_extract_requirements",
    "extract_requirements_agent_version",
    "extract_requirements_artifact_schema",
    "extract_requirements_capability",
    "register_deterministic_extract_requirements_handler",
]
