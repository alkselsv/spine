"""Trusted construction boundary for one local AgentExecutionContext.

The module consumes Issue #69 authorization contracts but does not issue them.
Production wiring must provide the Issue #72 verifier and an authoritative
scope validator.  Deterministic fakes may implement those ports in tests.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from spine.agents.runtime.contracts import (
    _VERIFIED_IDENTITY_TOKEN,
    AgentExecutionContext,
    CapabilitySchemaIdentity,
    ExecutionIdentitySnapshot,
    TrustedToolGrantSet,
)
from spine.agents.runtime.ports import (
    CancellationToken,
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


class AgentExecutionContextFactory:
    """Construct a detached context only after trusted boundary verification."""

    def __init__(
        self,
        *,
        authorization_verifier: TrustedAuthorizationContextVerifier | None,
        scope_validator: ExecutionScopeValidator | None,
    ) -> None:
        self._authorization_verifier = authorization_verifier
        self._scope_validator = scope_validator

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

        if self._authorization_verifier is None or self._scope_validator is None:
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
            canonical_diagnostic_context = self._canonical_diagnostic_context(
                authorized_context
            )
            self._validate_authorization_consistency(
                authorized_context=authorized_context,
                resolution=authorization_resolution,
                scope=scope,
                route_policy=route_policy,
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
            if context_profile is not None and (
                selected_version.context_profile is not None
                and context_profile != selected_version.context_profile
            ):
                raise ValueError
            selected_context_profile = (
                selected_version.context_profile
                if context_profile is None
                else context_profile
            )
            trusted_identity = ExecutionIdentitySnapshot._from_verified_authorization(
                _issuance_token=_VERIFIED_IDENTITY_TOKEN,
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
        return selected_capability, selected_schema, selected_version

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
    "AgentExecutionContextFactory",
]
