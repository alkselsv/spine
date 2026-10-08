---
status: accepted
---

# Grounded answer disclosure contract

R1 discloses an answer only when every externally verifiable factual claim is
linked to exact authorized evidence from the persisted Context Bundle and passes
deterministic identity, revision, digest, locator, canonical-currentness and
authorization checks plus a versioned semantic-support policy. Grounded Answer
is a binary disclosure predicate; groundedness and confidence remain evaluation
metadata and cannot override the gate. General model knowledge, prior answers and
user factual premises are not source evidence, while labelled Task Inputs and
hypotheses may support explicitly conditional reasoning.

Claims have explicit immutable evidence relationships and citations resolve to
SourceObject, SourceRevision, original digest, format-specific versioned locator
and exact Context Bundle evidence. Partiality, completeness and grounded conflict
are independent semantics. Exhaustive answers compare a server-derived Target
Completeness Scope with verified Observed Coverage; incomplete coverage permits a
partial fallback only when the user explicitly authorized it. Candidate content
is never streamed before the complete disclosure gate passes.

Each generation attempt pins one active Projection Snapshot, activation
generation, Canonical Boundary, `as_of` and Context Bundle. Later activation does
not alone invalidate the attempt, but material or unknown impact requires
revalidation or bounded regeneration. Current authorization and source validity
govern every later disclosure. Every protected source exposed to generation is a
Disclosure Dependency, so R1 hides the whole stored answer when any dependency
becomes unauthorized, superseded, tombstoned or retention-ineligible.

Expected inability to establish a grounded answer is abstention; authorization
rejection is denial; unavailable mandatory infrastructure is failure. Public
reasons are non-leaking, protected diagnostics require independent content
authorization, and clarification is limited to safely resolvable ambiguity.
Composite retries, cancellation and recovery remain Temporal-owned under the
accepted execution boundary, while bounded technical attempts remain runtime or
worker concerns.

## Rationale and rejected alternatives

Citation presence, sentence-level markers, model confidence, recency, retrieval
success and healthy projection status were rejected as sufficient grounding.
Spine also rejects silent partial answers, model arbitration of conflicts,
mixing snapshots within an attempt, candidate-token streaming, treating session
history as evidence and using stored excerpts or fake-only tests as disclosure or
production-release authority. These alternatives cannot preserve claim support,
least privilege, completeness, temporal consistency and reproducibility together.

## Consequences and related decisions

The capability, Context Broker, persistence, SSE interface and evaluations must
preserve claim-level lineage, immutable validation facts, protected audit records
and current disclosure eligibility. Mechanically enforceable safety invariants
are hard gates; probabilistic semantic correctness is measured with versioned
validators and human-labelled evaluations without claiming mathematical
certainty. Numerical quality thresholds belong to evaluation baseline and release
approval, and production qualification must exercise the configured integration
stack rather than only fakes.

This ADR composes with, and does not amend, ADR 0011: PostgreSQL current policy
remains the sole authorization authority and all contributing sources require
current access. It also composes with, and does not amend, ADR 0018: ordinary Q&A
uses an active publication, canonical revisions and tombstones override projected
state, `as_of` is server-selected, and historical disclosure remains limited to
the explicitly authorized revision-history or citation-inspection flow.
