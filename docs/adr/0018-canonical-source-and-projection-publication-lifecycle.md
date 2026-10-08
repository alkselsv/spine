---
status: accepted
---

# Canonical source and projection publication lifecycle

Spine records every observed source version as an immutable `SourceRevision`,
while append-only admission and canonical-acceptance decisions determine whether
it may become the `SourceObject`'s current revision. Stable source identity is
independent of filenames, paths and checksums. Trusted source ordering is used
when available; otherwise expected-current optimistic concurrency and a durable,
commit-consistent PostgreSQL acceptance order prevent late or concurrent writes
from silently replacing canonical state. A verified deletion is a tombstone
revision, and verified reappearance creates another revision of the same object
only when identity continuity is established.

Canonical current state advances independently of parsing or projection success.
The Context Broker therefore validates every projected or cached item against the
current revision, tombstone state and the current `AccessPolicy` required by ADR
0011 before ordinary retrieval, model use or evidence disclosure. Projection lag,
rollback and historical evidence cannot revive superseded, deleted or
unauthorized content in Q&A. R1 Q&A uses only the active publication; an explicit
history or citation-inspection flow may disclose an exact historical revision
only under current policy and retention/deletion rules, without treating it as
current or model context. Its server-selected `as_of` identifies a durable
`CanonicalBoundary`, not a client-selected historical query.

Projection processing configuration and publication state are separate:
`ProjectionConfigVersion` is an immutable processing definition, while
`ProjectionSnapshot` is an immutable logical manifest of one exact canonical
input cut, source membership, isolated partition receipts, validation results and
predecessor. Build and validation state belongs to runs and append-only records;
active/retired state is derived from activation history rather than mutating the
manifest. PostgreSQL owns zero active snapshot pointers before initial publication
and exactly one afterward, plus an activation generation, per workspace,
environment and projection kind. Shadow artifacts cannot serve ordinary traffic;
activation is a PostgreSQL compare-and-swap transaction after required validation.
Cognee aliases, caches and physical stores are derived state and never become an
activation authority.

A snapshot may activate with later canonical drift or typed per-source exclusions
only when its boundary is internally complete, all sources are explicitly
accounted for, applicable safety and approved quality gates pass, stale evidence
is fenced, and degraded status is visible without content leakage. Successful
publication is distinct from coverage completeness. Freshness or completeness
requirements that cannot be met cause safe abstention rather than fabricated or
silently partial answers.

Runtime condition does not mutate the snapshot manifest. Publication freshness
(`current | stale`) and health (`healthy | degraded`) are independent derived
dimensions; all four combinations are valid, while manifest and coverage
completeness remain separate facts. Before initial publication the projection is
unpublished/unavailable rather than forced into these dimensions. Serving remains
a per-request decision over current authorization, revision/tombstone validity,
evidence and Context Profile requirements, so healthy never means automatically
safe to answer.

Rollback publishes a new successor snapshot at a non-regressing boundary; it
never points back to an older snapshot or changes canonical source state. Prior
configuration or physical artifacts may be reused only with verified revision
identity, provenance, compatibility and integrity. Partition work is idempotent
by command identity and payload digest, partial writes remain isolated, and
canonical reconciliation fences missing or ambiguous backend state. Recovery may
reconstruct an existing artifact only when its required identity and integrity
are provable; otherwise it builds a successor snapshot. The concrete Cognee
shadow, routing and rollback mechanism remains a feasibility responsibility of
Issue #28 and must not require a distributed transaction with PostgreSQL.
