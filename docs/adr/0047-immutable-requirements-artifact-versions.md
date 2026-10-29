---
status: accepted
---

# Immutable requirements artifact versions

## Context

Conversational requirements extraction produces a reviewable proposal for a
specific WorkItem and WorkflowRun. Later clarification and editing must extend
that history without overwriting a prior candidate or creating competing
versioning protocols. Extraction is probabilistic, so confidence is evidence
about a candidate and cannot itself establish acceptance.

## Decisions

1. A requirements output is a typed, schema-versioned `RequirementsArtifact`
   containing typed `Requirement` records, explicit candidate/partial/
   unsupported/unverified state, accepted source revision references and
   upstream artifact lineage. Requirements have review state; no accepted state
   is inferred from model confidence.
2. PostgreSQL stores append-only artifact versions under the exact workspace,
   environment, WorkItem and WorkflowRun. Composite scope keys and foreign keys
   bind each version to its run and work item; tenant RLS is enforced and the
   runtime role cannot update or delete a version.
3. A producer identity is unique per run and artifact. Replaying it with the
   same digest returns the stored version; a different digest conflicts. A
   transaction-scoped artifact lock allocates successors monotonically, and
   every successor records its predecessor version and retains lineage.
4. Readback requires current server-derived workspace/environment
   authorization. Foreign or missing artifacts have the same not-found shape.
   The conversational card renders the stored artifact, not a browser-local
   extraction result.
5. The first candidate is produced by the proposal-specific
   `extract_requirements` capability through the existing Agent Runtime when
   that workflow composition is enabled. The pre-runtime deterministic
   producer remains as a compatibility fallback for the current start API and
   uses the same immutable artifact/version boundary.
6. Missing or ambiguous information is retained as a typed `Clarification`
   inside the exact artifact lineage. Opening it advances the existing run to a
   durable human wait; answering it is a shared `WorkflowCommand` against the
   originating artifact version, never a new WorkItem, WorkflowRun or Q&A
   Session.
7. Answer admission appends one successor, revalidates only the affected
   requirement evidence under current authority, and records content-free
   wait/resume events and Temporal signals. Exact replay returns the original
   receipt; stale run/artifact versions and mismatched clarification identity
   fail without a second successor.

## Consequences

- The artifact repository and version rules are the shared persistence seam for
  subsequent clarification, evidence review, editing and acceptance tickets.
- An artifact version is immutable even when a producer retries or a later
  version is created. Rebuilding the candidate requires a new producer identity
  and preserves the prior version.
- Source references identify accepted revisions only; disclosing source content
  remains subject to the separate evidence authorization contract.
- PostgreSQL remains the canonical read model for questions, answers, artifact
  lineage and run state. Temporal owns the durable wait and resumes the same
  pinned workflow from content-free outbox signals; its history is not the
  conversational timeline.

## Source

Ticket #640 under the R2 conversational client-request specification (#636).
