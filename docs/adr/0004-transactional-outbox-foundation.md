---
status: accepted
---

# Transactional outbox intent foundation

## Context

PostgreSQL is Spine's canonical store, while downstream projection, worker, and
workflow processing is asynchronous and at-least-once. Persisting a canonical
mutation and then separately requesting downstream work creates a failure window:
either the canonical change can commit without its intent, or an intent can
escape for a mutation that later rolls back. Spine also needs stable event
identity for future idempotent consumers without introducing another durable
workflow engine.

This ADR fulfils the ADR-004 reservation in `docs/ARCHITECTURE.md`. Its scope is
only durable outbox intent persistence. Issue #4 retains ownership of dispatch
and operational delivery behavior.

## Decision

PostgreSQL is the transactional authority for a canonical mutation and its
outbox intent. Both records are written through the same Unit of Work and commit
atomically in one database transaction; rollback removes both effects.

Each outbox record is immutable and has an opaque stable event identity, a
versioned event type and payload envelope, tenant scope, and producer-defined
deduplication identity where one exists. These identities are prerequisites for
a future consumer to record idempotent consumption atomically with its own
logical effect; they do not claim exactly-once delivery.

Payloads contain the minimum governed information required by the event type.
Protected content, questions, credentials, and other sensitive data are excluded
unless a consuming specification demonstrates necessity and defines applicable
authorization, retention, and redaction. Persisting an intent grants no
authorization or disclosure right.

Outbox claiming, dispatch, delivery retries, consumer processing, cleanup, and
operational telemetry are deferred to Issue #4. Composite durable lifecycle,
long waits, cancellation, retry exhaustion, approvals, and recovery remain
Temporal-owned under ADR 0010 and the accepted Issue #11 execution boundary. The
outbox is not a workflow engine.

## Rationale

The single PostgreSQL transaction removes the dual-write failure window without
a distributed transaction. Stable, versioned identities support at-least-once
delivery and future idempotent consumers while keeping canonical authority and
authorization in PostgreSQL. Deferring delivery mechanics keeps the R1 kernel
narrow and preserves Temporal as the sole durable orchestrator.

## Considered Alternatives

- Publishing directly after commit was rejected because a process crash can
  permanently lose the downstream intent.
- Publishing before commit was rejected because consumers can observe work whose
  canonical mutation later rolls back.
- A distributed transaction with a broker was rejected as unnecessary for the
  R1 single-deployment architecture.
- Storing delivery or workflow lifecycle in outbox rows was rejected because it
  duplicates responsibilities assigned to Issue #4 and Temporal.

## Consequences

- Every canonical mutation that requires downstream work must persist its outbox
  intent in the same Unit of Work.
- Failed transactions expose neither the canonical mutation nor its intent.
- Future dispatchers and consumers must tolerate duplicate delivery and use the
  stable identities rather than assume exactly-once processing.
- Event-specific specifications must define typed payload schemas and prove
  sensitive-data minimization.
- PostgreSQL backup, migration, retention, and recovery procedures include
  pending outbox intents.

## Relationships

- ADR 0001 establishes PostgreSQL as canonical storage and the Context Graph as
  a rebuildable projection.
- ADR 0010 keeps durable workflow orchestration in Temporal.
- ADR 0011 remains the authorization authority for protected document content;
  outbox persistence cannot grant access.
- ADR 0018 uses atomic outbox intents for canonical source and projection
  publication transitions without making the projection backend authoritative.
- Issue #11 selects PostgreSQL/outbox/workers for short idempotent work units and
  Temporal for composite durable lifecycle.
