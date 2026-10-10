"""Trusted construction boundary for one local AgentExecutionContext.

The module consumes Issue #69 authorization contracts but does not issue them.
Production wiring must provide the Issue #72 verifier and an authoritative
scope validator. Deterministic structural fakes may implement those ports for
plain data-construction tests; only the infrastructure-owned composition gate
can consume an execution admission.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from spine.agents.runtime.contracts import (
    AgentExecutionContext,
    CapabilitySchemaIdentity,
    ExecutionIdentitySnapshot,
    TrustedToolGrantSet,
    _validate_runtime_config,
)
from spine.agents.runtime.ports import (
    CancellationToken,
    CanonicalTrustedAuthorizationContextVerifier,
    ExecutionScopeValidator,
    EventSink,
    TrustedAuthorizationContextVerifier,
)
from spine.application.diagnostics import DiagnosticContext
from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.auth.contracts import (
    AuthorizedRequestContext,
    AuthorizationResolution,
    AuthorizationRole,
    AuthorizationScopeRequirement,
    RequestedAuthorizationScope,
    RouteAuthorizationPolicy,
)
from spine.domain.agents import AgentRuntimeKind, AgentVersion
from spine.domain.capabilities import CapabilityDefinition
from spine.domain.common import EnvironmentKind

if TYPE_CHECKING:
    from spine.agents.catalog.resolver import ResolvedAgentBinding


class AgentExecutionContextConstructionError(ValueError):
    """Disclosure-safe construction failure."""


_ADMISSION_CONSTRUCTION_KEY = object()


class AgentExecutionContextAdmission:
    """A non-serializable, current admission for one exact plain context.

    The admission keeps the exact sealed authorization context outside
    ``AgentExecutionContext`` and re-runs the factory checks at consumption
    time. It is consumable only by the composition-owned gate that retains the
    same factory instance; copying or reconstructing the plain context does
    not produce a matching admission.
    """

    __slots__ = (
        "_authorized_context",
        "_context",
        "_factory",
        "_permitted_tool_grants",
        "_resolved_binding",
        "_resolution",
        "_service_principal_id",
    )

    def __init__(
        self,
        *,
        factory: AgentExecutionContextFactory,
        context: AgentExecutionContext,
        authorized_context: AuthorizedRequestContext[DiagnosticContext],
        resolution: AuthorizationResolution,
        resolved_binding: ResolvedAgentBinding,
        permitted_tool_grants: TrustedToolGrantSet,
        service_principal_id: UUID | None,
        _construction_key: object,
    ) -> None:
        if _construction_key is not _ADMISSION_CONSTRUCTION_KEY:
            raise TypeError("execution admission must come from the factory")
        self._factory = factory
        self._context = context
        self._authorized_context = authorized_context
        self._resolution = resolution
        self._resolved_binding = resolved_binding
        self._permitted_tool_grants = permitted_tool_grants
        self._service_principal_id = service_principal_id

    def verify(self, context: AgentExecutionContext) -> None:
        """Recheck current provenance and exact execution metadata."""

        if context is not self._context:
            raise ValueError("execution context admission does not match")
        candidate = self._factory.create(
            authorized_context=self._authorized_context,
            authorization_resolution=self._resolution,
            workspace_id=context.workspace_id,
            environment_id=context.environment_id,
            environment=context.environment,
            resolved_binding=self._resolved_binding,
            work_item_id=context.work_item_id,
            workflow_run_id=context.workflow_run_id,
            step_run_id=context.step_run_id,
            agent_run_id=context.agent_run_id,
            attempt_id=context.attempt_id,
            idempotency_key=context.idempotency_key,
            permitted_tool_grants=self._permitted_tool_grants,
            cancellation=context.cancellation,
            event_sink=context.event_sink,
            capability=context.capability,
            schema_identity=context.schema_identity,
            service_principal_id=self._service_principal_id,
            diagnostic_context=context.diagnostic_context,
            input_artifact_ids=context.input_artifact_ids,
            deadline=context.deadline,
            context_profile=context.context_profile,
        )
        if (
            candidate.model_dump(mode="python") != context.model_dump(mode="python")
            or candidate.cancellation is not context.cancellation
            or candidate.event_sink is not context.event_sink
        ):
            raise ValueError("execution context admission does not match")

    def verify_selection(
        self,
        context: AgentExecutionContext,
        resolved_binding: ResolvedAgentBinding,
        trusted_factory: AgentExecutionContextFactory,
    ) -> None:
        """Revalidate the exact pinned selection attached to this admission."""

        if (
            trusted_factory is not self._factory
            or resolved_binding is not self._resolved_binding
        ):
            raise ValueError("execution selection admission does not match")
        self.verify(context)

    def verify_request(
        self,
        request: Any,
        context: AgentExecutionContext,
    ) -> None:
        """Require a request to carry the admitted capability and schemas."""

        from spine.agents.runtime.contracts import AgentRequest

        if not isinstance(request, AgentRequest):
            raise ValueError("execution request is invalid")
        self.verify(context)
        if (
            request.capability != context.capability
            or request.schema_identity != context.schema_identity
        ):
            raise ValueError("execution request does not match pinned capability")


class AgentExecutionContextFactory:
    """Construct a detached context only after trusted boundary verification."""

    def __init__(
        self,
        *,
        authorization_verifier: TrustedAuthorizationContextVerifier | None,
        scope_validator: ExecutionScopeValidator | None,
        expected_route_policy: RouteAuthorizationPolicy | None,
    ) -> None:
        """Configure trusted ports and the server-selected agent route policy.

        A factory can construct plain context data for validation tests. An
        admission becomes executable only when consumed by the trusted
        composition runtime that retains this exact factory instance.
        """

        self._authorization_verifier = authorization_verifier
        self._scope_validator = scope_validator
        self._expected_route_policy = expected_route_policy
    def create(
        self,
        *,
        authorized_context: AuthorizedRequestContext[DiagnosticContext],
        authorization_resolution: AuthorizationResolution,
        workspace_id: UUID,
        environment_id: UUID,
        environment: EnvironmentKind,
        resolved_binding: ResolvedAgentBinding,
        work_item_id: UUID,
        workflow_run_id: UUID,
        step_run_id: UUID,
        agent_run_id: UUID,
        idempotency_key: str,
        permitted_tool_grants: TrustedToolGrantSet,
        cancellation: CancellationToken,
        event_sink: EventSink[Any],
        attempt_id: UUID | None = None,
        capability: CapabilityDefinition | None = None,
        schema_identity: CapabilitySchemaIdentity | None = None,
        agent_version: AgentVersion | None = None,
        service_principal_id: UUID | None = None,
        diagnostic_context: DiagnosticContext | None = None,
        input_artifact_ids: tuple[UUID, ...] = (),
        deadline: datetime | None = None,
        context_profile: str | None = None,
    ) -> AgentExecutionContext:
        """Create one immutable context without invoking the selected handler."""

        if (
            self._authorization_verifier is None
            or self._scope_validator is None
            or self._expected_route_policy is None
        ):
            raise AgentExecutionContextConstructionError(
                "execution context is unavailable."
            )

        try:
            if not isinstance(authorized_context, AuthorizedRequestContext):
                raise ValueError
            if type(authorization_resolution) is not AuthorizationResolution:
                raise ValueError
            if type(permitted_tool_grants) is not TrustedToolGrantSet:
                raise ValueError
            authorization_resolution = AuthorizationResolution.model_validate(
                authorization_resolution.model_dump(mode="python"), strict=True
            )
            permitted_tool_grants = TrustedToolGrantSet.model_validate(
                permitted_tool_grants.model_dump(mode="python"), strict=True
            )

            # The verifier is the only provenance check. The resolution is
            # subsequently compared as canonical, validated authorization data.
            self._authorization_verifier.verify(
                authorized_context,
                authorization_resolution,
            )
            scope = self._canonical_scope(authorized_context)
            route_policy = self._canonical_route_policy(authorized_context)
            expected_route_policy = self._canonical_route_policy_value(
                self._expected_route_policy
            )
            canonical_diagnostic_context = self._canonical_diagnostic_context(
                authorized_context
            )
            self._validate_authorization_consistency(
                authorized_context=authorized_context,
                resolution=authorization_resolution,
                scope=scope,
                route_policy=route_policy,
                expected_route_policy=expected_route_policy,
                workspace_id=workspace_id,
                environment_id=environment_id,
                service_principal_id=service_principal_id,
            )
            if (
                diagnostic_context is not None
                and diagnostic_context != canonical_diagnostic_context
            ):
                raise ValueError

            self._scope_validator.validate(
                scope=scope,
                environment=environment,
                authorization_generation=authorization_resolution.authorization_generation,
            )

            selected_capability, selected_schema, selected_version = (
                self._validate_binding(
                    resolved_binding=resolved_binding,
                    capability=capability,
                    schema_identity=schema_identity,
                    agent_version=agent_version,
                    workspace_id=workspace_id,
                    environment=environment,
                )
            )
            self._validate_grants(
                permitted_tool_grants=permitted_tool_grants,
                agent_version=selected_version,
            )
            if context_profile != selected_version.context_profile:
                raise ValueError
            selected_context_profile = selected_version.context_profile
            trusted_identity = ExecutionIdentitySnapshot(
                workspace_id=authorization_resolution.scope.workspace_id,
                environment_id=authorization_resolution.scope.environment_id,
                acting_subject_id=authorization_resolution.acting_subject_id,
                service_principal_id=route_policy.service_principal_id,
                authorization_generation=authorization_resolution.authorization_generation,
            )
            return AgentExecutionContext(
                workspace_id=workspace_id,
                environment_id=environment_id,
                environment=environment,
                trusted_identity=trusted_identity,
                work_item_id=work_item_id,
                workflow_run_id=workflow_run_id,
                step_run_id=step_run_id,
                agent_run_id=agent_run_id,
                attempt_id=attempt_id,
                capability=selected_capability,
                schema_identity=selected_schema,
                agent_version=selected_version,
                idempotency_key=idempotency_key,
                diagnostic_context=canonical_diagnostic_context,
                input_artifact_ids=input_artifact_ids,
                deadline=deadline,
                context_profile=selected_context_profile,
                permitted_tool_capabilities=permitted_tool_grants.grants,
                cancellation=cancellation,
                event_sink=event_sink,
            )
        except AgentExecutionContextConstructionError:
            raise
        except Exception:
            raise AgentExecutionContextConstructionError(
                "execution context is invalid."
            ) from None

    def admission_for(
        self,
        *,
        context: AgentExecutionContext,
        authorized_context: AuthorizedRequestContext[DiagnosticContext],
        authorization_resolution: AuthorizationResolution,
        resolved_binding: ResolvedAgentBinding,
        permitted_tool_grants: TrustedToolGrantSet,
        service_principal_id: UUID | None = None,
    ) -> AgentExecutionContextAdmission:
        """Bind current trusted authorization to one constructed context.

        The returned admission is deliberately separate from serializable
        context data and must be checked again by a registry gate bound to the
        same composition-owned authority at handler consumption time. Direct
        adapters and unbound registry selections fail closed.
        """

        if type(context) is not AgentExecutionContext:
            raise AgentExecutionContextConstructionError(
                "execution context is invalid."
            )
        if not isinstance(
            self._authorization_verifier,
            CanonicalTrustedAuthorizationContextVerifier,
        ):
            raise AgentExecutionContextConstructionError(
                "execution admission is unavailable."
            )
        admission = AgentExecutionContextAdmission(
            factory=self,
            context=context,
            authorized_context=authorized_context,
            resolution=authorization_resolution,
            resolved_binding=resolved_binding,
            permitted_tool_grants=permitted_tool_grants,
            service_principal_id=service_principal_id,
            _construction_key=_ADMISSION_CONSTRUCTION_KEY,
        )
        try:
            admission.verify(context)
        except Exception:
            raise AgentExecutionContextConstructionError(
                "execution context admission is invalid."
            ) from None
        return admission

    @staticmethod
    def _canonical_scope(
        authorized_context: AuthorizedRequestContext[DiagnosticContext],
    ) -> RequestedAuthorizationScope:
        scope = authorized_context.scope
        if type(scope) is not RequestedAuthorizationScope:
            raise ValueError
        return RequestedAuthorizationScope.model_validate(
            scope.model_dump(mode="python"), strict=True
        )

    @staticmethod
    def _canonical_route_policy(
        authorized_context: AuthorizedRequestContext[DiagnosticContext],
    ) -> RouteAuthorizationPolicy:
        policy = authorized_context.route_policy
        if type(policy) is not RouteAuthorizationPolicy:
            raise ValueError
        return RouteAuthorizationPolicy(
            required_roles=frozenset(policy.required_roles),
            required_scope=policy.required_scope,
            purpose=PersistencePurpose(policy.purpose.value),
            operation=PersistenceOperation(policy.operation.value),
            service_principal_id=policy.service_principal_id,
        )

    @staticmethod
    def _canonical_route_policy_value(
        policy: RouteAuthorizationPolicy,
    ) -> RouteAuthorizationPolicy:
        if type(policy) is not RouteAuthorizationPolicy:
            raise ValueError
        return RouteAuthorizationPolicy(
            required_roles=frozenset(policy.required_roles),
            required_scope=policy.required_scope,
            purpose=PersistencePurpose(policy.purpose.value),
            operation=PersistenceOperation(policy.operation.value),
            service_principal_id=policy.service_principal_id,
        )

    @staticmethod
    def _canonical_diagnostic_context(
        authorized_context: AuthorizedRequestContext[DiagnosticContext],
    ) -> DiagnosticContext:
        value = authorized_context.diagnostic_context
        if type(value) is not DiagnosticContext:
            raise ValueError
        return DiagnosticContext.model_validate(value.model_dump(mode="python"), strict=True)

    @staticmethod
    def _validate_authorization_consistency(
        *,
        authorized_context: AuthorizedRequestContext[DiagnosticContext],
        resolution: AuthorizationResolution,
        scope: RequestedAuthorizationScope,
        route_policy: RouteAuthorizationPolicy,
        expected_route_policy: RouteAuthorizationPolicy,
        workspace_id: UUID,
        environment_id: UUID,
        service_principal_id: UUID | None,
    ) -> None:
        if scope != resolution.scope:
            raise ValueError
        if scope.workspace_id != workspace_id or scope.environment_id != environment_id:
            raise ValueError
        if authorized_context.acting_subject_id != resolution.acting_subject_id:
            raise ValueError
        if authorized_context.roles != resolution.roles:
            raise ValueError
        if authorized_context.authorization_generation != resolution.authorization_generation:
            raise ValueError
        if (
            authorized_context.authentication_configuration_version
            != resolution.authentication_configuration_version
        ):
            raise ValueError
        if route_policy.required_scope is not AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT:
            raise ValueError
        if route_policy.required_roles != frozenset({AuthorizationRole.ADMINISTRATOR}):
            raise ValueError
        if route_policy != expected_route_policy:
            raise ValueError
        if (
            service_principal_id is not None
            and service_principal_id != route_policy.service_principal_id
        ):
            raise ValueError

    @staticmethod
    def _validate_binding(
        *,
        resolved_binding: ResolvedAgentBinding,
        capability: CapabilityDefinition | None,
        schema_identity: CapabilitySchemaIdentity | None,
        agent_version: AgentVersion | None,
        workspace_id: UUID,
        environment: EnvironmentKind,
    ) -> tuple[CapabilityDefinition, CapabilitySchemaIdentity, AgentVersion]:
        from spine.agents.catalog.resolver import ResolvedAgentBinding
        from spine.domain.agents import AgentBinding, AgentDeployment

        if not isinstance(resolved_binding, ResolvedAgentBinding):
            raise ValueError
        if not isinstance(resolved_binding.binding, AgentBinding):
            raise ValueError
        if not isinstance(resolved_binding.deployment, AgentDeployment):
            raise ValueError
        if type(resolved_binding.capability) is not CapabilityDefinition:
            raise ValueError
        if type(resolved_binding.schema_identity) is not CapabilitySchemaIdentity:
            raise ValueError
        if type(resolved_binding.agent_version) is not AgentVersion:
            raise ValueError
        selected_capability = resolved_binding.capability
        selected_schema = resolved_binding.schema_identity
        selected_version = resolved_binding.agent_version
        selected_capability = CapabilityDefinition.model_validate(
            selected_capability.model_dump(mode="python"), strict=True
        )
        selected_schema = CapabilitySchemaIdentity.model_validate(
            selected_schema.model_dump(mode="python"), strict=True
        )
        selected_version = AgentVersion.model_validate(
            selected_version.model_dump(mode="python"), strict=True
        )
        if capability is not None and capability != selected_capability:
            raise ValueError
        if schema_identity is not None and schema_identity != selected_schema:
            raise ValueError
        if agent_version is not None and agent_version != selected_version:
            raise ValueError
        if (
            resolved_binding.workspace_id != workspace_id
            or resolved_binding.binding.workspace_id != workspace_id
            or resolved_binding.deployment.workspace_id != workspace_id
            or resolved_binding.environment is not environment
            or resolved_binding.binding.environment is not environment
            or resolved_binding.deployment.environment is not environment
            or resolved_binding.binding.agent_version_id != selected_version.id
            or resolved_binding.deployment.agent_version_id != selected_version.id
            or resolved_binding.binding.capability != selected_capability.key
            or resolved_binding.registration.agent_version_id != selected_version.id
            or resolved_binding.registration.agent_id != selected_version.agent_id
            or resolved_binding.registration.runtime is not selected_version.runtime
            or resolved_binding.registration.capability != selected_capability
            or resolved_binding.registration.schema_identity != selected_schema
            or resolved_binding.implementation_key != resolved_binding.registration.implementation_key
        ):
            raise ValueError
        if selected_capability.key not in selected_version.capabilities:
            raise ValueError
        if selected_version.runtime is AgentRuntimeKind.EXTERNAL:
            raise ValueError
        if selected_schema != CapabilitySchemaIdentity.from_capability(selected_capability):
            raise ValueError
        _validate_runtime_config(selected_version.runtime_config)
        forbidden_runtime_keys = {
            "callable",
            "class",
            "handler",
            "implementation_key",
            "import_path",
            "module",
        }
        if forbidden_runtime_keys.intersection(resolved_binding.agent_version.runtime_config):
            raise ValueError
        return (
            selected_capability,
            selected_schema,
            selected_version.model_copy(update={"runtime_config": {}}),
        )

    @staticmethod
    def _validate_grants(
        *,
        permitted_tool_grants: TrustedToolGrantSet,
        agent_version: AgentVersion,
    ) -> None:
        version_grants = tuple(agent_version.tool_grants)
        if len(set(version_grants)) != len(version_grants):
            raise ValueError
        if not set(permitted_tool_grants.grants).issubset(version_grants):
            raise ValueError


__all__ = [
    "AgentExecutionContextConstructionError",
    "AgentExecutionContextAdmission",
    "AgentExecutionContextFactory",
]
