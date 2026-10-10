"""Framework-independent local agent runtime contracts."""

from spine.agents.runtime.contracts import (
    AgentInvocationContext,
    AgentExecutionContext,
    AgentRequest,
    AgentResponse,
    CapabilitySchemaIdentity,
    ExecutionIdentitySnapshot,
    TrustedExecutionIdentitySnapshot,
    TrustedToolGrantSet,
    agent_result_from_response,
    invocation_context_from_invocation,
    typed_request_from_invocation,
)
from spine.agents.runtime.adapters import adapt_async_function
from spine.agents.runtime.ports import (
    AuthorizationContextView,
    CancellationToken,
    EventSink,
    ExecutionScopeValidator,
    TrustedAuthorizationContextVerifier,
    TrustedExecutionIdentity,
)
from spine.agents.runtime.protocol import AgentHandler, AgentRuntime
from spine.agents.runtime.context import (
    AgentExecutionContextConstructionError,
    AgentExecutionContextFactory,
)

__all__ = [
    "AgentExecutionContext",
    "AgentExecutionContextConstructionError",
    "AgentExecutionContextFactory",
    "AgentHandler",
    "AgentInvocationContext",
    "AgentRequest",
    "AgentResponse",
    "AuthorizationContextView",
    "AgentRuntime",
    "CancellationToken",
    "EventSink",
    "ExecutionScopeValidator",
    "ExecutionIdentitySnapshot",
    "CapabilitySchemaIdentity",
    "TrustedExecutionIdentity",
    "TrustedExecutionIdentitySnapshot",
    "TrustedAuthorizationContextVerifier",
    "TrustedToolGrantSet",
    "agent_result_from_response",
    "adapt_async_function",
    "invocation_context_from_invocation",
    "typed_request_from_invocation",
]
