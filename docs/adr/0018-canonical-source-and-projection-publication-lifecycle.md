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

## Exact-source tombstone authorization and versioned canonical linkage

### Context

Canonical source history represents verified removal through an immutable tombstone
`SourceRevision` and a `CurrentSourceHead` transition. The tombstone's
`deletion_provenance` records evidence lineage; it is not permission to change canonical
state. A provider observation, absence, timestamp, path/name/checksum match or
connection read grant alone does not authorize a tombstone.

ADR-0018 after PR #722 defines `SourceProcessingAdmission:v1` and content-only
`CanonicalAcceptance:v2`. Processing admission does not authorize deletion. This
amendment adds an independent deletion authority contract. `AccessPolicy` remains the
separate positive authority for disclosure.

### Decision

1. **Qualified deletion evidence.** A new canonical tombstone requires an immutable
   `QualifiedDeletionEvidence:v1` result from a versioned provider/source evidence
   profile. It binds workspace/environment, exact `SourceObject`, provider/source
   identity and namespace, exact connection and authorization/configuration generations
   when applicable, exact approved resource scope and scope-approval version/digest,
   observation/scan identity, verifier profile/version, canonical evidence digest, exact
   object identity, freshness, applicable ordering/completeness guarantees and
   identity-continuity result. It stores references and digests, not source contents,
   credentials, signed URLs, raw provider payloads or sensitive filenames.

   A positive `CONFIRMED_QUALIFIED_DELETION` result is distinct from outage, permission
   loss, incomplete or permission-filtered inventory, scope withdrawal, move-out, trash
   metadata, `DELETED_TYPE` alone, CRM relationship disappearance, OAuth/grant
   revocation, unknown identity continuity and reappearance. Those states may block
   reads or require review under their own contracts; none grants canonical tombstone
   authority. Evidence and approval are separate records.

2. **Yandex preservation and Bitrix24 restriction.** Retain the accepted Yandex Disk
   `YandexDiskRemovalPolicy` and strict confirmed two-pass complete inventory protocol,
   including same ordered full-inventory digest, expected count, exact coverage and
   current workspace/environment/connection/scope approval. A provider-neutral evidence
   profile cannot weaken or bypass this gate. Bitrix24 permanent-deletion signal,
   complete inventory, permission-filtered absence and identity continuity remain
   NOT_VERIFIED for production. Synthetic deterministic evidence may test offline
   contracts; this ADR authorizes no live Bitrix24 calls or production deletion.

3. **Policy approval and trusted issuance.** A distinct `SourceDeletionPolicyApprover`
   is a verified canonical human identity with current workspace/environment-scoped
   authority to approve an immutable exact deletion-policy proposal. The approval binds
   policy/version/digest, exact source contract/scope, approver identity and the
   authority generation used to validate the approver. Actual production role mapping,
   delegation, separation of duties, legal/retention authority and source allowlists
   remain separately approved governance inputs.

   A `TrustedTombstoneAuthorizationIssuer` is a Spine-owned source-governance service
   principal. It may issue only after resolving the current immutable approval, current
   scope/connection authority, a positive qualified evidence result and exact expected
   source head. The issuer cannot approve policy, qualify evidence by assertion, broaden
   scope or accept caller-selected authority. A qualified evidence verifier validates
   evidence only. A canonical commit verifier rechecks persisted authority inside
   PostgreSQL. Provider observation workers submit observations/references only and
   cannot issue authorization. Provider read scope, S2 content admission, `AccessPolicy`
   and `SourceProcessingAdmission` grant no deletion authority.

4. **`TombstoneAuthorization:v1`.** This frozen, immutable, content-free artifact binds
   exactly one transition: authorization ID/schema version; workspace/environment; exact
   SourceObject and source identity; provider kind/connection version and generations
   when provider-backed; exact object namespace and resource scope; current scope
   approval and source-scope policy versions/digests; qualified evidence
   ID/profile/version/digest and observation identity; exact qualified object identity;
   current deletion-policy approval/version/digest and approver authority; immutable
   issuance decision; current revocation/supersession head; expected current revision,
   canonical acceptance ID and head CAS version; exact tombstone revision and transition
   ID; applicable retention eligibility and legal-hold check references/generations;
   idempotency key/command digest; audit reference; issue/expiry times and
   predecessor/supersession lineage.

   Source identity uses the existing canonical `SourceObject` identity tuple.
   Provider-specific variants are explicit; absence of provider fields never infers
   another authority mode. The authorization is single-use for its exact head transition
   and cannot be transferred to another source or reused as a new transition.
   Revocation/supersession appends a decision and advances the current authority
   pointer; it never edits accepted authorization or canonical history.

5. **Deletion version selector and acceptance v3.** New governed tombstones use an
   explicit persisted, tombstone-only `CanonicalAcceptance:v3`. It requires
   `revision_kind=tombstone`, an exact `TombstoneAuthorization:v1` ID/digest and
   matching source/head/transition references; it forbids processing-admission and
   content-policy links. `CanonicalAcceptance:v2` remains content-only and requires its
   exact `SourceProcessingAdmission:v1`. Historical v1 records retain their persisted
   original meaning. Unknown schema versions fail closed for active/current use; no
   nullable authorization link or missing field is a version selector; no historical
   authorization is fabricated or backfilled.

   A separate immutable `TrustedCanonicalTombstoneVersionSelector:v1` selects a
   tombstone writer mode for an exact source contract/scope. It is distinct from
   `TrustedCanonicalAcceptanceVersionSelector:v1` for content processing and grants no
   deletion authority. It binds source/provider contract, scope, required schema mode,
   immutable approval reference, predecessor, audit/idempotency identity and validity
   interval. Connector workers cannot choose or widen the selector.

   Its modes are `LEGACY_V1_ALLOWED`, `V3_REQUIRED` and `DENY_NEW`. `LEGACY_V1_ALLOWED`
   is an explicit, source-contract-specific, approved and time-bounded compatibility
   mode. It may permit a new v1 transition only while its selector is current and
   unexpired, and only when that exact contract has current deletion-policy approval,
   qualified evidence, expected-head fencing and audit lineage. It does not create a v3
   authorization or generalize historical semantics. When it expires without an approved
   successor, new writes become `DENY_NEW`. `V3_REQUIRED` forbids every new v1 commit;
   v3 failures never fall back. `DENY_NEW` blocks new transitions but retains historical
   reads/replay. A source contract advances to `V3_REQUIRED` only after all writers and
   readers are v3-capable. Exact historical replay is allowed by its stored idempotency
   identity and original immutable record and does not consult a later approval.

6. **Same-UoW verification and current-head CAS.** Before v3 tombstone commit,
   PostgreSQL verifies trusted tenant/environment; exact
   source/provider/connection/scope; current connection and authorization/configuration
   generations; positive qualified evidence and freshness; current deletion-policy
   approval and issuer authority; no revocation/supersession; lifecycle, retention and
   applicable legal-hold eligibility; expected current revision + acceptance +
   `cas_version`; exact tombstone transition; and idempotency key/digest. Missing,
   unavailable, stale, expired, unsupported or mismatched required authority fails
   closed as `TOMBSTONE_NOT_AUTHORIZED` or an existing typed safe denial.

   The immutable authorization, v3 canonical acceptance/link, `CurrentSourceHead` CAS,
   required Audit Event, outbox intent and idempotency result commit within one
   PostgreSQL Unit of Work. Provider calls, inventory scans, external storage and
   network I/O remain outside that transaction.

7. **Authority serialization.** Current deletion-policy, connection, scope and relevant
   lifecycle authority mutations use serialization points compatible with the canonical
   commit and #718 current-authority invariant. If revocation/supersession serializes
   first, stale tombstone commit is denied. If tombstone commit serializes first,
   immutable canonical history remains and later restrictions are enforced by lifecycle,
   projection and disclosure guards. This ADR defines the invariant, not a concrete SQL
   lock order. Implementation inventories every mutation path and proves a compatible
   lock/fence protocol before asserting one.

8. **Idempotency and unknown commit.** Authorization is bound to one exact command key
   and digest. Exact duplicate replay returns the original stable result without a
   second effect; changed payload under the key conflicts. A later revocation does not
   retroactively erase a committed result. Known deadlock/serialization aborts may be
   retried only after rollback, bounded and with the same key/digest plus complete
   current-authority revalidation. Unknown commit is reconciled using the original
   idempotency identity/result receipt; a lost response never creates a fresh tombstone
   command. Required audit/outbox failure aborts the transition. No numeric production
   retry budget is set here.

9. **Historical compatibility and rollout.** Historical Google/Yandex content and
   tombstone v1 remain interpretable; historical replay does not consult present
   approval and no fabricated approvals are backfilled. Existing Yandex policy remains
   intact. New governed writes use v3 after explicit enforcement activation.
   Reader-first additive rollout supports v1/v2/v3 before any v3 writer. A per-contract
   explicit selector controls v1 compatibility; selector absence denies new writes.
   Rollback may disable v3 writes but retains v3 readers, schema and accepted records.
   Rollback never rewrites canonical history.

10. **Projection, retrieval and disclosure.** `SourceRevision` history remains
   immutable; accepted tombstone advances `CurrentSourceHead`. Older projection cannot
   become current. Projection resolution, Context Broker retrieval, citations,
   cached-result reuse and final disclosure revalidate exact current head and
   acceptance. PostgreSQL currentness denial is primary; asynchronous cache purge is
   supplementary. `AccessPolicy` is not automatically changed and remains the positive
   disclosure authority.

11. **Lifecycle and physical cleanup separation.** Scope withdrawal, move-out,
   `DisclosureHold`, legal hold and retention expiry do not authorize tombstone.
   Retention and legal eligibility are separately governed inputs; this ADR sets no
   period or legal conclusion. Canonical tombstone does not trigger provider mutation or
   physical deletion. Physical source/artifact cleanup requires its own current
   approval, hold-aware checks and idempotent reconciliation. Reappearance requires
   governed content authority and qualified identity continuity referencing the exact
   tombstone; no silent resurrection or AccessPolicy transfer.

12. **Offline verification and operational limits.** Offline implementation uses
   deterministic synthetic evidence and fake-first contract tests for exact binding, all
   non-authorizing outcomes, worker self-authorization, revocation, CAS, v1/v2/v3
   dispatch, replay, unknown commit, audit/outbox failure, cross-tenant isolation,
   Yandex preservation, Bitrix evidence rejection, reappearance and cache lag. Isolated
   PostgreSQL integration must qualify RLS, composite FKs, immutability, same-UoW
   effects, authority races, CAS, idempotency, unknown commit and reader-first
   migration/rollback. Fakes do not prove SQL atomicity. Audits and diagnostics contain
   only safe references/digests and bounded reason codes, never content, secrets,
   provider payloads or sensitive names. Offline contract approval is not production
   Security/Operations approval, provider qualification, deployment, retention/cleanup
   approval or authorization to execute #679.

13. **Independent processing boundary.** `SourceProcessingAdmission:v1` remains content
   processing authority only; it does not authorize deletion.
   `TombstoneAuthorization:v1` does not authorize content processing or disclosure.
   `CanonicalAcceptance:v2` remains content-only. `AccessPolicy` remains independent
   positive disclosure authority. These contracts are not merged.

### Consequences

The current runtime writer does not implement this contract. New v3 writers remain
blocked until the separate implementation and PostgreSQL gates pass. Existing v1
readers/replay remain intact; any new v1 writer requires explicit approved selector
mode. This amendment changes no provider behavior, production source allowlist,
retention period, legal authority or physical cleanup policy.

## Provider-neutral source lifecycle eligibility authority

Provider observations and governance decisions about source lifecycle are persisted
separately from canonical source state. A `LifecycleObservation:v1` is immutable,
provider-neutral evidence bound to the exact workspace, environment, `SourceObject`,
connection and connection version, approved scope version/digest, provider identity,
and evidence reference/digest. An observation alone changes no eligibility. Uncertain,
incomplete or inaccessible provider evidence cannot be converted into a deletion,
disclosure hold or legal hold.

An authorized `LifecycleDecision:v1` references one exact observation and the current
versioned governance authority. It may govern read eligibility, disclosure review,
retention applicability or legal-hold state only within its recorded source and scope.
Immutable decisions advance a tenant-scoped `CurrentLifecycleAuthority` through an
expected-current compare-and-swap. Decision authorization, current-pointer mutation,
Audit Event and outbox intent commit in one PostgreSQL unit of work. Workers may
produce observations but cannot authorize their own decisions; current governance
must be revalidated in the same transaction. Replays are idempotent, stale heads and
authority fail closed, and cross-workspace or cross-environment reads remain isolated.

Canonical deletion remains governed only by the versioned tombstone authorization
and canonical acceptance contract above. A lifecycle decision cannot write
`TOMBSTONED`, mutate `CurrentSourceHead`, or authorize physical deletion. The current
eligibility reader resolves the canonical head and current lifecycle decision in one
database statement: an exact canonical tombstone overrides ordinary lifecycle status,
while a non-tombstoned source requires an exact persisted lifecycle decision binding.
The returned tombstone retains the matching decision's connection/scope binding and
legal-hold state. No decision, stale scope/version or mismatched connection yields a
content-free unavailable result. In-memory implementations follow the same override
and binding rules.

`LifecycleObservation:v1`, `LifecycleDecision:v1` and current lifecycle authority are
new records under this amendment; none rewrites historical source observations,
canonical acceptance, tombstones or projection publication history. The projection,
retrieval and final-disclosure boundaries continue to treat canonical current
tombstone as the authoritative deletion fence and lifecycle eligibility as an
additional deny constraint.
