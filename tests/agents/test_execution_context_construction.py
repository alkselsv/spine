from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.agents.catalog import AgentImplementationRegistration, ResolvedAgentBinding
from spine.agents.runtime import (
    AgentExecutionContext,
    AgentExecutionContextFactory,
    CapabilitySchemaIdentity,
    TrustedToolGrantSet,
)
from spine.application.diagnostics import DiagnosticContext
from spine.auth import (
    AuthorizedRequestContext,
    AuthorizationResolution,
    AuthorizationRole,
    AuthorizationScopeRequirement,
    RequestedAuthorizationScope,
    RouteAuthorizationPolicy,
)
from spine.application.persistence import PersistenceOperation, PersistencePurpose
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


class FakeCancellation:
    is_cancelled = False

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise RuntimeError("cancelled")


class FakeEventSink:
    async def emit(self, event: object) -> None:
        del event


@dataclass
class FakeScopeValidator:
    expected_workspace_id: UUID
    expected_environment_id: UUID
    expected_environment: EnvironmentKind
    expected_generation: int

    def validate(
        self,
        *,
        scope: RequestedAuthorizationScope,
        environment: EnvironmentKind,
        authorization_generation: int,
    ) -> None:
        if (
            scope.workspace_id != self.expected_workspace_id
            or scope.environment_id != self.expected_environment_id
            or environment is not self.expected_environment
            or authorization_generation != self.expected_generation
        ):
            raise ValueError("execution scope is invalid")


class FakeVerifier:
    def __init__(self, expected_context: AuthorizedRequestContext[DiagnosticContext]) -> None:
        self.expected_context = expected_context
        self.calls = 0

    def verify(
        self,
        context: AuthorizedRequestContext[DiagnosticContext],
        resolution: AuthorizationResolution,
    ) -> None:
        self.calls += 1
        if context is not self.expected_context:
            raise ValueError("authorization context is invalid")
        if resolution.scope != context.scope:
            raise ValueError("authorization context is invalid")


class Handler:
    async def invoke(self, request: object, context: AgentExecutionContext) -> object:
        del request, context
        raise AssertionError("construction must not invoke a handler")


def capability() -> CapabilityDefinition:
    return CapabilityDefinition(
        id=uid(10),
        key="answer_question",
        display_name="Answer question",
        input_schema=SchemaRef(name="answer_question.input", version="1"),
        output_schema=SchemaRef(name="answer_question.output", version="1"),
    )


def version(*, workspace_id: UUID = uid(1)) -> AgentVersion:
    del workspace_id
    return AgentVersion(
        id=uid(20),
        agent_id=uid(21),
        version="2026.10.1",
        runtime=AgentRuntimeKind.CODE,
        capabilities=("answer_question",),
        tool_grants=("documents.read", "documents.search"),
    )


def resolved_binding(
    *,
    selected_workspace_id: UUID = uid(1),
    selected_environment: EnvironmentKind = EnvironmentKind.DEVELOPMENT,
    selected_capability: CapabilityDefinition | None = None,
    selected_version: AgentVersion | None = None,
) -> ResolvedAgentBinding:
    selected_capability = selected_capability or capability()
    selected_version = selected_version or version()
    schema_identity = CapabilitySchemaIdentity.from_capability(selected_capability)
    binding = AgentBinding(
        id=uid(30),
        workspace_id=selected_workspace_id,
        environment=selected_environment,
        capability=selected_capability.key,
        agent_version_id=selected_version.id,
    )
    deployment = AgentDeployment(
        id=uid(31),
        workspace_id=selected_workspace_id,
        environment=selected_environment,
        agent_version_id=selected_version.id,
        stage=DeploymentStage.DEVELOPMENT,
    )
    return ResolvedAgentBinding(
        workspace_id=selected_workspace_id,
        environment=selected_environment,
        capability=selected_capability,
        schema_identity=schema_identity,
        binding=binding,
        deployment=deployment,
        agent_version=selected_version,
        registration=AgentImplementationRegistration.from_version(
            version=selected_version,
            capability=selected_capability,
            implementation_key="qa.answer.local",
        ),
        implementation_key="qa.answer.local",
        handler=Handler(),  # type: ignore[arg-type]
    )


def authorized_context(
    *,
    workspace_id: UUID = uid(1),
    environment_id: UUID = uid(2),
    acting_subject_id: UUID = uid(3),
    generation: int = 7,
) -> tuple[MagicMock, AuthorizationResolution]:
    scope = RequestedAuthorizationScope(
        workspace_id=workspace_id,
        environment_id=environment_id,
    )
    resolution = AuthorizationResolution(
        acting_subject_id=acting_subject_id,
        scope=scope,
        roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        authorization_generation=generation,
        authentication_configuration_version="oidc-2026-10",
    )
    context = MagicMock(spec=AuthorizedRequestContext)
    context.acting_subject_id = acting_subject_id
    context.scope = scope
    context.roles = resolution.roles
    context.authorization_generation = generation
    context.authentication_configuration_version = (
        resolution.authentication_configuration_version
    )
    context.route_policy = RouteAuthorizationPolicy(
        required_roles=resolution.roles,
        required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
        purpose=PersistencePurpose("agent_execution"),
        operation=PersistenceOperation("invoke_agent"),
    )
    context.diagnostic_context = DiagnosticContext(
        trace_id=uid(40),
        correlation_id=uid(41),
        causation_id=uid(42),
    )
    return context, resolution


def make_context(
    *,
    workspace_id: UUID = uid(1),
    environment_id: UUID = uid(2),
    environment: EnvironmentKind = EnvironmentKind.DEVELOPMENT,
    selected_binding: ResolvedAgentBinding | None = None,
    resolution: AuthorizationResolution | None = None,
    authorized: MagicMock | None = None,
    verifier: FakeVerifier | None = None,
    grants: TrustedToolGrantSet | None = None,
    **overrides: object,
) -> AgentExecutionContext:
    if authorized is None or resolution is None:
        authorized, resolution = authorized_context(
            workspace_id=workspace_id,
            environment_id=environment_id,
        )
    verifier = verifier or FakeVerifier(authorized)
    values: dict[str, object] = {
        "authorized_context": authorized,
        "authorization_resolution": resolution,
        "workspace_id": workspace_id,
        "environment_id": environment_id,
        "environment": environment,
        "resolved_binding": selected_binding or resolved_binding(),
        "work_item_id": uid(50),
        "workflow_run_id": uid(51),
        "step_run_id": uid(52),
        "agent_run_id": uid(53),
        "idempotency_key": "agent:50:1",
        "input_artifact_ids": (uid(60),),
        "permitted_tool_grants": grants
        or TrustedToolGrantSet(grants=("documents.read",)),
        "cancellation": FakeCancellation(),
        "event_sink": FakeEventSink(),
    }
    values.update(overrides)
    return AgentExecutionContextFactory(
        authorization_verifier=verifier,
        scope_validator=FakeScopeValidator(
            expected_workspace_id=workspace_id,
            expected_environment_id=environment_id,
            expected_environment=environment,
            expected_generation=resolution.authorization_generation,
        ),
    ).create(**values)


def test_factory_constructs_detached_context_for_each_authorized_scope() -> None:
    first = make_context()
    second = make_context(
        workspace_id=uid(101),
        environment_id=uid(102),
        selected_binding=resolved_binding(selected_workspace_id=uid(101)),
    )

    assert first.workspace_id == uid(1)
    assert first.environment_id == uid(2)
    assert first.trusted_identity.acting_subject_id == uid(3)
    assert second.workspace_id == uid(101)
    assert second.environment_id == uid(102)
    assert first is not second


@pytest.mark.parametrize(
    "change",
    (
        {"workspace_id": uid(101)},
        {"environment": EnvironmentKind.PRODUCTION},
    ),
)
def test_factory_rejects_scope_substitution_without_disclosure(change: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="execution context is invalid"):
        make_context(**change)


def test_factory_rejects_cross_environment_substitution_against_fixed_authorization() -> None:
    authorized, resolution = authorized_context(environment_id=uid(2))

    with pytest.raises(ValueError, match="execution context is invalid"):
        make_context(
            environment_id=uid(102),
            authorized=authorized,
            resolution=resolution,
            selected_binding=resolved_binding(),
        )


def test_factory_rejects_forged_verifier_input_and_generation_mismatch() -> None:
    authorized, resolution = authorized_context()
    forged = MagicMock(spec=AuthorizedRequestContext)
    verifier = FakeVerifier(authorized)

    with pytest.raises(ValueError, match="execution context is invalid"):
        make_context(authorized=forged, resolution=resolution, verifier=verifier)

    stale_authorized, current_resolution = authorized_context()
    stale_resolution = current_resolution.model_copy(
        update={"authorization_generation": 8}
    )
    with pytest.raises(ValueError, match="execution context is invalid"):
        make_context(
            authorized=stale_authorized,
            resolution=stale_resolution,
            verifier=FakeVerifier(stale_authorized),
        )


def test_factory_requires_trusted_verification_and_scope_authority() -> None:
    authorized, resolution = authorized_context()

    with pytest.raises(ValueError, match="execution context is unavailable"):
        AgentExecutionContextFactory(
            authorization_verifier=None,
            scope_validator=FakeScopeValidator(uid(1), uid(2), EnvironmentKind.DEVELOPMENT, 7),
        ).create(
            authorized_context=authorized,
            authorization_resolution=resolution,
            workspace_id=uid(1),
            environment_id=uid(2),
            environment=EnvironmentKind.DEVELOPMENT,
            resolved_binding=resolved_binding(),
            work_item_id=uid(50),
            workflow_run_id=uid(51),
            step_run_id=uid(52),
            agent_run_id=uid(53),
            idempotency_key="agent:50:1",
            permitted_tool_grants=TrustedToolGrantSet(grants=("documents.read",)),
            cancellation=FakeCancellation(),
            event_sink=FakeEventSink(),
        )


def test_authorization_resolution_alone_cannot_mint_context() -> None:
    _, resolution = authorized_context()

    with pytest.raises(ValueError, match="execution context is unavailable"):
        AgentExecutionContextFactory(
            authorization_verifier=None,
            scope_validator=None,
        ).create(
            authorized_context=object(),  # type: ignore[arg-type]
            authorization_resolution=resolution,
            workspace_id=uid(1),
            environment_id=uid(2),
            environment=EnvironmentKind.DEVELOPMENT,
            resolved_binding=resolved_binding(),
            work_item_id=uid(50),
            workflow_run_id=uid(51),
            step_run_id=uid(52),
            agent_run_id=uid(53),
            idempotency_key="agent:50:1",
            permitted_tool_grants=TrustedToolGrantSet(grants=("documents.read",)),
            cancellation=FakeCancellation(),
            event_sink=FakeEventSink(),
        )


def test_factory_rejects_version_capability_and_schema_substitution() -> None:
    mismatched_version = version().model_copy(update={"capabilities": ("other",)})
    mismatched_capability = capability().model_copy(update={"key": "other"})

    with pytest.raises(ValueError, match="execution context is invalid"):
        make_context(selected_binding=resolved_binding(selected_version=mismatched_version))
    with pytest.raises(ValueError, match="execution context is invalid"):
        make_context(selected_binding=resolved_binding(selected_capability=mismatched_capability))


def test_factory_rejects_grant_expansion_duplicates_and_malformed_grants() -> None:
    with pytest.raises(ValidationError):
        TrustedToolGrantSet(grants=("documents.read", "documents.read"))
    with pytest.raises(ValidationError):
        TrustedToolGrantSet(grants=("documents.read", "Bearer secret"))
    with pytest.raises(ValueError, match="execution context is invalid"):
        make_context(grants=TrustedToolGrantSet(grants=("documents.write",)))


def test_factory_preserves_diagnostics_lineage_deadline_and_live_ports() -> None:
    cancellation = FakeCancellation()
    event_sink = FakeEventSink()
    deadline = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)

    context = make_context(
        deadline=deadline,
        cancellation=cancellation,
        event_sink=event_sink,
    )

    assert context.diagnostic_context == DiagnosticContext(
        trace_id=uid(40), correlation_id=uid(41), causation_id=uid(42)
    )
    assert context.input_artifact_ids == (uid(60),)
    assert context.deadline == deadline
    assert context.cancellation is cancellation
    assert context.event_sink is event_sink
    assert "Bearer" not in context.model_dump_json()
    assert "cancellation" not in context.model_dump()
    with pytest.raises(ValidationError):
        AgentExecutionContext.model_validate(context.model_dump())


def test_context_serialization_excludes_runtime_configuration_and_credentials() -> None:
    selected_version = version().model_copy(
        update={"runtime_config": {"access_token": "Bearer secret"}}
    )

    with pytest.raises(ValueError, match="execution context is invalid"):
        make_context(
            selected_binding=resolved_binding(selected_version=selected_version),
        )


def test_factory_rejects_naive_deadline_zero_lineage_and_bad_diagnostics() -> None:
    with pytest.raises(ValueError, match="execution context is invalid"):
        make_context(deadline=datetime(2026, 10, 11, 12, 0))
    with pytest.raises(ValueError, match="execution context is invalid"):
        make_context(input_artifact_ids=(UUID(int=0),))
    with pytest.raises(ValidationError):
        make_context(diagnostic_context=DiagnosticContext(trace_id=UUID(int=0)))
