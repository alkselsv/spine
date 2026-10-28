---
status: accepted
---

# Typed workflow commands and the event read model

## Context

After conversational admission, a manager must continue the same durable work
without creating another WorkItem, WorkflowRun, Q&A Session, or browser-owned
workflow state. Retries must be safe, concurrent edits must not overwrite one
another, and the product feed must not depend on Temporal history or expose
protected command content.

## Decisions

1. `POST /api/v1/runs/{run_id}/commands` accepts only command variants shipped
   in the pinned product contract. The first registry contains `add_context:v1`;
   every envelope has command/idempotency identity, trusted tenant and actor
   scope, expected run version, correlation/causation, and exact artifact
   versions when a command consumes artifacts. Unknown fields are rejected.
2. PostgreSQL atomically claims idempotency, compares `expected_run_version`,
   increments the canonical run version, stores a command receipt with the
   protected typed payload, and appends a disclosure-safe event. A replay is
   resolved before the optimistic check and returns the original receipt.
3. Terminal states reject continuation. A stale version returns a conflict and
   the current disclosure-safe snapshot; an absent or foreign-tenant run has the
   same not-found result.
4. `GET /api/v1/runs/{run_id}` returns `WorkflowRunSnapshot`, not protected
   trigger or command content. `GET .../events` returns fixed safe summaries in
   canonical sequence order through signed, scope- and query-bound cursors.
   Neither endpoint reads Temporal history.
5. The browser restores its timeline from snapshot and events. Its
   `allowed_commands` controls are advisory; every mutation is independently
   authorized and validated by the server.

## Consequences

- A lost HTTP response can be retried without a duplicate logical effect.
- Canonical protected context remains available to later workflow composition,
  while the generic feed and diagnostics remain disclosure-safe.
- New command kinds require an explicit versioned payload model, transition
  policy, persistence mapping, tests, and event summary; arbitrary external
  schemas are not accepted.
- PostgreSQL owns product receipts, snapshots, and events. Temporal continues to
  own durable execution history and is not a browser read model.

## Source

Ticket #639 under the R2 conversational client-request specification (#636).
