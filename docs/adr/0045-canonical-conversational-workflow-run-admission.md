---
status: accepted
---

# Canonical conversational workflow run admission

## Context

The first human message that starts operational work must create a durable
business work item and an execution identity that can be retried and inspected.
The request must not become a second Q&A session or remain only in a chat
transcript. PostgreSQL owns the admission facts; Temporal owns durable execution
after an outbox handoff.

## Decisions

1. `POST /api/v1/runs` accepts a versioned client request and an idempotency key.
   Workspace, environment, actor, trace context, and workflow version are
   resolved by trusted server boundaries; callers cannot select an implementation
   version directly.
2. One tenant-scoped PostgreSQL transaction writes the `WorkItem`, pinned
   `WorkflowRun`, idempotency receipt, and content-safe outbox intent. A retry
   with the same key and payload returns the original run. Reusing the key with
   changed protected data conflicts.
3. The outbox event contains stable identities and the immutable workflow
   version reference. It excludes the user's message and source content. The
   Temporal start uses the canonical run ID as its stable workflow ID, so
   duplicate delivery addresses the same execution.
4. Run readback is scoped to the authorized workspace and environment. A run
   outside that scope is indistinguishable from a missing run.
5. Deployments publish workflow versions through an immutable configuration
   registry. An absent version fails closed; request input never creates or
   changes a workflow definition.

## Consequences

- PostgreSQL and the outbox are the recovery boundary between admission and
  Temporal dispatch. Dispatch is at-least-once and safe to retry.
- The run record pins its selected version even if the deployment later
  publishes another version.
- The first message is stored with the canonical work item for authorized
  readback. It is deliberately absent from dispatch payloads and diagnostics.
- Execution behavior for individual business workflows remains the
  responsibility of their versioned workflow definitions and workers.

## Source

Ticket #638, under the R2 conversational run admission specification.
