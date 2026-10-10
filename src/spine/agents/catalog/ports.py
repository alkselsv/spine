"""Read-only ports used by local agent binding resolution."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable
from uuid import UUID

from spine.domain.agents import AgentBinding, AgentDeployment, AgentVersion
from spine.domain.common import EnvironmentKind


@runtime_checkable
class AgentBindingCatalog(Protocol):
    """Read-only catalog needed to resolve one local Agent Binding."""

    def list_bindings(
        self,
        workspace_id: UUID,
        environment: EnvironmentKind,
        capability_key: str,
    ) -> Sequence[AgentBinding]: ...

    def get_agent_version(self, agent_version_id: UUID) -> AgentVersion | None: ...

    def list_deployments(
        self,
        workspace_id: UUID,
        environment: EnvironmentKind,
        agent_version_id: UUID,
    ) -> Sequence[AgentDeployment]: ...
