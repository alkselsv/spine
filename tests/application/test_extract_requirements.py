from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.agents.catalog import (
    AgentBindingResolver,
    AgentImplementationRegistration,
    DeploymentSelectionPolicy,
    HandlerRegistry,
)
from spine.agents.runtime import AgentExecutionContext, AgentRequest, AgentResponse
from spine.agents.runtime.artifacts import PersistedAgentInvocationResult
from spine.application.extract_requirements import (
    EXTRACT_REQUIREMENTS_IMPLEMENTATION_KEY,
    RequirementsExecutionRequest,
    RequirementsExtractionExecutionBoundary,
    extract_requirements_agent_version,
    extract_requirements_artifact_schema,
    extract_requirements_capability,
    register_deterministic_extract_requirements_handler,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.application.persistence.idempotency import OpaqueResultReference
from spine.domain.agents import (
    AgentBinding,
    AgentDeployment,
    AgentInvocation,
    AgentResult,
    AgentRunStatus,
    DeploymentStage,
)
from spine.domain.artifacts import Artifact
from spine.domain.capabilities import (
    ExtractRequirementsInput,
    RequirementsExtractionOutcome,
    RequirementsExtractionOutput,
)
from spine.domain.common import ActorKind, ActorRef, EnvironmentKind
from spine.domain.workflows import WorkflowRun


def uid(value: int) -> UUID:
    return UUID(int=value)


WORKSPACE = uid(1)
ENVIRONMENT = uid(2)
RUN = uid(3)
WORK_ITEM = uid(4)
INPUT_ARTIFACT = uid(5)
OUTPUT_ARTIFACT = uid(6)


def request() -> RequirementsExecutionRequest:
    return RequirementsExecutionRequest(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        environment=EnvironmentKind.DEVELOPMENT,
        workflow_run_id=RUN,
        work_item_id=WORK_ITEM,
        step_run_id=uid(7),
        agent_run_id=uid(8),
        input_artifact_id=INPUT_ARTIFACT,
        acting_on_behalf_of=ActorRef(kind=ActorKind.HUMAN, id=uid(9)),
        idempotency_key="requirements:run:3",
    )


def persistence_context() -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(WORKSPACE, ENVIRONMENT),
        origin=__import__("spine.domain.common", fromlist=["ContextOrigin"]).ContextOrigin.WORKER,
        purpose=PersistencePurpose("workflow_execution"),
        operation=PersistenceOperation("workflow_run_start"),
        trace_id=uid(10),
        acting_subject_id=None,
        service_principal_id=uid(11),
        provenance=TrustedContextProvenance(uid(12), b"trusted"),
    )


class Handler:
    async def invoke(
        self,
        request: AgentRequest[object],
        context: AgentExecutionContext,
    ) -> AgentResponse[object]:
        del request, context
        raise AssertionError("handler is owned by the runtime")


@dataclass
class Catalog:
    version: object

    def list_bindings(self, workspace_id, environment, capability_key):
        return (
            AgentBinding(
                id=uid(20),
                workspace_id=workspace_id,
                environment=environment,
                capability=capability_key,
                agent_version_id=self.version.id,
            ),
        )

    def get_agent_version(self, agent_version_id):
        return self.version if agent_version_id == self.version.id else None

    def list_deployments(self, workspace_id, environment, agent_version_id):
        return (
            AgentDeployment(
                id=uid(21),
                workspace_id=workspace_id,
                environment=environment,
                agent_version_id=agent_version_id,
                stage=DeploymentStage.DEVELOPMENT,
            ),
        )


class Runtime:
    def __init__(self, output: Artifact) -> None:
        self.output = output
        self.calls: list[tuple[object, AgentInvocation]] = []

    async def invoke_with_reference(self, version, invocation):
        self.calls.append((version, invocation))
        return PersistedAgentInvocationResult(
            result=AgentResult(
                status=AgentRunStatus.SUCCEEDED,
                output_artifact_ids=(OUTPUT_ARTIFACT,),
            ),
            reference=OpaqueResultReference(
                result_type="agent_invocation", result_id=uid(8), schema_version=1
            ),
        )


class Reader:
    def __init__(self, input_artifact: Artifact, output_artifact: Artifact) -> None:
        self.items = {input_artifact.id: input_artifact, output_artifact.id: output_artifact}

    async def load(self, artifact_id, *, workspace_id, environment_id, work_item_id):
        del workspace_id, environment_id, work_item_id
        return self.items.get(artifact_id)


class ArtifactService:
    def __init__(self) -> None:
        self.calls = []

    async def persist_extracted_candidate(self, **kwargs):
        self.calls.append(kwargs)
        return "requirements-artifact-v1"


def boundary() -> tuple[RequirementsExtractionExecutionBoundary, Runtime, ArtifactService]:
    capability = extract_requirements_capability()
    version = extract_requirements_agent_version()
    registry = HandlerRegistry()
    registry.register_handler(
        registration=AgentImplementationRegistration.from_version(
            version=version,
            capability=capability,
            implementation_key=EXTRACT_REQUIREMENTS_IMPLEMENTATION_KEY,
        ),
        handler=Handler(),
    )
    registry.finalize()
    resolver = AgentBindingResolver(
        catalog=Catalog(version),
        registry=registry,
        approved_deployment_policy=DeploymentSelectionPolicy(
            allowed_stages=frozenset({DeploymentStage.DEVELOPMENT})
        ),
    )
    input_model = ExtractRequirementsInput(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        environment=EnvironmentKind.DEVELOPMENT,
        workflow_run_id=RUN,
        work_item_id=WORK_ITEM,
        client_request={"message": "Prepare proposal requirements"},
    )
    output_model = RequirementsExtractionOutput(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        workflow_run_id=RUN,
        work_item_id=WORK_ITEM,
        outcome=RequirementsExtractionOutcome.ABSTAINED,
        reason={"code": "insufficient_information"},
    )
    schema = extract_requirements_artifact_schema(capability)
    input_artifact = Artifact(
        id=INPUT_ARTIFACT,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        work_item_id=WORK_ITEM,
        produced_by=ActorRef(kind=ActorKind.HUMAN, id=uid(9)),
        artifact_schema=capability.input_schema,
        content=input_model.model_dump(mode="json"),
    )
    output_artifact = Artifact(
        id=OUTPUT_ARTIFACT,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        work_item_id=WORK_ITEM,
        produced_by=ActorRef(kind=ActorKind.AGENT, id=version.agent_id),
        produced_by_agent_version_id=version.id,
        artifact_schema=capability.output_schema,
        content=output_model.model_dump(mode="json"),
    )
    runtime = Runtime(output_artifact)
    reader = Reader(input_artifact, output_artifact)
    service = ArtifactService()

    class Provider:
        def runtime_for(self, **kwargs):
            return runtime

    return (
        RequirementsExtractionExecutionBoundary(
            capability=capability,
            binding_resolver=resolver,
            runtime_provider=Provider(),
            artifacts=reader,
            requirements_artifacts=service,
        ),
        runtime,
        service,
    )


@pytest.mark.asyncio
async def test_execution_pins_binding_and_runtime_config_and_preserves_typed_abstention() -> None:
    service, runtime, artifacts = boundary()

    result = await service.execute(
        request=request(), persistence_context=persistence_context()
    )

    assert result.pin.implementation_key == EXTRACT_REQUIREMENTS_IMPLEMENTATION_KEY
    assert result.pin.agent_version_id == extract_requirements_agent_version().id
    assert runtime.calls[0][1].execution_configuration_digest == result.pin.pin_digest
    assert result.extraction is not None
    assert result.extraction.outcome is RequirementsExtractionOutcome.ABSTAINED
    assert result.artifact is None
    assert artifacts.calls == []


def test_extract_requirements_schema_rejects_wrong_capability_schema() -> None:
    capability = extract_requirements_capability().model_copy(
        update={"output_schema": {"name": "other", "version": "v1"}}
    )
    with pytest.raises(ValueError, match="schemas are incompatible"):
        extract_requirements_artifact_schema(capability)


def test_deterministic_handler_is_registered_through_the_existing_registry() -> None:
    registry = HandlerRegistry()
    register_deterministic_extract_requirements_handler(registry)
    registry.finalize()

    selected = registry.construct(
        capability=extract_requirements_capability(),
        version=extract_requirements_agent_version(),
    )
    assert selected is not None
