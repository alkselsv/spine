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

## Processing admission and versioned content acceptance

Canonical acceptance of new S2 content carries an immutable
`SourceProcessingAdmission:v1` with `processing_admission_id`,
`schema_version`, `workspace_id`, `environment_id`, `source_object_id` and
`source_revision_id`, and the exact immutable `selector_id`, bound to that source
and authority scope. Its immutable references include the provider
connection/version and authorization and configuration generations when
provider-backed, exact resource scope or upload authority, and root/content
policy identities with exact versions and digests.
It references the finalized `SourceRevisionReceiptAssociation`, trusted verifier
evidence reference, verifier profile version and evidence digest, applicable
lifecycle and retention evidence, idempotency receipt, audit event and creation
time. It records
the verified SHA-256 digest and byte length. Its discriminated mode carries
either a qualified processor capability, exact profile version and qualification
evidence reference, or an exact opaque approval ID/version/digest with approved
purpose, never both.
Current authority and eligibility are resolved again at commit. The record
references existing immutable source, receipt, policy and audit records rather
than duplicating their canonical facts. It contains no credentials, signed URLs,
provider payloads, content bytes, sensitive filenames or excerpts.

`PROCESSOR_REQUIRED` is the default for new content admission. Before canonical
acceptance, the system verifies the exact source and receipt evidence, current
source and policy authority, lifecycle/retention eligibility, expected current
head, and a qualified processor capability/profile. The revision, finalized
receipt association and verifier evidence must agree on the verified digest and
byte length. This capability check is a preflight, not parser execution. Parsing
occurs after canonical acceptance;
parser failure does not alter the immutable `SourceRevision`, roll back
`CurrentSourceHead`, create a tombstone, change the disposition to opaque, or
make an older processed revision current.

`OPAQUE_ACCEPTANCE` requires a separate immutable approval for the exact
workspace, environment, source/root scope, recognized verified format, approved
purpose, security controls and retention applicability. An opaque revision may
become canonical current without becoming ordinary model context. It is not
parsed, projected, indexed, made searchable, retrieved through the Context
Broker, or disclosed to an agent or LLM. Opaque admission grants no `AccessPolicy`
permission. Unknown or ambiguous formats are not eligible for opaque acceptance,
and parser failure never creates opaque approval.

### Trusted canonical acceptance version selector

`TrustedCanonicalAcceptanceVersionSelector:v1` is an immutable, typed binding
that selects the acceptance schema for a trusted source-admission contract. Its
record carries `selector_id`, stable `selector_series_id`, immutable
`selector_version`, `schema_version`,
`workspace_id`, `environment_id`, `source_kind`, `source_contract_id` and
`source_contract_version`, `authority_kind`, exact approved scope,
`required_canonical_acceptance_schema_version`, approval reference, predecessor
selector, audit reference, idempotency identity and creation time. The
provider-backed variant additionally binds `provider_kind`, logical
`provider_connection_id`, exact connection/approval version and
`resource_scope_reference`; the upload variant binds the exact upload authority
reference. Both variants reference immutable root/content policy identities,
versions and digests. The selector references existing approved authorities;
it grants no read, processing, disclosure or deletion permission, does not
replace root/content approval or `AccessPolicy`, and cannot be issued or widened
by a connector worker.

PostgreSQL owns the versioned current selector head for each exact selector
series. The head is keyed by `workspace_id`, `environment_id` and
`selector_series_id` and carries `current_selector_id` and `cas_version`. A
series is scoped by workspace, environment, source kind and
source-contract identity/version, authority kind, exact resource scope, and
logical provider connection when applicable. Immutable selector successors are
selected through a compare-and-swap head that retains its version history.
Server-side resolution against persisted source provenance must find exactly
one applicable current selector. Before commit, the processing admission's
`selector_id`, source/scope bindings and exact root/content policy
identity/version/digests must match that resolved selector and its current head.
Missing, unsupported, stale or ambiguous selectors fail closed; overlapping
selectors with conflicting version or policy bindings are denied rather than
selected by recency or caller choice. Production
approval roles and activation remain subject to their existing governance
approvals; this selector defines no new approver authority.

### Canonical acceptance versions and compatibility

Historical `CanonicalAcceptance:v1` records retain their original meaning.
`CanonicalAcceptance:v2` is a content-only acceptance contract for new content
subject to the S2 processing contract. Its explicit
`canonical_acceptance_schema_version` is `2`, and it requires an exact,
immutable `processing_admission_id` bound to the same workspace, environment,
source, revision and finalized receipt association. The linked processing
admission records the exact selector ID used. Missing or mismatched processing
admission denies v2 content acceptance. The trusted selector, not the caller,
chooses the version; new Bitrix24 S2 content cannot downgrade to v1.

New v1 compatibility writes are permitted only under an explicit approved
compatibility selector for that source contract. Their immutable admission and
audit lineage records the selector used. Google v1 remains supported by its
exact compatibility contract; Yandex and filesystem writers require their own
verified compatibility contracts. Historical records are replayed according to
their persisted schema and provenance semantics, without consulting a later
current selector. An accepted v2 replay follows the `selector_id` in its linked
immutable processing admission; it never substitutes the current selector head.
Historical records are not backfilled with fabricated selectors or
processing approvals, and absence of a processing link is never a general
legacy permission.

Tombstones do not carry or require `SourceProcessingAdmission`. Historical v1
tombstone semantics remain intact. Processing approval does not authorize
deletion. A future tombstone-specific authorization and canonical acceptance
variant/version is governed separately by Issue #720; this amendment reserves
that compatible extension point without selecting its schema or version.

### Canonical current-authority transaction boundary

Before a new S2 content acceptance commits, its canonical PostgreSQL transaction
boundary revalidates and serializes all applicable current authority: Provider
Connection version, authorization/configuration generations and exact scope;
current selector head and root/content policy; required `AdmissionDecision` and
the current `AccessPolicy` reference where the canonical content contract
requires it;
processor qualification or exact opaque approval; lifecycle and retention
eligibility; exact immutable revision, finalized receipt and verifier evidence;
and the expected `CurrentSourceHead`. A current-authority read without
serialization against concurrent revocation or supersession is insufficient.

Authority mutation and acceptance use compatible serialization points. If
revocation or supersession commits first, stale acceptance is rejected. If
acceptance serializes first, its canonical history remains immutable and the
later restriction is enforced by lifecycle, projection and disclosure guards.
The processing admission, canonical acceptance/link, current-head CAS, required
Audit Event, outbox intent and idempotency result commit atomically within the
canonical PostgreSQL boundary. This does not promise a distributed transaction
with a provider, object storage, verifier or parser; provider reads, downloads,
verification and parsing remain outside the database transaction. Implementations
must coordinate every authority writer with this boundary. The ADR specifies
this serialization invariant, not a particular SQL lock order.

Known transaction aborts may be retried only after rollback and full current
authority revalidation, with the same idempotency key and command digest and a
bounded retry policy. An unknown commit outcome is reconciled by that exact key
and digest; changed payload under the same key conflicts. Missing or unavailable
authority fails closed. Audit, outbox and idempotency remain in the canonical
transaction as required by ADR-0020.

### Projection, retrieval and disclosure

Projection request creation suppresses processing work for opaque revisions.
Build excludes opaque bytes before parser, projection adapter or index writer
receives them. Activation and retrieval validate exact source head, canonical
acceptance and processing disposition. Citation validation, cached-result reuse,
Context Broker retrieval and final disclosure repeat currentness and disposition
checks. If an opaque revision becomes current, every earlier processed
projection is historical and cannot be served as current or substituted into
ordinary model context. Uncertain currentness or authority fails closed.

`AccessPolicy` remains the sole positive disclosure authority. Lifecycle,
retention and applicable holds are additional deny constraints. Historical
inspection remains a separately authorized flow and does not make a revision
current or ordinary model context. Projection rollback remains a new successor
publication and never rolls back canonical source truth.

### Governed reprocessing

Reprocessing reuses the exact `SourceRevision` and existing `ProcessingReceipt`,
`ProcessOnlyOperationLineage` and projection publication contracts. It does not
rewrite the original processing admission or canonical acceptance, invent a
provider revision, or make a historical revision current. A repeat processing
operation records its exact processor/profile and current policy,
lifecycle/retention and processing authority in the governed operation lineage
and is idempotent by operation identity and payload. Processing a revision whose
original disposition was opaque requires separate exact authorization. That
authorization may permit parser execution for the exact revision and creation
of immutable process-only receipts and artifacts in the governed operation
lineage. The artifacts remain process-only: they do not create a projection or
index, enter search or model context, or grant `AccessPolicy`. Any protected
inspection or disclosure remains subject to the existing positive authorization
and final revalidation contracts.

The original `OPAQUE_ACCEPTANCE` remains immutable and continues to exclude that
revision from projection request, build and activation. Reprocessing
authorization does not lift that exclusion; a separately granted `AccessPolicy`
or publication approval cannot override it. Therefore the same revision is not
eligible for projection publication or ordinary model disclosure under this
amendment, even after process-only artifacts exist. A future change that permits
publication from an originally opaque revision requires a separately reviewed
versioned contract decision; this ADR defines no such transition or competing
current-processing pointer. No old-current fallback is permitted.

### Persistence and rollout obligations

Persistence changes are additive: immutable processing-admission and selector
records, versioned current selector heads, and an explicit canonical acceptance
schema discriminator and exact processing reference for v2 content. Scoped
references and mode-specific constraints must prevent tenant/source/revision or
receipt substitution. Historical v1 rows retain their meaning and receive no
fabricated evidence. Readers and replay paths that understand v1 and v2, plus
explicit compatibility selectors for continuing legacy writers, precede v2
writers. Rollback may disable v2 writes but must preserve and correctly interpret
accepted v2 records; v2 history is never rewritten or silently read as v1.

The exact PostgreSQL constraints, migration order, mixed-version deployment,
rollback/recovery, lock coordination and deterministic concurrency and
projection/disclosure tests are implementation obligations. This ADR does not
claim that selector persistence, processing-admission persistence, migrations or
the shared serialization protocol are already implemented. The offline handoff
and implementation verification are tracked by Issues #718 and #714. Production
roots, formats, processor qualifications, opaque ingestion and deployment remain
separately gated.

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
