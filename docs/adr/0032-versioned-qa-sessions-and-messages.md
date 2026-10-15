---
status: accepted
---

# Versioned Q&A sessions and messages

Q&A history is a product read model for navigation and review. It is not a
source of truth for operational facts, evidence, authorization, or future
retrieval context.

## Context

The Q&A product case needs durable sessions and messages that can be listed,
restored, retried and inspected independently of an agent implementation. The
records are tenant-owned, must survive retries, and may contain protected
question or answer content. Authorization can change after a message is
written, so a historical record cannot carry a permanent disclosure decision.

## Decision

- PostgreSQL is the canonical store for Q&A sessions and messages. Each record
  is scoped by `workspace_id` and `environment_id` and is represented by
  append-only version rows with a stable identity and explicit version number.
- The application boundary exposes a provider-neutral session/message port.
  In-memory implementations are test seams; they do not change canonical
  ownership or the API contract.
- Session and message mutations are idempotent and use optimistic version
  checks for restore and future state transitions. Cursors are signed and
  bound to the workspace/environment and operation.
- A message may retain run, trace and disclosure-dependency metadata, but prior
  answers are interpretive history only. They are never evidence and never
  authorize a later answer.
- Every message read re-evaluates current disclosure eligibility. If the
  question or answer is not currently eligible, the response exposes safe
  metadata while omitting the protected content as a whole.

## Consequences

Q&A API consumers can safely replay commands and paginate a stable tenant
scope. Rebuilds and migrations must preserve immutable versions and their
provenance. A later authorization change can make an otherwise valid historical
message metadata-only, so clients must handle partial content and cannot cache
disclosure decisions indefinitely.
