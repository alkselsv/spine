from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.agents.catalog import (
    AgentBindingResolver,
    AgentImplementationRegistration,
    DeploymentSelectionPolicy,
    HandlerRegistry,
)
from spine.agents.runtime import AgentExecutionContext, AgentRequest, AgentResponse
from spine.application.answer_question_execution import (
    AnswerQuestionExecutionBoundary,
    AnswerQuestionExecutionError,
    AnswerQuestionExecutionErrorCode,
    AnswerQuestionExecutionIntent,
    AnswerQuestionExecutionRequest,
    AnswerQuestionRuntimeProvider,
    PinnedAgentVersion,
    PinnedConfigurationVersion,
    PinnedContextProfile,
    PinnedProjection,
    TrustedAnswerQuestionConfiguration,
)
from spine.domain.agents import (
    AgentBinding,
    AgentDeployment,
    AgentInvocation,
    AgentResult,
    AgentRunStatus,
    AgentRuntimeKind,
    AgentVersion,
    DeploymentStage,
)
from spine.domain.capabilities import CapabilityDefinition
from spine.domain.capabilities.answer_question_execution import agent_version_digest
from spine.domain.common import ActorKind, ActorRef, EnvironmentKind, SchemaRef
from spine.domain.context_profiles import ContextProfileRef


def uid(value: int) -> UUID:
    return UUID(int=value)


WORKSPACE_ID = uid(1)
ENVIRONMENT_ID = uid(2)
ENVIRONMENT = EnvironmentKind.DEVELOPMENT


def capability() -> CapabilityDefinition:
    return CapabilityDefinition(
        id=uid(10),
        key="answer_question",
        display_name="Answer question",
        input_schema=SchemaRef(name="answer-question.input", version="v1"),
        output_schema=SchemaRef(name="answer-question.output", version="v1"),
    )


def agent_version() -> AgentVersion:
    return AgentVersion(
        id=uid(20),
        agent_id=uid(21),
        version="2026.10.1",
        runtime=AgentRuntimeKind.LLM,
        capabilities=("answer_question",),
        prompt_version="qa.prompt:7",
        model_profile="qa.model:4",
        context_profile="qa.context:3",
        runtime_config={"provider_family": "test"},
    )


class Handler:
    async def invoke(
        self,
        request: AgentRequest[object],
        context: AgentExecutionContext,
    ) -> AgentResponse[object]:
        del request, context
        raise AssertionError("pin resolution must not invoke the handler directly")


@dataclass
class Catalog:
    version: AgentVersion
    deployment_stage: DeploymentStage = DeploymentStage.DEVELOPMENT

    def list_bindings(
        self,
        workspace_id: UUID,
        environment: EnvironmentKind,
        capability_key: str,
    ) -> tuple[AgentBinding, ...]:
        del workspace_id, environment, capability_key
        return (
            AgentBinding(
                id=uid(30),
                workspace_id=WORKSPACE_ID,
                environment=ENVIRONMENT,
                capability="answer_question",
                agent_version_id=self.version.id,
            ),
        )

    def get_agent_version(self, agent_version_id: UUID) -> AgentVersion | None:
        return self.version if agent_version_id == self.version.id else None

    def list_deployments(
        self,
        workspace_id: UUID,
        environment: EnvironmentKind,
        agent_version_id: UUID,
    ) -> tuple[AgentDeployment, ...]:
        del workspace_id, environment, agent_version_id
        return (
            AgentDeployment(
                id=uid(31),
                workspace_id=WORKSPACE_ID,
                environment=ENVIRONMENT,
                agent_version_id=self.version.id,
                stage=self.deployment_stage,
            ),
        )


def configuration(
    *,
    workspace_id: UUID = WORKSPACE_ID,
    environment_id: UUID = ENVIRONMENT_ID,
    input_schema: SchemaRef | None = None,
    approved: bool = True,
) -> TrustedAnswerQuestionConfiguration:
    scope = {"workspace_id": workspace_id, "environment_id": environment_id}
    return TrustedAnswerQuestionConfiguration(
        configuration_generation=9,
        capability_id=uid(10),
        input_schema=input_schema
        or SchemaRef(name="answer-question.input", version="v1"),
        output_schema=SchemaRef(name="answer-question.output", version="v1"),
        agent_version=PinnedAgentVersion(
            **scope,
            configuration_generation=9,
            agent_version_id=uid(20),
            agent_id=uid(21),
            version="2026.10.1",
            digest=agent_version_digest(agent_version()),
        ),
        prompt_version=PinnedConfigurationVersion(
            **scope,
            configuration_generation=9,
            identity_id=uid(40),
            binding_key="qa.prompt:7",
            version="7",
            digest="b" * 64,
        ),
        model_profile=PinnedConfigurationVersion(
            **scope,
            configuration_generation=9,
            identity_id=uid(41),
            binding_key="qa.model:4",
            version="4",
            digest="c" * 64,
        ),
        context_profile=PinnedContextProfile(
            **scope,
            configuration_generation=9,
            binding_key="qa.context:3",
            profile=ContextProfileRef(
                profile_id=uid(42),
                profile_version=3,
                profile_digest="d" * 64,
            ),
        ),
        projection=PinnedProjection(
            **scope,
            configuration_generation=9,
            projection_snapshot_id=uid(43),
            snapshot_manifest_digest="e" * 64,
            projection_activation_id=uid(44),
            activation_generation=12,
            projection_config_version_id=uid(45),
            projection_config_digest="f" * 64,
            canonical_boundary_id=uid(46),
            canonical_boundary_digest="1" * 64,
        ),
        semantic_support_policy=PinnedConfigurationVersion(
            **scope,
            configuration_generation=9,
            identity_id=uid(47),
            binding_key="qa.semantic-support:2",
            version="2",
            digest="2" * 64,
        ),
        approved=approved,
    )


@dataclass
class ConfigurationCatalog:
    selected: object
    trusted: object | None = None
    verified: list[object] | None = None

    def __post_init__(self) -> None:
        if self.trusted is None:
            self.trusted = self.selected
        if self.verified is None:
            self.verified = []

    def resolve_for_agent_version(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        environment: EnvironmentKind,
        capability_key: str,
        agent_version_id: UUID,
    ) -> object:
        del workspace_id, environment_id, environment, capability_key, agent_version_id
        return self.selected

    def verify_configuration(
        self,
        configuration: TrustedAnswerQuestionConfiguration,
    ) -> bool:
        assert self.verified is not None
        self.verified.append(configuration)
        return configuration == self.trusted


class Runtime:
    def __init__(self) -> None:
        self.calls: list[tuple[AgentVersion, AgentInvocation]] = []
        self.effects: list[str] = []
        self._results: dict[str, AgentResult] = {}
        self._commands: dict[str, tuple[AgentVersion, AgentInvocation]] = {}

    async def invoke(
        self,
        version: AgentVersion,
        invocation: AgentInvocation,
    ) -> AgentResult:
        self.calls.append((version, invocation))
        existing = self._commands.get(invocation.idempotency_key)
        if existing is not None and existing != (version, invocation):
            return AgentResult(
                status=AgentRunStatus.FAILED,
                error="Agent execution failed.",
            )
        if invocation.idempotency_key not in self._results:
            self.effects.append(invocation.idempotency_key)
            self._commands[invocation.idempotency_key] = (version, invocation)
            self._results[invocation.idempotency_key] = AgentResult(
                status=AgentRunStatus.SUCCEEDED
            )
        return self._results[invocation.idempotency_key]

    async def cancel(self, step_run_id: UUID, *, scope: object = None) -> None:
        del step_run_id, scope


class RuntimeProvider(AnswerQuestionRuntimeProvider):
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.selections: list[tuple[object, ...]] = []

    def runtime_for(
        self,
        *,
        resolved_binding: object,
        agent_version: AgentVersion,
        handler: object,
        implementation_key: str,
        pin: object,
    ) -> Runtime:
        self.selections.append(
            (
                resolved_binding,
                agent_version,
                handler,
                implementation_key,
                pin,
            )
        )
        return self.runtime


def boundary(
    *,
    selected_configuration: object | None = None,
    runtime: Runtime | None = None,
    register_handler: bool = True,
    deployment_stage: DeploymentStage = DeploymentStage.DEVELOPMENT,
    trusted_configuration: object | None = None,
    configuration_catalog: ConfigurationCatalog | None = None,
) -> tuple[AnswerQuestionExecutionBoundary, Runtime, RuntimeProvider]:
    version = agent_version()
    selected_capability = capability()
    registry = HandlerRegistry()
    if register_handler:
        registry.register_handler(
            registration=AgentImplementationRegistration.from_version(
                version=version,
                capability=selected_capability,
                implementation_key="qa.answer.local",
            ),
            handler=Handler(),
        )
    registry.finalize()
    resolver = AgentBindingResolver(
        catalog=Catalog(version, deployment_stage=deployment_stage),
        registry=registry,
        approved_deployment_policy=DeploymentSelectionPolicy(
            allowed_stages=frozenset({DeploymentStage.DEVELOPMENT})
        ),
    )
    selected_runtime = runtime or Runtime()
    provider = RuntimeProvider(selected_runtime)
    selected = selected_configuration or configuration()
    catalog = configuration_catalog or ConfigurationCatalog(
        selected,
        trusted=trusted_configuration or selected,
    )
    return (
        AnswerQuestionExecutionBoundary(
            capability=selected_capability,
            binding_resolver=resolver,
            configuration_catalog=catalog,
            runtime_provider=provider,
        ),
        selected_runtime,
        provider,
    )


def request(
    *,
    workspace_id: UUID = WORKSPACE_ID,
    environment_id: UUID = ENVIRONMENT_ID,
    allow_partial_answer: bool = False,
) -> AnswerQuestionExecutionRequest:
    return AnswerQuestionExecutionRequest(
        workspace_id=workspace_id,
        environment_id=environment_id,
        environment=ENVIRONMENT,
        intent=AnswerQuestionExecutionIntent(
            allow_partial_answer=allow_partial_answer
        ),
    )


def invocation(**constraints: object) -> AgentInvocation:
    return AgentInvocation(
        work_item_id=uid(50),
        step_run_id=uid(51),
        capability="answer_question",
        acting_on_behalf_of=ActorRef(kind=ActorKind.HUMAN, id=uid(52)),
        input_artifact_ids=(uid(53),),
        constraints=constraints,
        idempotency_key="qa:50:1",
    )


@pytest.mark.asyncio
async def test_trusted_configuration_executes_with_one_immutable_replayable_pin() -> None:
    service, runtime, provider = boundary()
    selected_invocation = invocation(allow_partial_answer=True)

    first = await service.execute(
        request(allow_partial_answer=True), selected_invocation
    )
    replay = await service.execute(
        request(allow_partial_answer=True), selected_invocation
    )

    assert first.pin == replay.pin
    assert first.pin.pin_digest == replay.pin.pin_digest
    assert first.pin.agent_version.digest == agent_version_digest(agent_version())
    assert first.pin.prompt_version.digest == "b" * 64
    assert first.pin.model_profile.digest == "c" * 64
    assert first.pin.context_profile.profile.profile_digest == "d" * 64
    assert first.pin.projection.snapshot_manifest_digest == "e" * 64
    assert first.pin.semantic_support_policy.digest == "2" * 64
    assert first.result.status is AgentRunStatus.SUCCEEDED
    expected_runtime_invocation = selected_invocation.model_copy(
        update={"execution_configuration_digest": first.pin.pin_digest}
    )
    assert runtime.calls == [
        (agent_version(), expected_runtime_invocation),
        (agent_version(), expected_runtime_invocation),
    ]
    assert runtime.effects == [selected_invocation.idempotency_key]
    first_selection = provider.selections[0]
    assert first_selection[1] == agent_version()
    assert first_selection[2] is not None
    assert first_selection[3] == "qa.answer.local"
    assert first_selection[4] == first.pin
    serialized = first.pin.model_dump(mode="json")
    assert "runtime_config" not in serialized
    assert "confidence" not in serialized


@pytest.mark.parametrize(
    "selector",
    [
        "agentVersion",
        "prompt-version",
        "model_profile",
        "contextProfile",
        "projection_id",
        "validation-policy",
    ],
)
@pytest.mark.asyncio
async def test_caller_authoritative_version_override_is_rejected_before_runtime(
    selector: str,
) -> None:
    service, runtime, _ = boundary()

    with pytest.raises(AnswerQuestionExecutionError) as captured:
        await service.execute(
            request(),
            invocation(options={selector: "caller-choice"}),
        )

    assert captured.value.code is AnswerQuestionExecutionErrorCode.INVALID_REQUEST
    assert runtime.calls == []
    with pytest.raises(ValidationError):
        AnswerQuestionExecutionRequest.model_validate(
            {
                "workspace_id": WORKSPACE_ID,
                "environment_id": ENVIRONMENT_ID,
                "environment": ENVIRONMENT,
                "agent_version_id": uid(999),
            }
        )


@pytest.mark.parametrize(
    ("selected_configuration", "expected_code"),
    [
        (
            configuration(workspace_id=uid(100)),
            AnswerQuestionExecutionErrorCode.SCOPE_MISMATCH,
        ),
        (
            configuration(environment_id=uid(101)),
            AnswerQuestionExecutionErrorCode.SCOPE_MISMATCH,
        ),
        (
            configuration(
                input_schema=SchemaRef(name="answer-question.input", version="v2")
            ),
            AnswerQuestionExecutionErrorCode.SCHEMA_INCOMPATIBLE,
        ),
        (
            configuration(approved=False),
            AnswerQuestionExecutionErrorCode.CONFIGURATION_UNAPPROVED,
        ),
    ],
)
def test_incompatible_configuration_fails_closed_with_typed_error(
    selected_configuration: TrustedAnswerQuestionConfiguration,
    expected_code: AnswerQuestionExecutionErrorCode,
) -> None:
    service, _, _ = boundary(selected_configuration=selected_configuration)

    with pytest.raises(AnswerQuestionExecutionError) as captured:
        service.resolve(request())

    assert captured.value.code is expected_code
    assert str(captured.value) == expected_code.value


@pytest.mark.parametrize(
    "corrupted_field",
    ["malformed_digest", "mismatched_agent_digest", "configuration_generation"],
)
def test_malformed_digest_or_generation_fails_closed(
    corrupted_field: str,
) -> None:
    selected = configuration()
    if corrupted_field == "malformed_digest":
        object.__setattr__(selected.prompt_version, "digest", "not-a-digest")
    elif corrupted_field == "mismatched_agent_digest":
        object.__setattr__(selected.agent_version, "digest", "0" * 64)
    else:
        object.__setattr__(selected.prompt_version, "configuration_generation", 10)
    service, _, _ = boundary(selected_configuration=selected)

    with pytest.raises(AnswerQuestionExecutionError) as captured:
        service.resolve(request())

    assert captured.value.code is AnswerQuestionExecutionErrorCode.CONFIGURATION_INVALID


@pytest.mark.parametrize(
    ("service_options", "expected_code"),
    [
        (
            {"deployment_stage": DeploymentStage.PRODUCTION},
            AnswerQuestionExecutionErrorCode.DEPLOYMENT_UNAPPROVED,
        ),
        (
            {"register_handler": False},
            AnswerQuestionExecutionErrorCode.HANDLER_UNAVAILABLE,
        ),
    ],
)
def test_unapproved_deployment_or_missing_handler_fails_closed(
    service_options: dict[str, object],
    expected_code: AnswerQuestionExecutionErrorCode,
) -> None:
    service, _, _ = boundary(**service_options)  # type: ignore[arg-type]

    with pytest.raises(AnswerQuestionExecutionError) as captured:
        service.resolve(request())

    assert captured.value.code is expected_code


def test_pin_is_immutable_and_detects_digest_tampering() -> None:
    service, _, _ = boundary()
    _, pin = service.resolve(request())

    with pytest.raises(ValidationError):
        pin.model_copy(update={"pin_digest": "0" * 64})
    with pytest.raises(ValidationError):
        pin.agent_version.model_copy(update={"digest": "invalid"})


def test_well_formed_but_untrusted_configuration_digest_fails_closed() -> None:
    trusted = configuration()
    forged_prompt = trusted.prompt_version.model_copy(update={"digest": "9" * 64})
    forged = trusted.model_copy(update={"prompt_version": forged_prompt})
    service, _, _ = boundary(
        selected_configuration=forged,
        trusted_configuration=trusted,
    )

    with pytest.raises(AnswerQuestionExecutionError) as captured:
        service.resolve(request())

    assert captured.value.code is AnswerQuestionExecutionErrorCode.CONFIGURATION_INVALID


@pytest.mark.asyncio
async def test_unbounded_or_version_like_constraints_are_rejected() -> None:
    service, runtime, _ = boundary()

    for constraints in (
        {"preferred_model": "caller-choice"},
        {"projection_snapshot_id": str(uid(999))},
        {"temperature": 0.2},
    ):
        with pytest.raises(AnswerQuestionExecutionError) as captured:
            await service.execute(request(), invocation(**constraints))
        assert captured.value.code is AnswerQuestionExecutionErrorCode.INVALID_REQUEST

    assert runtime.calls == []


@pytest.mark.asyncio
async def test_caller_cannot_supply_server_execution_configuration_digest() -> None:
    service, runtime, _ = boundary()
    selected_invocation = invocation().model_copy(
        update={"execution_configuration_digest": "9" * 64}
    )

    with pytest.raises(AnswerQuestionExecutionError) as captured:
        await service.execute(request(), selected_invocation)

    assert captured.value.code is AnswerQuestionExecutionErrorCode.INVALID_REQUEST
    assert runtime.calls == []


def test_two_workspaces_resolve_isolated_pins() -> None:
    first_service, _, _ = boundary()
    second_workspace = uid(100)
    second_configuration = configuration(workspace_id=second_workspace)

    version = agent_version()
    selected_capability = capability()
    registry = HandlerRegistry()
    registry.register_handler(
        registration=AgentImplementationRegistration.from_version(
            version=version,
            capability=selected_capability,
            implementation_key="qa.answer.local",
        ),
        handler=Handler(),
    )
    registry.finalize()
    second_catalog = Catalog(version)
    original_list_bindings = second_catalog.list_bindings

    def second_bindings(
        workspace_id: UUID,
        environment: EnvironmentKind,
        capability_key: str,
    ) -> tuple[AgentBinding, ...]:
        binding = original_list_bindings(workspace_id, environment, capability_key)[0]
        return (binding.model_copy(update={"workspace_id": second_workspace}),)

    second_catalog.list_bindings = second_bindings  # type: ignore[method-assign]
    original_deployments = second_catalog.list_deployments

    def second_deployments(
        workspace_id: UUID,
        environment: EnvironmentKind,
        agent_version_id: UUID,
    ) -> tuple[AgentDeployment, ...]:
        deployment = original_deployments(
            workspace_id, environment, agent_version_id
        )[0]
        return (deployment.model_copy(update={"workspace_id": second_workspace}),)

    second_catalog.list_deployments = second_deployments  # type: ignore[method-assign]
    second_runtime = Runtime()
    second_service = AnswerQuestionExecutionBoundary(
        capability=selected_capability,
        binding_resolver=AgentBindingResolver(
            catalog=second_catalog,
            registry=registry,
            approved_deployment_policy=DeploymentSelectionPolicy(
                allowed_stages=frozenset({DeploymentStage.DEVELOPMENT})
            ),
        ),
        configuration_catalog=ConfigurationCatalog(second_configuration),
        runtime_provider=RuntimeProvider(second_runtime),
    )

    _, first_pin = first_service.resolve(request())
    _, second_pin = second_service.resolve(
        request(workspace_id=second_workspace)
    )

    assert first_pin.workspace_id == WORKSPACE_ID
    assert second_pin.workspace_id == second_workspace
    assert first_pin.pin_digest != second_pin.pin_digest


@pytest.mark.asyncio
async def test_changed_configuration_conflicts_with_existing_replay_identity() -> None:
    initial = configuration()
    catalog = ConfigurationCatalog(initial)
    service, runtime, _ = boundary(configuration_catalog=catalog)
    selected_invocation = invocation()

    first = await service.execute(request(), selected_invocation)
    next_generation = 10
    changed = initial.model_copy(
        update={
            "configuration_generation": next_generation,
            "agent_version": initial.agent_version.model_copy(
                update={"configuration_generation": next_generation}
            ),
            "prompt_version": initial.prompt_version.model_copy(
                update={
                    "configuration_generation": next_generation,
                    "identity_id": uid(140),
                    "version": "8",
                    "digest": "8" * 64,
                }
            ),
            "model_profile": initial.model_profile.model_copy(
                update={"configuration_generation": next_generation}
            ),
            "context_profile": initial.context_profile.model_copy(
                update={"configuration_generation": next_generation}
            ),
            "projection": initial.projection.model_copy(
                update={"configuration_generation": next_generation}
            ),
            "semantic_support_policy": initial.semantic_support_policy.model_copy(
                update={"configuration_generation": next_generation}
            ),
        }
    )
    catalog.selected = changed
    catalog.trusted = changed

    conflict = await service.execute(request(), selected_invocation)

    assert first.result.status is AgentRunStatus.SUCCEEDED
    assert conflict.result.status is AgentRunStatus.FAILED
    assert conflict.pin.pin_digest != first.pin.pin_digest
    assert runtime.effects == [selected_invocation.idempotency_key]
