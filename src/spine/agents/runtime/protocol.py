from __future__ import annotations

from typing import Generic, Protocol, TypeVar, runtime_checkable
from uuid import UUID

from spine.agents.runtime.contracts import AgentExecutionContext, AgentRequest, AgentResponse
from spine.domain.agents.contracts import AgentInvocation, AgentResult
from spine.domain.agents.models import AgentVersion

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")


@runtime_checkable
class AgentHandler(Protocol, Generic[InputT, OutputT]):
    """Structural async handler contract with no framework base class."""

    async def invoke(
        self,
        request: AgentRequest[InputT],
        context: AgentExecutionContext,
    ) -> AgentResponse[OutputT]: ...


class AgentRuntime(Protocol):
    """Executes one pinned agent version without owning business workflow state."""

    async def invoke(
        self,
        version: AgentVersion,
        invocation: AgentInvocation,
    ) -> AgentResult: ...

    async def cancel(self, step_run_id: UUID) -> None: ...
