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

For content observations, a verified physical `WriteReceipt` may be associated
with the immutable revision and its observation provenance in the same canonical
transaction. This association preserves the receipt, command-result, object
generation, digest and length evidence, but does not admit, accept, publish or
select a current revision. A finalized object without that committed association
remains an inaccessible orphan candidate; later tombstones and lifecycle changes
retain historical associations without making their physical bytes readable.

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

## Provider absence and canonical deletion authority

Provider enumeration is observation evidence, not deletion authority. For a
Yandex Disk path-scoped source, canonical deletion requires both a confirmed
complete scan and an immutable `YandexDiskRemovalPolicy` admitted for the exact
workspace, environment, connection and resource scope. The provider-connection
control plane owns these approval artifacts; PostgreSQL is their canonical store.
The reconciliation replay envelope, the provider and Cognee are not policy
authorities. This extends the provider approval separation in ADR-0050 to the
stronger action of changing canonical source truth.

A Data/Security Owner (or another explicitly delegated approver recorded by the
control plane) may approve the policy. Connector workers and reconciliation
agents may only consume it through the admission port and cannot create or widen
their own deletion authority. Policies and approvals are append-only and
versioned. Revocation or supersession records a new current-authority fact rather
than editing the approved artifact. Admission revalidates current authority on
every start or resume; a revoked approval fails closed. A run pins both
`policy_version_id` and `approval_id` in its identity and durable state, so a
retry cannot replace a non-deleting policy with a deleting one. A newly approved
policy requires a distinct reconciliation run.

Offset pagination alone is not deletion evidence. The qualified Yandex adapter
must finish a bounded inventory pass, repeat the complete inventory, and confirm
the same ordered digest over the full provider resource representations. The
application additionally requires one consistent expected count, exact unique
coverage and that confirmed scan generation before absence can be classified as
removal. Incomplete pagination, reordering or substitution between passes,
throttling exhaustion, outage, access loss, scope loss and authorization
revocation remain non-deleting evidence. This conservative two-pass fence is
sufficient for the current provider contract because any unconfirmed concurrent
change rejects the generation; if Yandex exposes a stronger snapshot token, a
successor adapter/policy version may require it without weakening this rule.

Even with deletion authority, path equality, filename and checksum never prove
object continuity. Rename/move is an observation at a new path plus separately
authorized absence at the old path; reappearance after a tombstone creates a new
path-scoped `SourceObject` and retains explicit non-equivalence lineage.

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
