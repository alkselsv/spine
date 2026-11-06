---
status: accepted
---

# Email Option B content disposition and canonical erasure boundary

This ADR records the internal policy decisions approved under #998 for future
Option B design. Implementation and qualification remain pending. Work is
synthetic/offline only; #876 Option C NO-GO remains effective. This ADR grants no
Gmail access, live retention, production enrollment or activation.

## Decision

Treat every Gmail-derived source-bearing artifact as removable data. The
`SourceObject` / `SourceRevision` identity, provider identifiers, provenance,
references and joins exist only while their related content remains active.
Disposition removes the source-specific canonical identity/history and every
message-resolving link as well as payload copies. Immutability means a retained
record is not rewritten; it does not require retaining a source-specific record
after its approved lifetime. Do not replace erased lineage with a tombstone,
hash, opaque reference, correlation key or audit join that can resolve the
message. Canonical deletion history is therefore intentionally not retained for
disposed Gmail sources.

Persist only the exact #998 HD1 content-free per-operation audit allowlist:
random audit event ID, workspace ID, provider and operation type, applied
policy ID/version/digest, request and completion times, aggregate
processed/deleted/failed counts, allowlisted result/error codes, executing
component version and an internal approval reference when required. The record
proves that a procedure ran; it does not prove deletion of a particular message.
It expires no later than 365 days after operation completion, with earlier
workspace deletion when required. It contains no Gmail identifiers, addresses,
content, filenames, hashes, SourceObject/SourceRevision references, provenance,
evidence links, correlation tokens or join paths. An idempotency key and its
Gmail mapping are limited to the bounded open operation/retry lifecycle and are
disposed when that lifecycle closes.

## Artifact policy

The #820 classes have these internal dispositions:

| # | Artifact class | Maximum / disposition |
|---|---|---|
| 1 | Raw MIME / RFC822 | Transient only; clean on every terminal path. |
| 2 | Headers and addresses | At most 90 days from provider receipt, while current authority applies. |
| 3 | Plain-text body | Sanitized/normalized form only, at most 90 days; unsanitized working form is transient. |
| 4 | Parsed HTML | Transient only; no durable HTML after sanitization. |
| 5 | Sanitized content | At most 90 days from provider receipt. |
| 6 | Attachment originals | Transient only; no durable persistence. |
| 7 | Attachment-derived text | Separately allowlisted types and purpose; no longer than the parent source and at most 90 days. |
| 8 | Attachment-derived structured artifacts | Not approved by default; requires a new class/purpose decision. |
| 9 | Thread / conversation metadata | At most 90 days from provider receipt. |
| 10 | Provider message / folder identifiers | Only while related content is active; delete with the source. |
| 11 | SourceObject / SourceRevision identity and lineage | Only while related content is active; remove with the source and all joins. |
| 12 | Search indexes | Minimum necessary only, at most 90 days, excluded from backup. |
| 13 | Vector embeddings / projections | Not approved while ordinary search is sufficient; requires a new necessity decision. |
| 14 | Generated summaries | At most 90 days and never longer than the source. |
| 15 | Monitoring evidence | At most 90 days, never longer than the source, and subject to current disclosure authority. |
| 16 | Staging artifacts | Transient only; clean on success, failure, cancellation and retry exhaustion. |
| 17 | Temporary files / caches | Transient only; no independent retention or backup. |
| 18 | Replicas / backups | Active replicas converge with primary disposition. Backups may retain protected ciphertext under the daily / 30-day rotation profile; readable capability ends at disposition. |
| 19 | Audit records | HD1 allowlist only; at most 365 days from operation completion, then delete. |
| 20 | Operational diagnostics | Allowlisted content-free safe codes only; no Gmail identifiers, addresses, subjects, filenames, content, hashes or join paths. Use the applicable diagnostics expiry; diagnostics are not an evidence store. |

All source-bearing deadlines start at provider receipt, never ingestion. Missing
or ambiguous receipt time fails closed; do not substitute ingestion time. Earlier
disposition applies on authority withdrawal, mailbox reassignment or disconnect,
purpose termination, verified provider deletion after qualified reconciliation,
or an applicable deletion request. A derivative never outlives its source.
Missing, stale, ambiguous or expired `RetentionApplicability` is
`NOT_APPROVED`. No Gmail legal hold exists in Option B v1; external preservation
does not extend Spine retention.

## Disposition and recovery invariants

`RetentionApplicability` establishes retention eligibility and terms; by itself
it is not deletion authority. A disposition command must also use the existing
versioned current deletion-policy approval pattern from #812. Bind that approval
to the trusted workspace and environment, exact source scope and expected source
generation, the approved policy/version/digest, one recognized disposition
basis, its immutable evidence reference, and an idempotent operation identity.
Revalidate the approval's issuer, current pointer, generation and revocation
state in the same PostgreSQL transaction that creates `DELETION_PENDING` and
the outbox intent. Connector observations and caller-supplied reason codes are
never authority.

Recognized bases are: deterministic expiry from verified provider receipt under
the current approved terms; an authorized owner/authority withdrawal, mailbox
reassignment/disconnect or approved-purpose termination; a deletion request
validated and approved under the current deletion policy; and provider deletion
only after qualified reconciliation plus a current authorized lifecycle
decision. These are removal triggers, not new read or retention grants. A
read/processing/disclosure revocation immediately denies those actions but does
not revoke the separate deletion-only authority needed to finish cleanup. A
deletion-policy approval revoked before the fence transaction denies new work;
revocation after fencing cannot clear the fence. Cleanup may resume only under
a fresh current deletion approval bound to the same scope and operation. If
that authority is unavailable, keep the source fenced and report
`NOT_VERIFIED`.

Disposition is a tenant- and environment-scoped, idempotent operation. The
durable coordinator persists its operation state and transactional outbox intent
in PostgreSQL, then invokes independently idempotent store handlers outside the
canonical transaction. Before fencing, a revoked deletion approval or mismatched
source generation denies the operation. Before each store side effect, recheck
the current deletion-only approval and exact source generation. Revoking only
read/processing authority does not block cleanup; revoking deletion authority or
changing its generation pauses further side effects, retains the fence, and
reports `NOT_VERIFIED`. `DELETION_PENDING` fences reads, processing,
projection/index rebuild and publication, disclosure, and checkpoint advancement
before cleanup starts. Keep the fence active through partial
failure, cancellation and unknown commit until the same operation is
reconciled and every registered store verifies disposition. Unknown stores,
incomplete cleanup or failed restore checks are `NOT_VERIFIED`, never PASS.

The versioned store-coverage manifest maps every #820 class to every configured
durable and transient boundary, its owner, handler and verifier, including
PostgreSQL, object versions, staging, projections/indexes, caches, diagnostics,
replicas, WAL and backup/restore paths. Release qualification reconciles that
manifest against the application adapter registry and the declared runtime and
recovery topology. An unregistered, newly configured or otherwise unknown
boundary invalidates completeness and cannot be omitted from a success result.
Local synthetic qualification proves only the declared local topology; an
unknown production topology remains `NOT_VERIFIED`.

Use the existing deletion-authorization pattern in #812 and publication /
revocation fences in #813 and #976 at their established boundaries. Do not
create a second retention authority or make a tombstone an erasure receipt.
Per-store evidence and diagnostics are content-free. Retry uses the same
operation identity and current-authority checks; changed scope or generation is
not replayed as the old operation.

Backups use the approved `r1-coordinated-backup:v1` daily cadence and
30-day ciphertext rotation. The 30-day period never extends readability or
source retention. The key/deletion capability is independent of backup contents
and is removed at disposition; no restore may recreate it. A restore remains
quarantined until current deletion state is reconciled, required cleanup is
replayed, and negative checks deny read, processing, publication, disclosure
and checkpoint effects. Rollback may stop new admissions or resume from the
last verified state, but never restores disposed content or key capability and
never clears an incomplete-deletion fence.

## Consequences and limits

The implementation must qualify each store boundary with synthetic data,
including real local PostgreSQL and applicable object-storage, index/cache and
backup/restore contracts. Fake-only contract PASS does not establish physical
deletion. Production topology, external key service behavior, Google policy
compatibility, restricted-scope verification, CASA/LOA and Security/Operations
deployment acceptance remain separate and `NOT_VERIFIED`. Closing #995 does not
supersede #876, authorize Gmail API calls, activate Option B, or close #864 or
#996.
