from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from spine.agents.catalog import HandlerRegistry, HandlerRegistryError
from spine.agents.runtime import (
    AgentExecutionContext,
    AgentExecutionContextFactory,
    AgentRequest,
    TrustedToolGrantSet,
    adapt_async_function,
)
from spine.auth import AuthorizationResolution
from spine.domain.common import EnvironmentKind
from spine.infrastructure.auth import (
    RequestAgentAuthorizationVerifier,
    compose_agent_execution_runtime,
)

from tests.agents.test_async_adapter import InputModel, OutputModel, request
from tests.agents.test_execution_context_construction import (
    FakeCancellation,
    FakeEventSink,
    FakeScopeValidator,
    NominalNoopVerifier,
    resolved_binding,
    uid,
)
from tests.infrastructure.auth.test_request_authorization_composition import (
    composition,
    credentials,
    diagnostic_context,
    scope,
)


class FixedExecutionScope:
    def __init__(self, *, generation: int, workspace_id, environment_id) -> None:
        self.generation = generation
        self.workspace_id = workspace_id
        self.environment_id = environment_id

    def validate(self, *, scope, environment, authorization_generation) -> None:
        if (
            scope.workspace_id != self.workspace_id
            or scope.environment_id != self.environment_id
            or environment is not EnvironmentKind.PRODUCTION
            or authorization_generation != self.generation
        ):
            raise ValueError("execution scope is invalid")


@pytest.mark.asyncio
async def test_agent_verifier_rechecks_real_issue72_request_lifecycle() -> None:
    composer, _, request_boundary, _ = await composition()

    async with composer.bind_route("knowledge.sources.list").authorize(
        credentials=credentials(),
        requested_scope=scope(),
        diagnostic_context=diagnostic_context(),
    ) as authorized:
        request_context = authorized.request_context
        resolution = AuthorizationResolution(
            acting_subject_id=request_context.acting_subject_id,
            scope=request_context.scope,
            roles=request_context.roles,
            authorization_generation=request_context.authorization_generation,
            authentication_configuration_version=(
                request_context.authentication_configuration_version
            ),
        )
        verifier = RequestAgentAuthorizationVerifier(
            boundary=request_boundary,
            authorized=authorized,
        )

        verifier.verify(request_context, resolution)

        async def use_in_child_task() -> None:
            verifier.verify(request_context, resolution)

        with pytest.raises(ValueError, match="authorization context is invalid"):
            await asyncio.create_task(use_in_child_task())

    with pytest.raises(ValueError, match="authorization context is invalid"):
        verifier.verify(request_context, resolution)


@pytest.mark.asyncio
async def test_real_issue72_composition_binds_the_execution_gate_factory() -> None:
    composer, _, request_boundary, _ = await composition()

    async with composer.bind_route("knowledge.sources.list").authorize(
        credentials=credentials(),
        requested_scope=scope(),
        diagnostic_context=diagnostic_context(),
    ) as authorized:
        request_context = authorized.request_context
        resolution = AuthorizationResolution(
            acting_subject_id=request_context.acting_subject_id,
            scope=request_context.scope,
            roles=request_context.roles,
            authorization_generation=request_context.authorization_generation,
            authentication_configuration_version=(
                request_context.authentication_configuration_version
            ),
        )
        registry = HandlerRegistry()
        runtime = compose_agent_execution_runtime(
            boundary=request_boundary,
            authorized=authorized,
            scope_validator=FixedExecutionScope(
                generation=resolution.authorization_generation,
                workspace_id=resolution.scope.workspace_id,
                environment_id=resolution.scope.environment_id,
            ),
            expected_route_policy=request_context.route_policy,
            registry=registry,
        )
        factory = runtime.factory

        selected = resolved_binding(
            selected_workspace_id=resolution.scope.workspace_id,
            selected_environment=EnvironmentKind.PRODUCTION,
        )
        calls: list[tuple[object, AgentExecutionContext]] = []

        async def handler(
            received_request: AgentRequest[InputModel],
            received_context: AgentExecutionContext,
        ) -> OutputModel:
            calls.append((received_request, received_context))
            return OutputModel(answer="ok")

        registry.register_handler(
            registration=selected.registration,
            handler=adapt_async_function(handler),
        )
        registry.finalize()
        registered = registry.construct(
            capability=selected.capability,
            version=selected.agent_version,
        )
        selected = replace(selected, handler=registered)
        context = factory.create(
            authorized_context=request_context,
            authorization_resolution=resolution,
            workspace_id=resolution.scope.workspace_id,
            environment_id=resolution.scope.environment_id,
            environment=EnvironmentKind.PRODUCTION,
            resolved_binding=selected,
            work_item_id=uid(50),
            workflow_run_id=uid(51),
            step_run_id=uid(52),
            agent_run_id=uid(53),
            idempotency_key="agent:50:1",
            permitted_tool_grants=TrustedToolGrantSet(grants=("documents.read",)),
            cancellation=FakeCancellation(),
            event_sink=FakeEventSink(),
        )
        admission = factory.admission_for(
            context=context,
            authorized_context=request_context,
            authorization_resolution=resolution,
            resolved_binding=selected,
            permitted_tool_grants=TrustedToolGrantSet(grants=("documents.read",)),
        )
        guarded = runtime.bind_for_execution(
            handler=registered,
            context=context,
            admission=admission,
            resolved_binding=selected,
        )

        result = await guarded.invoke(request(), context)

        assert result.output == OutputModel(answer="ok")
        assert len(calls) == 1
        assert calls[0][0].capability == context.capability
        assert calls[0][1] is context


@pytest.mark.asyncio
async def test_nominal_noop_factory_cannot_bind_to_real_execution_gate() -> None:
    composer, _, request_boundary, _ = await composition()

    async with composer.bind_route("knowledge.sources.list").authorize(
        credentials=credentials(),
        requested_scope=scope(),
        diagnostic_context=diagnostic_context(),
    ) as authorized:
        request_context = authorized.request_context
        resolution = AuthorizationResolution(
            acting_subject_id=request_context.acting_subject_id,
            scope=request_context.scope,
            roles=request_context.roles,
            authorization_generation=request_context.authorization_generation,
            authentication_configuration_version=(
                request_context.authentication_configuration_version
            ),
        )
        registry = HandlerRegistry()
        runtime = compose_agent_execution_runtime(
            boundary=request_boundary,
            authorized=authorized,
            scope_validator=FixedExecutionScope(
                generation=resolution.authorization_generation,
                workspace_id=resolution.scope.workspace_id,
                environment_id=resolution.scope.environment_id,
            ),
            expected_route_policy=request_context.route_policy,
            registry=registry,
        )
        selected = resolved_binding(
            selected_workspace_id=resolution.scope.workspace_id,
            selected_environment=EnvironmentKind.PRODUCTION,
        )
        calls = 0

        async def handler(
            received_request: AgentRequest[InputModel],
            received_context: AgentExecutionContext,
        ) -> OutputModel:
            nonlocal calls
            del received_request, received_context
            calls += 1
            return OutputModel(answer="must not run")

        registry.register_handler(
            registration=selected.registration,
            handler=adapt_async_function(handler),
        )
        registry.finalize()
        registered = registry.construct(
            capability=selected.capability,
            version=selected.agent_version,
        )
        selected = replace(selected, handler=registered)

        fake_factory = AgentExecutionContextFactory(
            authorization_verifier=NominalNoopVerifier(),
            scope_validator=FakeScopeValidator(
                environment_kinds={
                    (resolution.scope.workspace_id, resolution.scope.environment_id): (
                        EnvironmentKind.PRODUCTION
                    )
                },
                expected_generation=resolution.authorization_generation,
            ),
            expected_route_policy=request_context.route_policy,
        )
        context = fake_factory.create(
            authorized_context=request_context,
            authorization_resolution=resolution,
            workspace_id=resolution.scope.workspace_id,
            environment_id=resolution.scope.environment_id,
            environment=EnvironmentKind.PRODUCTION,
            resolved_binding=selected,
            work_item_id=uid(50),
            workflow_run_id=uid(51),
            step_run_id=uid(52),
            agent_run_id=uid(53),
            idempotency_key="agent:50:1",
            permitted_tool_grants=TrustedToolGrantSet(grants=("documents.read",)),
            cancellation=FakeCancellation(),
            event_sink=FakeEventSink(),
        )
        admission = fake_factory.admission_for(
            context=context,
            authorized_context=request_context,
            authorization_resolution=resolution,
            resolved_binding=selected,
            permitted_tool_grants=TrustedToolGrantSet(grants=("documents.read",)),
        )

        with pytest.raises(HandlerRegistryError, match="admission"):
            guarded = runtime.bind_for_execution(
                handler=registered,
                context=context,
                admission=admission,
                resolved_binding=selected,
            )
            await guarded.invoke(request(), context)

        assert calls == 0


@pytest.mark.asyncio
async def test_foreign_registry_handler_cannot_bind_to_trusted_runtime() -> None:
    composer, _, request_boundary, _ = await composition()

    async with composer.bind_route("knowledge.sources.list").authorize(
        credentials=credentials(),
        requested_scope=scope(),
        diagnostic_context=diagnostic_context(),
    ) as authorized:
        request_context = authorized.request_context
        resolution = AuthorizationResolution(
            acting_subject_id=request_context.acting_subject_id,
            scope=request_context.scope,
            roles=request_context.roles,
            authorization_generation=request_context.authorization_generation,
            authentication_configuration_version=(
                request_context.authentication_configuration_version
            ),
        )
        registry_a = HandlerRegistry()
        registry_b = HandlerRegistry()
        runtime = compose_agent_execution_runtime(
            boundary=request_boundary,
            authorized=authorized,
            scope_validator=FixedExecutionScope(
                generation=resolution.authorization_generation,
                workspace_id=resolution.scope.workspace_id,
                environment_id=resolution.scope.environment_id,
            ),
            expected_route_policy=request_context.route_policy,
            registry=registry_a,
        )
        selected = resolved_binding(
            selected_workspace_id=resolution.scope.workspace_id,
            selected_environment=EnvironmentKind.PRODUCTION,
        )
        calls: list[str] = []

        async def handler_a(
            received_request: AgentRequest[InputModel],
            received_context: AgentExecutionContext,
        ) -> OutputModel:
            del received_request, received_context
            calls.append("A")
            return OutputModel(answer="A")

        async def handler_b(
            received_request: AgentRequest[InputModel],
            received_context: AgentExecutionContext,
        ) -> OutputModel:
            del received_request, received_context
            calls.append("B")
            return OutputModel(answer="B")

        registry_a.register_handler(
            registration=selected.registration,
            handler=adapt_async_function(handler_a),
        )
        registry_b.register_handler(
            registration=selected.registration,
            handler=adapt_async_function(handler_b),
        )
        registry_a.finalize()
        registry_b.finalize()
        registered_a = registry_a.construct(
            capability=selected.capability,
            version=selected.agent_version,
        )
        registered_b = registry_b.construct(
            capability=selected.capability,
            version=selected.agent_version,
        )
        del registered_a
        foreign_binding = replace(selected, handler=registered_b)
        context = runtime.factory.create(
            authorized_context=request_context,
            authorization_resolution=resolution,
            workspace_id=resolution.scope.workspace_id,
            environment_id=resolution.scope.environment_id,
            environment=EnvironmentKind.PRODUCTION,
            resolved_binding=foreign_binding,
            work_item_id=uid(60),
            workflow_run_id=uid(61),
            step_run_id=uid(62),
            agent_run_id=uid(63),
            idempotency_key="agent:60:1",
            permitted_tool_grants=TrustedToolGrantSet(grants=("documents.read",)),
            cancellation=FakeCancellation(),
            event_sink=FakeEventSink(),
        )
        admission = runtime.factory.admission_for(
            context=context,
            authorized_context=request_context,
            authorization_resolution=resolution,
            resolved_binding=foreign_binding,
            permitted_tool_grants=TrustedToolGrantSet(grants=("documents.read",)),
        )

        with pytest.raises(HandlerRegistryError, match="registry"):
            guarded = runtime.bind_for_execution(
                handler=registered_b,
                context=context,
                admission=admission,
                resolved_binding=foreign_binding,
            )
            await guarded.invoke(request(), context)

        assert calls == []
