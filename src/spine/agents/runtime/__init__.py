"""Framework-independent local agent runtime contracts."""

from spine.agents.runtime.contracts import (
    AgentExecutionContext,
    AgentRequest,
    AgentResponse,
    TrustedExecutionIdentitySnapshot,
    agent_request_from_invocation,
    agent_result_from_response,
)
from spine.agents.runtime.ports import CancellationToken, EventSink, TrustedExecutionIdentity
from spine.agents.runtime.protocol import AgentHandler, AgentRuntime

__all__ = [
    "AgentExecutionContext",
    "AgentHandler",
    "AgentRequest",
    "AgentResponse",
    "AgentRuntime",
    "CancellationToken",
    "EventSink",
    "TrustedExecutionIdentity",
    "TrustedExecutionIdentitySnapshot",
    "agent_request_from_invocation",
    "agent_result_from_response",
]
