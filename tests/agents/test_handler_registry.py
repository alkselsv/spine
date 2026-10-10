from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict

from spine.agents.catalog import (
    AgentBindingCatalog,
    AgentBindingResolver,
    BindingResolutionError,
    BindingResolutionRequest,
    DeploymentSelectionPolicy,
    HandlerRegistry,
    HandlerRegistryError,
)
from spine.agents.runtime import (
    AgentExecutionContext,
    AgentHandler,
    AgentRequest,
    AgentResponse,
    CapabilitySchemaIdentity,
    adapt_async_function,
)
from spine.domain.agents import (
    AgentBinding,
    AgentDeployment,
    AgentRuntimeKind,
    AgentVersion,
    DeploymentStage,
)
from spine.domain.capabilities import CapabilityDefinition
from spine.domain.common import EnvironmentKind, SchemaRef


def uid(value: int) -> UUID:
    return UUID(int=value)


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str


class OutputModel(BaseModel):
    answer: str


class StructuralHandler:
    async def invoke(
        self,
        request: AgentRequest[InputModel],
        context: AgentExecutionContext,
    ) -> AgentResponse[OutputModel]:
        del request, context
        raise AssertionError("registry resolution must not invoke a handler")


class MalformedHandler:
    async def invoke(self, request: AgentRequest[InputModel]) -> AgentResponse[OutputModel]:
        del request
        raise AssertionError("malformed handler must not be invoked")


def capability(*, identity_seed: int = 10) -> CapabilityDefinition:
    return CapabilityDefinition(
        id=uid(identity_seed),
        key="answer_question",
        display_name="Answer question",
        input_schema=SchemaRef(name="answer_question.input", version="1"),
        output_schema=SchemaRef(name="answer_question.output", version="1"),
    )


def version(
    *,
    version_id: int = 20,
    implementation_key: str = "qa.answer.local",
    runtime: AgentRuntimeKind = AgentRuntimeKind.CODE,
    capabilities: tuple[str, ...] = ("answer_question",),
) -> AgentVersion:
    return AgentVersion(
        id=uid(version_id),
        agent_id=uid(21),
        version="2026.10.1",
        runtime=runtime,
        capabilities=capabilities,
        runtime_config={"implementation_key": implementation_key},
    )


def binding(
    *,
    binding_id: int = 30,
    workspace_id: UUID = uid(1),
    environment: EnvironmentKind = EnvironmentKind.DEVELOPMENT,
    version_id: int = 20,
    priority: int = 100,
) -> AgentBinding:
    return AgentBinding(
        id=uid(binding_id),
        workspace_id=workspace_id,
        environment=environment,
        capability=capability().key,
        agent_version_id=uid(version_id),
        priority=priority,
    )


def deployment(
    *,
    deployment_id: int = 40,
    workspace_id: UUID = uid(1),
    environment: EnvironmentKind = EnvironmentKind.DEVELOPMENT,
    version_id: int = 20,
    stage: DeploymentStage = DeploymentStage.DEVELOPMENT,
    traffic_percentage: int = 100,
) -> AgentDeployment:
    return AgentDeployment(
        id=uid(deployment_id),
        workspace_id=workspace_id,
        environment=environment,
        agent_version_id=uid(version_id),
        stage=stage,
        traffic_percentage=traffic_percentage,
    )


@dataclass
class InMemoryCatalog:
    bindings: tuple[AgentBinding, ...] = ()
    versions: dict[UUID, AgentVersion] = field(default_factory=dict)
    deployments: tuple[AgentDeployment, ...] = ()

    def list_bindings(
        self,
        workspace_id: UUID,
        environment: EnvironmentKind,
        capability_key: str,
    ) -> tuple[AgentBinding, ...]:
        del workspace_id, environment, capability_key
        return self.bindings

    def get_agent_version(self, agent_version_id: UUID) -> AgentVersion | None:
        return self.versions.get(agent_version_id)

    def list_deployments(
        self,
        workspace_id: UUID,
        environment: EnvironmentKind,
        agent_version_id: UUID,
    ) -> tuple[AgentDeployment, ...]:
        del workspace_id, environment, agent_version_id
        return self.deployments


def identity() -> CapabilitySchemaIdentity:
    return CapabilitySchemaIdentity.from_capability(capability())


def registry_with_factory(
    *,
    implementation_key: str = "qa.answer.local",
    selected_capability: CapabilityDefinition | None = None,
    factory=None,
) -> HandlerRegistry:
    selected_capability = selected_capability or capability()
    factory = factory or (lambda selected_version: StructuralHandler())
    registry = HandlerRegistry()
    registry.register_factory(
        implementation_key=implementation_key,
        capability=selected_capability,
        schema_identity=CapabilitySchemaIdentity.from_capability(selected_capability),
        factory=factory,
    )
    registry.finalize()
    return registry


def resolution_request(
    *,
    selected_capability: CapabilityDefinition | None = None,
    environment: EnvironmentKind = EnvironmentKind.DEVELOPMENT,
    allowed_stages: frozenset[DeploymentStage] = frozenset({DeploymentStage.DEVELOPMENT}),
) -> BindingResolutionRequest:
    return BindingResolutionRequest(
        workspace_id=uid(1),
        environment=environment,
        capability=selected_capability or capability(),
        deployment_policy=DeploymentSelectionPolicy(allowed_stages=allowed_stages),
    )


def test_registry_registers_structural_handler_and_constructs_after_finalize() -> None:
    handler = StructuralHandler()
    registry = HandlerRegistry()
    registry.register_handler(
        implementation_key="qa.answer.local",
        capability=capability(),
        schema_identity=identity(),
        handler=handler,
    )

    with pytest.raises(HandlerRegistryError):
        registry.construct(
            implementation_key="qa.answer.local",
            capability=capability(),
            version=version(),
        )

    registry.finalize()
    assert registry.construct(
        implementation_key="qa.answer.local",
        capability=capability(),
        version=version(),
    ) is handler


def test_registry_accepts_explicit_async_function_adapter() -> None:
    async def handler(
        request: AgentRequest[InputModel],
        context: AgentExecutionContext,
    ) -> OutputModel:
        del request, context
        return OutputModel(answer="unused")

    adapted = adapt_async_function(handler)
    registry = HandlerRegistry()
    registry.register_handler(
        implementation_key="qa.answer.async",
        capability=capability(),
        schema_identity=identity(),
        handler=adapted,
    )
    registry.finalize()

    assert registry.construct(
        implementation_key="qa.answer.async",
        capability=capability(),
        version=version(implementation_key="qa.answer.async"),
    ) is adapted


def test_registry_factory_receives_pinned_version_and_does_not_invoke_handler() -> None:
    constructed: list[AgentVersion] = []

    def factory(selected_version: AgentVersion) -> StructuralHandler:
        constructed.append(selected_version)
        return StructuralHandler()

    registry = registry_with_factory(factory=factory)
    selected_version = version()

    handler = registry.construct(
        implementation_key="qa.answer.local",
        capability=capability(),
        version=selected_version,
    )

    assert isinstance(handler, AgentHandler)
    assert constructed == [selected_version]
    assert constructed[0] is selected_version


@pytest.mark.parametrize(
    ("method", "value"),
    (
        ("register_handler", StructuralHandler()),
        ("register_factory", lambda selected_version: StructuralHandler()),
    ),
)
def test_registry_rejects_duplicate_capability_registration(method, value) -> None:
    registry = HandlerRegistry()
    kwargs = {
        "implementation_key": "qa.answer.local",
        "capability": capability(),
        "schema_identity": identity(),
    }
    registration_value = {"handler" if method == "register_handler" else "factory": value}
    getattr(registry, method)(**kwargs, **registration_value)

    with pytest.raises(HandlerRegistryError, match="duplicate"):
        getattr(registry, method)(**kwargs, **registration_value)


def test_registry_rejects_incompatible_schema_mapping() -> None:
    selected_capability = capability()
    mismatched_identity = CapabilitySchemaIdentity.from_capability(
        capability(identity_seed=11)
    )
    registry = HandlerRegistry()

    with pytest.raises(HandlerRegistryError, match="schema"):
        registry.register_factory(
            implementation_key="qa.answer.local",
            capability=selected_capability,
            schema_identity=mismatched_identity,
            factory=lambda selected_version: StructuralHandler(),
        )


def test_registry_rejects_construct_request_with_mismatched_schema_identity() -> None:
    registry = registry_with_factory()

    with pytest.raises(HandlerRegistryError, match="schema"):
        registry.construct(
            implementation_key="qa.answer.local",
            capability=capability(identity_seed=11),
            version=version(),
        )


def test_registry_rejects_missing_or_invalid_factories_and_handlers() -> None:
    registry = HandlerRegistry()

    with pytest.raises(HandlerRegistryError):
        registry.register_factory(
            implementation_key="qa.answer.local",
            capability=capability(),
            schema_identity=identity(),
            factory=None,
        )
    with pytest.raises(HandlerRegistryError):
        registry.register_handler(
            implementation_key="qa.answer.invalid",
            capability=capability(),
            schema_identity=identity(),
            handler=object(),
        )
    with pytest.raises(HandlerRegistryError):
        registry.register_handler(
            implementation_key="qa.answer.malformed",
            capability=capability(),
            schema_identity=identity(),
            handler=MalformedHandler(),
        )
    with pytest.raises(HandlerRegistryError):
        registry.register_factory(
            implementation_key="qa.answer.malformed",
            capability=capability(),
            schema_identity=identity(),
            factory=lambda: StructuralHandler(),
        )


def test_registry_sanitizes_factory_failures() -> None:
    def failing_factory(selected_version: AgentVersion) -> StructuralHandler:
        del selected_version
        raise RuntimeError("provider=secret password=hidden")

    registry = registry_with_factory(factory=failing_factory)

    with pytest.raises(HandlerRegistryError) as error:
        registry.construct(
            implementation_key="qa.answer.local",
            capability=capability(),
            version=version(),
        )

    assert str(error.value) == "local handler construction failed"
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("implementation_key", ("module:handler", "../handler", "handler.py"))
def test_registry_rejects_executable_import_path_keys(implementation_key: str) -> None:
    with pytest.raises(HandlerRegistryError):
        HandlerRegistry().register_factory(
            implementation_key=implementation_key,
            capability=capability(),
            schema_identity=identity(),
            factory=lambda selected_version: StructuralHandler(),
        )


def test_registries_are_isolated_and_finalization_prevents_order_dependence() -> None:
    first = registry_with_factory()
    second = HandlerRegistry()

    with pytest.raises(HandlerRegistryError):
        second.construct(
            implementation_key="qa.answer.local",
            capability=capability(),
            version=version(),
        )
    with pytest.raises(HandlerRegistryError):
        first.register_factory(
            implementation_key="qa.answer.other",
            capability=capability(identity_seed=11),
            schema_identity=CapabilitySchemaIdentity.from_capability(capability(identity_seed=11)),
            factory=lambda selected_version: StructuralHandler(),
        )


def test_resolver_selects_exact_scope_and_approved_deployment() -> None:
    selected_version = version()
    registry = registry_with_factory()
    catalog = InMemoryCatalog(
        bindings=(binding(),),
        versions={selected_version.id: selected_version},
        deployments=(deployment(),),
    )
    resolver = AgentBindingResolver(catalog=catalog, registry=registry)

    resolved = resolver.resolve(resolution_request())

    assert resolved.workspace_id == uid(1)
    assert resolved.environment is EnvironmentKind.DEVELOPMENT
    assert resolved.binding.agent_version_id == selected_version.id
    assert resolved.agent_version is selected_version
    assert resolved.implementation_key == "qa.answer.local"
    assert isinstance(resolved.handler, AgentHandler)


def test_resolver_does_not_infer_binding_priority_winner() -> None:
    selected_version = version()
    second_version = version(version_id=22)
    registry = HandlerRegistry()
    registry.register_factory(
        implementation_key="qa.answer.local",
        capability=capability(),
        schema_identity=identity(),
        factory=lambda selected_version: StructuralHandler(),
    )
    registry.finalize()
    catalog = InMemoryCatalog(
        bindings=(binding(priority=10), binding(binding_id=31, version_id=22, priority=20)),
        versions={selected_version.id: selected_version, second_version.id: second_version},
        deployments=(deployment(), deployment(deployment_id=41, version_id=22)),
    )

    with pytest.raises(BindingResolutionError, match="ambiguous"):
        AgentBindingResolver(catalog=catalog, registry=registry).resolve(resolution_request())


@pytest.mark.parametrize(
    ("catalog", "message"),
    (
        (InMemoryCatalog(), "binding"),
        (
            InMemoryCatalog(bindings=(binding(),), deployments=(deployment(),)),
            "version",
        ),
        (
            InMemoryCatalog(
                bindings=(binding(),),
                versions={uid(20): version()},
                deployments=(deployment(stage=DeploymentStage.DISABLED),),
            ),
            "deployment",
        ),
    ),
)
def test_resolver_rejects_missing_unknown_or_disabled_selection(
    catalog: InMemoryCatalog,
    message: str,
) -> None:
    resolver = AgentBindingResolver(catalog=catalog, registry=registry_with_factory())

    with pytest.raises(BindingResolutionError, match=message):
        resolver.resolve(resolution_request())


def test_resolver_rejects_zero_traffic_and_conflicting_deployments() -> None:
    selected_version = version()
    catalog = InMemoryCatalog(
        bindings=(binding(),),
        versions={selected_version.id: selected_version},
        deployments=(deployment(traffic_percentage=0),),
    )
    resolver = AgentBindingResolver(catalog=catalog, registry=registry_with_factory())

    with pytest.raises(BindingResolutionError, match="deployment"):
        resolver.resolve(resolution_request())

    catalog.deployments = (deployment(), deployment(deployment_id=41))
    with pytest.raises(BindingResolutionError, match="ambiguous"):
        resolver.resolve(resolution_request())


def test_resolver_rejects_unsupported_runtime_and_missing_capability() -> None:
    external_version = version(runtime=AgentRuntimeKind.EXTERNAL)
    catalog = InMemoryCatalog(
        bindings=(binding(),),
        versions={external_version.id: external_version},
        deployments=(deployment(),),
    )
    resolver = AgentBindingResolver(catalog=catalog, registry=registry_with_factory())
    with pytest.raises(BindingResolutionError, match="runtime"):
        resolver.resolve(resolution_request())

    unsupported = version(capabilities=("other_capability",))
    catalog.versions = {unsupported.id: unsupported}
    with pytest.raises(BindingResolutionError, match="capability"):
        resolver.resolve(resolution_request())


def test_resolver_rejects_unregistered_or_executable_implementation_key() -> None:
    selected_version = version(implementation_key="qa.answer.unknown")
    catalog = InMemoryCatalog(
        bindings=(binding(),),
        versions={selected_version.id: selected_version},
        deployments=(deployment(),),
    )
    resolver = AgentBindingResolver(catalog=catalog, registry=registry_with_factory())

    with pytest.raises(BindingResolutionError, match="implementation"):
        resolver.resolve(resolution_request())

    executable = version(implementation_key="module:handler")
    catalog.versions = {executable.id: executable}
    with pytest.raises(BindingResolutionError, match="implementation"):
        resolver.resolve(resolution_request())


def test_resolver_rejects_cross_scope_candidates_without_fallback() -> None:
    selected_version = version()
    foreign_workspace = binding(workspace_id=uid(99))
    foreign_environment = binding(
        binding_id=31,
        environment=EnvironmentKind.PRODUCTION,
    )
    catalog = InMemoryCatalog(
        bindings=(foreign_workspace, foreign_environment),
        versions={selected_version.id: selected_version},
        deployments=(deployment(),),
    )
    resolver = AgentBindingResolver(catalog=catalog, registry=registry_with_factory())

    with pytest.raises(BindingResolutionError, match="binding"):
        resolver.resolve(resolution_request())


def test_resolver_rejects_incompatible_deployment_policy() -> None:
    selected_version = version()
    catalog = InMemoryCatalog(
        bindings=(binding(),),
        versions={selected_version.id: selected_version},
        deployments=(deployment(),),
    )

    with pytest.raises(BindingResolutionError, match="deployment"):
        AgentBindingResolver(catalog=catalog, registry=registry_with_factory()).resolve(
            resolution_request(allowed_stages=frozenset({DeploymentStage.PRODUCTION}))
        )


def test_resolver_pins_resolution_against_later_catalog_mutation() -> None:
    selected_version = version()
    replacement = version(version_id=22, implementation_key="qa.answer.replacement")
    catalog = InMemoryCatalog(
        bindings=(binding(),),
        versions={selected_version.id: selected_version},
        deployments=(deployment(),),
    )
    resolved = AgentBindingResolver(catalog=catalog, registry=registry_with_factory()).resolve(
        resolution_request()
    )

    catalog.versions[selected_version.id] = replacement
    catalog.bindings = (binding(version_id=22),)

    assert resolved.agent_version is selected_version
    assert resolved.binding.agent_version_id == selected_version.id


def test_binding_resolution_requests_reject_zero_workspace_ids() -> None:
    with pytest.raises(ValueError):
        BindingResolutionRequest(
            workspace_id=UUID(int=0),
            environment=EnvironmentKind.DEVELOPMENT,
            capability=capability(),
            deployment_policy=DeploymentSelectionPolicy(
                allowed_stages=frozenset({DeploymentStage.DEVELOPMENT})
            ),
        )


def test_deployment_policy_rejects_disabled_only_policy() -> None:
    with pytest.raises(ValueError):
        DeploymentSelectionPolicy(allowed_stages=frozenset({DeploymentStage.DISABLED}))


def test_catalog_port_is_structural() -> None:
    assert isinstance(InMemoryCatalog(), AgentBindingCatalog)
