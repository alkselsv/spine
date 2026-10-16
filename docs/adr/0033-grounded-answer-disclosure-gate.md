---
status: accepted
---

# Grounded answer disclosure gate

The final Q&A disclosure decision is a provider-neutral, fail-closed domain
boundary. Candidate answer content is untrusted until the complete validation
predicate succeeds.

## Decision

- The gate requires exact ContextBundle identity, citation validation, semantic
  support, completeness/partiality and conflict evaluation, and a fresh current
  disclosure-eligibility result. Caller-provided disclosure booleans,
  confidence, retrieval presence and model metadata cannot override these facts.
- The accepted value is an immutable `AcceptedGroundedAnswer` carrying the
  answer and all validation lineage. Safe outcomes contain only typed terminal
  metadata; rejected candidate text is never persisted or sent to the accepted
  output port.
- Every attempt persists an immutable result record, including validation
  references when available. Publication acknowledgement is separate mutable
  delivery state so a post-persistence publication failure can be retried
  without recomputing or exposing the candidate.
- The repository is tenant-scoped and idempotent on workspace, environment and
  request identity. Concurrent inserts use conflict-safe persistence.

## Consequences

The gate can be reused by API and workflow transports without coupling the
decision to FastAPI, an agent framework or a model provider. Operators can
replay safe failures and publication attempts while preserving the original
decision. Consumers must handle abstention, denial, cancellation and failure as
terminal no-content outcomes.
