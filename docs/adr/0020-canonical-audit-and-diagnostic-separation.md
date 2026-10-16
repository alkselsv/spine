---
status: accepted
---

# Canonical audit and diagnostic separation

Spine records meaningful commands, decisions, state transitions, retry outcomes
and feedback as immutable, versioned Audit Events in tenant-scoped PostgreSQL.
When an event proves a canonical mutation, it commits in the same Unit of Work;
when it gates protected disclosure or an external action, required audit must be
durable before that effect is released. A failed required audit never permits the
effect to proceed.

Operational logs, metrics, spans and progress observations are Diagnostic Events,
not audit authority. They use allowlisted disclosure-safe schemas and may be
sampled, delayed or unavailable without rewriting history. Audit Events and
Diagnostic Events never contain protected document text, credentials, prompts,
answers, citation excerpts, raw provider payloads, exception messages, stack
traces or chain-of-thought. Protected detail remains in its owning canonical
record and is retrieved only through its authorization contract.

Outbox intents remain immutable facts that downstream work is required. Mutable
claim, lease, retry and acknowledgement state lives in separate operational
delivery records, so dispatch does not turn the outbox into a workflow engine or
claim exactly-once delivery. Duplicate delivery is expected and consumers use the
stable event identity for idempotency.

A dedicated `spine_dispatcher` login is the only cross-tenant operational
reader/writer. It may read immutable outbox intents and mutate only their
delivery control rows. It is a non-owner role with `NOBYPASSRLS`, has no role
memberships, and cannot access canonical business or audit tables. The ordinary
runtime role remains tenant-scoped.

The dispatcher records a delivery transition and its required Audit Event with
one fixed-shape, migration-owned database function. The dispatcher role receives
only `EXECUTE` on that function, never general Audit Event table privileges. A
failed audit insert rolls back the delivery transition; Diagnostic Events are
emitted only after this transaction commits and cannot change its outcome.

This separation trades some diagnostic convenience for a smaller disclosure
surface and preserves the different retention, availability and evidentiary
semantics of canonical audit, asynchronous delivery and telemetry. It composes
with ADR 0004 and does not transfer composite lifecycle, long waits, cancellation
or business retry exhaustion away from Temporal.

## R1 operational observation profile

`OperationalObservation:v1` is the Spine-owned diagnostic contract across API,
outbox, worker, projection, retrieval, generation, persisted run and SSE
boundaries. It retains the trusted `DiagnosticContext` identity and exact
workspace/environment scope while allowing only closed component, stage,
operation, outcome and safe-failure vocabularies. Run, attempt and message IDs
are correlation references; metric dimensions are limited to the closed
low-cardinality vocabulary and never include those identities.

The versioned R1 policy retains observations for 30 days subject to mandatory
legal holds, keeps 100% of error traces and a deterministic 10% of successful
traces, and disables external export. Sampling never changes propagation and
never applies to required Audit Events. Observation sink failure remains
best-effort; the required Audit Event gate above remains fail-closed.

The in-memory retention/read adapter qualifies the application contract only.
Production qualification still requires a deployment-owned retention and
legal-hold binding. No external telemetry backend, credentials or audit
authority are approved by this profile.

Operational hooks must sit at the boundary they describe. The API adapter may
observe HTTP acceptance but cannot synthesize generation or run-persistence
outcomes; generation, outbox enqueue, run persistence and SSE delivery are
observed by their runtime or port adapters. Contract and trusted-context
validation fail closed before the handoff. Only emission to an already
validated diagnostic sink is isolated as best-effort.
