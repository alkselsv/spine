"""Fail-closed resolution of approved local Agent Bindings."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from spine.agents.catalog.ports import AgentBindingCatalog
from spine.agents.catalog.registry import HandlerRegistry, HandlerRegistryError
from spine.agents.runtime import AgentHandler, CapabilitySchemaIdentity
from spine.domain.agents import (
    AgentBinding,
    AgentDeployment,
    AgentRuntimeKind,
    AgentVersion,
    DeploymentStage,
)
from spine.domain.capabilities import CapabilityDefinition
from spine.domain.common import EnvironmentKind


class BindingResolutionError(ValueError):
    """A deterministic, disclosure-safe binding resolution failure."""


@dataclass(frozen=True, slots=True)
class DeploymentSelectionPolicy:
    """The approved deployment stages accepted by one resolution request."""

    allowed_stages: frozenset[DeploymentStage]

    def __post_init__(self) -> None:
        stages = frozenset(self.allowed_stages)
        if not stages or any(not isinstance(stage, DeploymentStage) for stage in stages):
            raise ValueError("deployment policy is invalid")
        if DeploymentStage.DISABLED in stages:
            raise ValueError("deployment policy is invalid")
        object.__setattr__(self, "allowed_stages", stages)


@dataclass(frozen=True, slots=True)
class BindingResolutionRequest:
    """Exact scope and capability requested by a composition boundary."""

    workspace_id: UUID
    environment: EnvironmentKind
    capability: CapabilityDefinition
    deployment_policy: DeploymentSelectionPolicy

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, UUID) or self.workspace_id.int == 0:
            raise ValueError("binding resolution workspace is invalid")
        if not isinstance(self.environment, EnvironmentKind):
            raise ValueError("binding resolution environment is invalid")
        if not isinstance(self.capability, CapabilityDefinition):
            raise ValueError("binding resolution capability is invalid")
        if not isinstance(self.deployment_policy, DeploymentSelectionPolicy):
            raise ValueError("binding resolution policy is invalid")


@dataclass(frozen=True, slots=True)
class ResolvedAgentBinding:
    """One immutable, pinned local selection for subsequent runtime execution."""

    workspace_id: UUID
    environment: EnvironmentKind
    capability: CapabilityDefinition
    schema_identity: CapabilitySchemaIdentity
    binding: AgentBinding
    deployment: AgentDeployment
    agent_version: AgentVersion
    implementation_key: str
    handler: AgentHandler[Any, Any]


class AgentBindingResolver:
    """Resolve catalog data without invocation, retries, persistence, or fallback.

    A positive deployment traffic value makes a deployment eligible; this seam
    does not perform traffic routing. The current domain model does not define
    an authoritative ordering for binding priorities, so multiple eligible
    candidates fail closed as ambiguous.
    """

    def __init__(self, *, catalog: AgentBindingCatalog, registry: HandlerRegistry) -> None:
        self._catalog = catalog
        self._registry = registry

    def resolve(self, request: BindingResolutionRequest) -> ResolvedAgentBinding:
        bindings = self._read(
            lambda: self._catalog.list_bindings(
                request.workspace_id,
                request.environment,
                request.capability.key,
            ),
            "local agent catalog is unavailable",
        )
        candidates = tuple(
            item
            for item in bindings
            if isinstance(item, AgentBinding)
            and item.workspace_id == request.workspace_id
            and item.environment is request.environment
            and item.capability == request.capability.key
        )
        if not candidates:
            raise BindingResolutionError("local agent binding is missing")

        eligible: list[tuple[AgentBinding, AgentDeployment, AgentVersion]] = []
        for candidate in candidates:
            deployments = self._read(
                lambda candidate=candidate: self._catalog.list_deployments(
                    request.workspace_id,
                    request.environment,
                    candidate.agent_version_id,
                ),
                "local agent catalog is unavailable",
            )
            approved = tuple(
                item
                for item in deployments
                if isinstance(item, AgentDeployment)
                and item.workspace_id == request.workspace_id
                and item.environment is request.environment
                and item.agent_version_id == candidate.agent_version_id
                and item.stage in request.deployment_policy.allowed_stages
                and item.stage is not DeploymentStage.DISABLED
                and item.traffic_percentage > 0
            )
            if len(approved) > 1:
                raise BindingResolutionError("local agent binding selection is ambiguous")
            if not approved:
                continue
            version = self._read_value(
                lambda candidate=candidate: self._catalog.get_agent_version(
                    candidate.agent_version_id
                ),
                "local agent catalog is unavailable",
            )
            if not isinstance(version, AgentVersion):
                raise BindingResolutionError("local agent version is unknown")
            eligible.append((candidate, approved[0], version))

        if not eligible:
            raise BindingResolutionError("local agent deployment is not approved")
        if len(eligible) > 1:
            raise BindingResolutionError("local agent binding selection is ambiguous")

        selected_binding, selected_deployment, selected_version = eligible[0]
        if selected_version.runtime is not AgentRuntimeKind.CODE:
            raise BindingResolutionError("local agent runtime is unsupported")
        if request.capability.key not in selected_version.capabilities:
            raise BindingResolutionError("local agent capability is unsupported")

        implementation_key = selected_version.runtime_config.get("implementation_key")
        try:
            handler = self._registry.construct(
                implementation_key=implementation_key,
                capability=request.capability,
                version=selected_version,
            )
        except HandlerRegistryError:
            raise BindingResolutionError("local agent implementation is unavailable") from None

        return ResolvedAgentBinding(
            workspace_id=request.workspace_id,
            environment=request.environment,
            capability=request.capability,
            schema_identity=CapabilitySchemaIdentity.from_capability(request.capability),
            binding=selected_binding,
            deployment=selected_deployment,
            agent_version=selected_version,
            implementation_key=implementation_key,
            handler=handler,
        )

    @staticmethod
    def _read(operation: Any, failure_message: str) -> Sequence[Any]:
        try:
            result = operation()
        except Exception:
            raise BindingResolutionError(failure_message) from None
        if not isinstance(result, Sequence):
            raise BindingResolutionError(failure_message)
        return result

    @staticmethod
    def _read_value(operation: Any, failure_message: str) -> Any:
        try:
            return operation()
        except Exception:
            raise BindingResolutionError(failure_message) from None
