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
from spine.agents.runtime.artifacts import (
    ArtifactDraft,
    ArtifactPersistence,
    ArtifactReadError,
    CapabilityArtifactSchema,
)
from spine.agents.runtime.local import LocalAgentRuntime
from spine.agents.runtime.ports import (
    AuthorizationContextView,
    CanonicalTrustedAuthorizationContextVerifier,
    CancellationToken,
    EventSink,
    ExecutionScopeValidator,
    TrustedAuthorizationContextVerifier,
    TrustedExecutionAdmission,
    TrustedExecutionIdentity,
)
from spine.agents.runtime.protocol import AgentHandler, AgentRuntime
from spine.agents.runtime.context import (
    AgentExecutionContextAdmission,
    AgentExecutionContextConstructionError,
    AgentExecutionContextFactory,
)

__all__ = [
    "AgentExecutionContext",
    "AgentExecutionContextAdmission",
    "AgentExecutionContextConstructionError",
    "AgentExecutionContextFactory",
    "AgentHandler",
    "AgentInvocationContext",
    "AgentRequest",
    "AgentResponse",
    "AuthorizationContextView",
    "CanonicalTrustedAuthorizationContextVerifier",
    "AgentRuntime",
    "ArtifactDraft",
    "ArtifactPersistence",
    "ArtifactReadError",
    "CancellationToken",
    "EventSink",
    "ExecutionScopeValidator",
    "ExecutionIdentitySnapshot",
    "CapabilitySchemaIdentity",
    "CapabilityArtifactSchema",
    "LocalAgentRuntime",
    "TrustedExecutionIdentity",
    "TrustedExecutionIdentitySnapshot",
    "TrustedAuthorizationContextVerifier",
    "TrustedExecutionAdmission",
    "TrustedToolGrantSet",
    "agent_result_from_response",
    "adapt_async_function",
    "invocation_context_from_invocation",
    "typed_request_from_invocation",
]
