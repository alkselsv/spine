from __future__ import annotations

from typing import Protocol
from uuid import UUID

from spine.domain.agents.contracts import AgentInvocation, AgentResult
from spine.domain.agents.models import AgentVersion


class AgentRuntime(Protocol):
    """Executes one pinned agent version without owning business workflow state."""

    async def invoke(
        self,
        version: AgentVersion,
        invocation: AgentInvocation,
    ) -> AgentResult: ...

    async def cancel(self, step_run_id: UUID) -> None: ...
