---
status: accepted
---

# Resumable Q&A event contract

## Context

Q&A clients need progress and terminal-safe delivery without making a browser
transcript the source of truth. SSE may disconnect, reconnect with a
`Last-Event-ID`, or be unavailable while the same contract is used by polling.
Protected answer fragments and citations also require a fresh disclosure check
after persistence because authorization can change between emissions.

## Decision

- Q&A emissions use one versioned, transport-neutral envelope with a stable
  emission UUID, positive monotonic sequence, workspace/environment/session
  scope, trace identity, optional message/run correlation for pre-run and
  cancellation emissions, and a closed payload for each supported event type.
- The append-only event log is idempotent by emission identity, rejects gaps,
  sequence reuse and emissions after a terminal event, and treats persistence
  as proof of emission only—not proof of client receipt.
- Replay accepts a scope-bound event cursor or sequence, applies a bounded
  ordered result, and fails safely for malformed, stale, ambiguous or
  cross-scope cursors. A terminal event can be rehydrated after disconnect.
- Heartbeat and cancellation are explicit stream operations. Retries of the
  same heartbeat or cancellation have one logical effect; cancellation does
  not create a second terminal event.
- `answer_delta` and `citation_added` carry an opaque authorized
  accepted-output/disclosure receipt. The event contract contains no candidate
  output, provider payload, credentials or chain-of-thought. Current
  authorization is checked again for each protected delivery; a changed grant
  produces a safe denial without returning the protected emission.
- The deterministic in-memory log and stream are contract fakes. They do not
  implement production API/SSE handlers or replace PostgreSQL canonical event
  persistence.

## Consequences

API/SSE and polling adapters can share ordering, replay, terminal and
disclosure semantics without importing FastAPI, a frontend state model or a
provider SDK. A future durable implementation must preserve the same event
identity, scope-bound cursor and no-receipt-acknowledgement invariants, and must
persist the canonical emission separately from mutable delivery state.
