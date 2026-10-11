from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

from spine.agents.catalog import HandlerRegistry
from spine.agents.runtime import (
    AgentExecutionContext,
    AgentHandler,
    AgentRequest,
    TrustedToolGrantSet,
    adapt_async_function,
)
from spine.auth import AuthorizationResolution
from spine.domain.common import EnvironmentKind
from spine.infrastructure.auth import compose_agent_execution_runtime

from tests.agents.test_execution_context_construction import (
    FakeCancellation,
    FakeEventSink,
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
    def __init__(self, *, generation: int, workspace_id: Any, environment_id: Any) -> None:
        self.generation = generation
        self.workspace_id = workspace_id
        self.environment_id = environment_id

    def validate(self, *, scope: Any, environment: EnvironmentKind, authorization_generation: int) -> None:
        if (
            scope.workspace_id != self.workspace_id
            or scope.environment_id != self.environment_id
            or environment is not EnvironmentKind.PRODUCTION
            or authorization_generation != self.generation
        ):
            raise ValueError("execution scope is invalid")


@asynccontextmanager
async def bound_adapter(
    function: Callable[..., Awaitable[Any]],
) -> AsyncIterator[tuple[AgentHandler[Any, Any], AgentExecutionContext]]:
    """Yield an adapter admitted through the public production composition seam."""

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
        selected = resolved_binding(
            selected_workspace_id=resolution.scope.workspace_id,
            selected_environment=EnvironmentKind.PRODUCTION,
        )
        registry = HandlerRegistry()
        registry.register_handler(
            registration=selected.registration,
            handler=adapt_async_function(function),
        )
        registry.finalize()
        registered = registry.construct(
            capability=selected.capability,
            version=selected.agent_version,
        )
        selected = replace(selected, handler=registered)
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
        context = runtime.factory.create(
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
        admission = runtime.factory.admission_for(
            context=context,
            authorized_context=request_context,
            authorization_resolution=resolution,
            resolved_binding=selected,
            permitted_tool_grants=TrustedToolGrantSet(grants=("documents.read",)),
        )
        yield (
            runtime.bind_for_execution(
                handler=registered,
                context=context,
                admission=admission,
                resolved_binding=selected,
            ),
            context,
        )
