---
status: accepted
---

# Capability-based local agent runtime

Workflow steps invoke a versioned Capability rather than a Python class, model or
agent framework. Each capability owns its typed input/output schemas, and an
Agent Binding resolves it to a pinned Agent Version. Local implementations either
satisfy a small structural `AgentHandler` protocol or use an async-function
adapter; mandatory base classes, module auto-discovery and executable import paths
from stored configuration are rejected to keep implementations replaceable and
deployments explicit.

The runtime translates the artifact-based `AgentInvocation` into a typed
`AgentRequest` plus `AgentExecutionContext`, then validates and persists the
typed `AgentResponse` as an `AgentResult`. It owns lineage, idempotency, events,
cooperative cancellation and short technical retries. Durable retries, fallback,
approvals and stop decisions remain workflow concerns. Agent implementations are
stateless between calls; internal multi-agent coordination remains private only
while it has no independently evaluated artifact, external action, approval or
durable lifecycle.

Deterministic async code is a `code` step when it is bound directly into a
workflow. It becomes an agent implementation only when exposed as a replaceable,
versioned capability governed by Agent Binding, deployment and evaluations.
External agents and their SDK/transport are deferred until a concrete product
case requires them; the local runtime seam must allow a future remote adapter
without changing workflow or capability contracts.
