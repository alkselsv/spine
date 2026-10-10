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

This separation trades some diagnostic convenience for a smaller disclosure
surface and preserves the different retention, availability and evidentiary
semantics of canonical audit, asynchronous delivery and telemetry. It composes
with ADR 0004 and does not transfer composite lifecycle, long waits, cancellation
or business retry exhaustion away from Temporal.
