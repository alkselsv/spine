# Issue #6: Durable Original Document Storage

Status: local specification; not published to GitHub.

Originating issue: GitHub Issue #6, `[R0 blocker] Implement durable original document storage`.

## Problem Statement

Spine must preserve the exact bytes observed during document admission so that
parsing, stable locators, evidence, projection rebuilds, deletion handling and
historical inspection can be tied to an immutable `SourceRevision`. The current
repository has an empty object-storage package and a compatibility ingestion path
that reads arbitrary local paths directly into memory and sends extracted text to
Cognee. That path is a prototype, not durable original storage: it has no
canonical receipt, tenant boundary, integrity contract, crash recovery, orphan
reconciliation or controlled deletion lifecycle.

Issue #8 provides the PostgreSQL runtime, Unit of Work and transactional outbox
foundation. Issue #14 and ADR 0018 define the authoritative SourceRevision,
canonical acceptance, current-revision, tombstone and `as_of` semantics. Issue
#7 remains open and owns the eventual `SourceObject`, `SourceRevision`, Access
Policy and projection records. This specification therefore defines the
replaceable byte-storage boundary and its coordination protocol, without
implementing those records or the ingestion pipeline.

## Solution

Introduce an application-facing `OriginalObjectStore` port for immutable byte
objects. The port accepts a server-issued opaque object identity, streams bytes
into isolated staged storage, verifies a declared SHA-256 digest and returns
provisional physical-storage evidence. A successful finalization publishes the
object exactly once under an immutable reference. Raw physical reads are
available only to trusted application services through an internal port; API
handlers, tools, model-facing code, citations and arbitrary callers cannot call
that port. An authorized original-content read service accepts a trusted
Workspace/Environment context and SourceRevision identity, resolves the
PostgreSQL association, evaluates current Access Policy and lifecycle state, and
only then obtains a short-lived internal read capability. The adapter never
evaluates ACLs, chooses a current revision, interprets tombstones, or grants
disclosure authority.

For the approved R1 single-deployment target, the recommended adapter is a
dedicated local filesystem storage root on a persistent volume, provided the
deployment accepts and verifies the platform-specific durability assumptions
listed below. PostgreSQL remains the canonical source for receipt/reference
metadata, SourceRevision association, authorization and lifecycle state. An
S3-compatible adapter is a later migration path, not an R1 infrastructure
requirement.

The protocol deliberately avoids a distributed transaction. The application
persists provisional storage evidence only after the object has been durably
finalized and verified. PostgreSQL may then persist a durable SourceRevision
observation and its receipt/reference association; that observation may remain
unadmitted. The canonical owner records append-only admission disposition
separately, and a later ADR 0018 transaction controls canonical acceptance and
current-revision publication. Object storage never admits, accepts, publishes or
selects a current revision. If PostgreSQL rolls back, the object is recoverable
as an unassociated finalized object and reconciliation may remove it only under
an approved cleanup policy. If finalization has not completed, no PostgreSQL
reference is valid. Any missing, mismatched or ambiguous object causes
fail-closed behavior for content reads and projection.

## Architectural Context and Authoritative Sources

The specification follows these repository sources:

- `AGENTS.md`, `CONTEXT.md` and `docs/GLOSSARY.md` for boundaries and domain
  vocabulary.
- `docs/ARCHITECTURE.md` for PostgreSQL authority, admission isolation,
  immutable originals, source identity, provenance, tombstones and the R1
  ingestion sequence.
- `docs/ROADMAP.md` for the R1 Q&A slice and the requirement for reliable upload,
  source versions, deletion and rebuild behavior.
- ADR 0001 for PostgreSQL as canonical store and the Context Graph as a
  rebuildable projection.
- ADR 0004 for atomic PostgreSQL mutation plus outbox intent and at-least-once
  delivery.
- ADR 0011 for Access Policy as the document-content authorization authority.
- ADR 0018 for immutable SourceRevision, canonical acceptance, tombstone,
  current-revision and `as_of` semantics.
- The accepted Issue #8 specification for PostgreSQL, tenant context, Unit of
  Work, error translation, test harness and the absence of a distributed
  transaction with object storage.
- Issue #14's accepted resolution as represented by ADR 0018.

Repository facts verified for this specification:

- `src/spine/infrastructure/object_storage/__init__.py` is only a description;
  it contains no storage port or adapter.
- `src/spine/ingest/loaders.py` is a compatibility loader for paths and returns
  extracted `Document` values. It reads text eagerly and uses regular workbook
  loading for XLSX; it does not preserve original-byte receipts.
- `src/spine/api/main.py` exposes a legacy `/ingest` endpoint that accepts a
  caller-provided path and calls the compatibility loader/Cognee path directly.
  Issue #6 does not silently replace this endpoint.
- Issue #8 supplies framework-independent persistence contracts, an in-memory
  adapter and a mandatory real PostgreSQL harness. It does not yet supply
  SourceObject or SourceRevision repositories.
- No production SourceObject, SourceRevision or object-storage implementation
  is present in the current tree.

## Ownership and Trust Boundaries

| Concern | Owner | Rule |
| --- | --- | --- |
| Original bytes and staged/finalized physical objects | Issue #6 adapter | Stores bytes and verifies physical integrity only. |
| Opaque object reference and write receipt schema | Issue #6 application/domain contract | Stable, typed, provider-neutral and non-disclosing. |
| Workspace/Environment scope | Issue #8 persistence context and trusted application boundary | Never selected from an object reference or filename. |
| SourceObject identity and SourceRevision history | Issue #7 / PostgreSQL | Object storage does not create, merge or select source identity. |
| Access Policy and content disclosure | Issue #7 / application policy boundary under ADR 0011 | Must be evaluated before a read stream is opened. |
| Admission orchestration | Issue #3 | Consumes this port; this specification does not define the pipeline. |
| Canonical audit semantics and delivery diagnostics | Issue #4 | Receives minimal references/diagnostics through its contracts. |
| Durable composite lifecycle | Temporal boundary from ADR 0010/Issue #11 | Object-store retries are bounded idempotent work, not a workflow engine. |
| PostgreSQL receipt/reference metadata | Issue #8 Unit of Work plus Issue #7 repositories | Canonical authority; object storage is a projection of those references. |

Possession of an `ObjectReference` is not authorization. A caller may present a
valid reference and still be denied because the current policy, Workspace,
Environment, SourceRevision disposition or tombstone state does not permit
content disclosure. The same applies to an internal read capability: it is a
short-lived, non-serializable application value, not a reusable grant, and it
does not survive policy revocation or an authorization-version mismatch.

## Terminology

- **Original object**: the immutable byte sequence stored for one observed
  source revision.
- **Staged object**: an incomplete, non-readable-to-ordinary-consumers upload
  identified by a temporary upload identity.
- **Finalized object**: a verified immutable byte object available to an
  authorized application read.
- **ObjectReference**: a provider-neutral opaque locator for one finalized
  object, not a path, URL, bucket key or filesystem detail.
- **WriteReceipt**: immutable provisional physical-storage evidence returned by
  the store after a successful staged write/finalization, including object
  identity, byte count and verified digest.
- **Receipt/reference record**: PostgreSQL metadata associating a finalized
  object with a SourceRevision. It is canonical metadata, not a copy of the
  bytes.
- **Orphan**: a staged or finalized physical object with no valid, current
  PostgreSQL association, or a PostgreSQL association whose object is absent or
  invalid.

## Domain and Application Contracts

The contracts are framework-independent. Public contract models are immutable,
typed, reject unknown fields (`extra="forbid"` when implemented with Pydantic),
and contain no provider-specific path, bucket, URL or credential.

### Object reference

The conceptual schema is:

```text
ObjectReference
  schema_version: positive integer
  object_id: UUID                  # generated by Spine, never derived from filename/digest
  storage_generation: opaque UUID  # one immutable physical byte generation
  digest_algorithm: "sha256"
  digest_hex: 64 lowercase hex characters
  byte_length: positive integer or zero
```

`object_id` is the logical identity assigned by the application. The reference
must not expose a physical path, storage root, provider name, bucket, signed
URL, access token or content bytes. `storage_generation` is unique within the
storage adapter's logical object namespace, is stable for one immutable physical
byte generation, and is never reused for different bytes. An exact restore that
preserves the same immutable bytes and generation preserves the generation; a
repair that writes a new physical generation creates a new generation and a
successor reference/receipt record under Issue #7 lifecycle rules before content
can be read, without mutating an immutable historical receipt. A reference is usable
only when its complete value matches a PostgreSQL canonical receipt and the
authorized read service has approved the associated revision. Generation
equality is part of reference and receipt equality; it is not a physical path,
provider identifier or authorization token.

The reference is not a SourceObject ID and is not a SourceRevision ID. The same
digest may appear in many references for different tenants, SourceObjects and
SourceRevisions. No digest-only lookup may merge provenance or authorization.

### Upload command and write receipt

The application-facing upload command contains:

```text
OriginalUpload
  operation_name: stable identifier # Issue #8 operation identity
  operation_schema_version: version
  upload_id: UUID                  # Issue #8 caller-supplied idempotency key
  request_digest: PreWriteRequestDigest
  object_id: UUID                  # server-issued logical object identity
  workspace_id: UUID               # trusted context, not caller authority
  environment_id: UUID | null
  expected_sha256: lowercase hex | null
  expected_byte_length: non-negative integer | null
  declared_media_type: bounded string | null
  declared_filename: bounded display metadata | null
  purpose: bounded server-selected identifier
```

Filename and media type are hints for validation and display; neither becomes a
storage path or proof of content. The application calculates the digest while
streaming and treats a client declaration as an expectation to verify, not as
trusted metadata. The normalized filename is display/provenance metadata owned
by Issue #3/#7 and is not part of the Issue #6 pre-write request digest unless those
contracts explicitly make it immutable command meaning.

The request-digest value is:

```text
PreWriteRequestDigest
  algorithm: "sha256"
  algorithm_version: Issue #8 canonical digest version
  digest_hex: 64 lowercase hex characters
```

`OriginalUploadCommandPayload` is the validated semantic payload known before
opening or resuming the stream:

```text
OriginalUploadCommandPayload
  workspace_id: trusted UUID
  environment_id: UUID | null
  purpose: server-selected identifier
  object_id: UUID
  expected_sha256: lowercase hex | null
  expected_byte_length: non-negative integer | null
```

The `request_digest` is Issue #8's canonical request digest: SHA-256 over its
explicitly versioned canonical byte representation containing the operation
name, operation schema version and this validated semantic payload. Issue #6
reuses Issue #8's canonicalizer, digest algorithm/version and cross-version test
vectors; it does not define a second serialization. UUIDs, enums, timestamps,
lists, object keys, binary references and unsupported values follow the exact
Issue #8 rules. Trace IDs, timestamps used only for execution, retry counters,
transport chunking, physical/provider data and display filename are excluded.

The pre-write request digest excludes observed content digest, observed byte
length and any validated content classification produced only after streaming
or inspection. A later admission field that changes immutable command meaning
requires coordination with Issue #8's versioned command contract; it cannot be
silently appended to this digest.

The distinct post-stream observed content identity is:

```text
ObservedContentIdentity
  observed_sha256: lowercase hex
  observed_byte_length: non-negative integer
```

It is SHA-256 calculated from the actual streamed bytes and their observed
length. It is not available when the pre-write request digest is calculated.

`ValidatedContentEvidence` is a bounded, versioned result produced by Issue #3's
security/format validation after streaming, or by its approved adapter. Issue #6
may transport it as typed provisional evidence but does not own its validation
rules. It does not alter the already accepted pre-write request digest; a
conflicting result is a typed admission/observation conflict and fails closed.

Its conceptual shape is:

```text
ValidatedContentEvidence
  schema_version: positive integer
  producer: Issue #3-owned validator/version reference
  content_class: bounded versioned value
  outcome: valid | quarantined | rejected
```

The immutable receipt is:

```text
WriteReceipt
  schema_version: positive integer
  upload_id: UUID
  request_digest: PreWriteRequestDigest
  object_reference: ObjectReference
  observed_content: ObservedContentIdentity
  validated_content_evidence: ValidatedContentEvidence | null
  verification: IntegrityResult
  finalized_at: server timestamp
```

The opaque Issue #8 result reference resolves to this storage-owned operational
state:

```text
UploadCommandState
  issue8_receipt_id: opaque UUID
  request_digest: PreWriteRequestDigest
  state: staging_available | finalized | aborted | integrity_conflict
  provisional_receipt: WriteReceipt | null
```

This state is not a second idempotency identity. Its identity and replay
behavior come from the Issue #8 receipt; it only records the progress and
verified result of the physical upload command.

`WriteReceipt` is provisional physical-storage evidence returned by the
adapter. It is not a PostgreSQL command receipt and does not prove SourceRevision
admission, canonical acceptance, currentness or authorization. Receipt
persistence is separate from physical writing: the adapter returns a successful
provisional receipt only after finalization and verification, and PostgreSQL
becomes the canonical owner only when it persists the receipt/reference
association and command receipt.

The canonical PostgreSQL command/observation records jointly contain the Issue
#8 operation identity, operation schema version, `upload_id`, pre-write request
digest, observed content identity, workspace/environment scope, object
reference, SourceRevision association and its lifecycle disposition as owned by
Issue #7. The Issue #8 idempotency receipt itself owns the key, request digest
and opaque result reference; the linked `UploadCommandState` and
receipt/reference association carry the observed facts. Together they
distinguish requested expectations from observed facts. Repeated commands with
the same `upload_id` and request digest reuse the existing verified result; any
changed pre-write semantic field is an Issue #8 idempotency conflict. A
different observed content identity after finalization is a typed
content/idempotency conflict and never replaces the finalized object.
PostgreSQL owns these durable records; the adapter receipt alone never does.

### Integrity result

```text
IntegrityResult
  status: verified | mismatch | unavailable | indeterminate
  algorithm: sha256
  expected_digest: digest | null
  observed_digest: digest | null  # same fact as WriteReceipt.observed_content.observed_sha256
  expected_length: integer | null
  observed_length: integer | null  # same fact as WriteReceipt.observed_content.observed_byte_length
```

Only `verified` permits a finalized receipt to be associated with a content
SourceRevision. `mismatch`, `unavailable` and `indeterminate` are non-success
outcomes. The application must not infer that unavailable verification means
verified. `observed_digest` and `observed_length` are projections of the one
`ObservedContentIdentity`, not a second content-identity contract.

### Internal storage port and authorized read service

`OriginalObjectStore` is an infrastructure/internal port used only by trusted
application services. Its raw physical read operation is not exposed to API
handlers, tools, model-facing code, citations or arbitrary callers. The
application composition root wires this port only into the upload,
reconciliation and authorized-read services.

The authorized original-content read service is the public application seam. It
accepts a trusted persistence context and `SourceRevision` identity, never a
caller-supplied `ObjectReference`. It resolves the PostgreSQL
receipt/reference association, evaluates current Access Policy, tombstone,
admission/disposition, retention and disclosure state, and fails closed for
missing, stale, conflicting or ambiguous state. The trusted application read
gateway owns grant issuance, current-version revalidation, replay prevention
and the only call path to the adapter's raw read primitive. Only after its
immediate revalidation does it consume a one-shot grant and ask the internal
port for a stream.

```text
AuthorizedOriginalReadGrant (opaque, non-serializable)
  grant_id: process-local opaque value
  workspace_id: UUID
  environment_id: UUID | null
  source_revision_id: UUID
  object_reference: ObjectReference
  purpose: server-selected identifier
  authorization_decision_version: immutable policy/version identifier
  expires_at: server timestamp
```

`AuthorizedOriginalReadGrant` is minted only by the authorized read service or
trusted gateway. Its fields are binding metadata, not a reconstructible DTO:
callers cannot construct a valid grant by copying them. It is stored only in a
process-local one-shot registry or equivalent opaque mechanism, is consumed at
most once, and is rejected on replay, expiry, scope/generation mismatch or
authorization/lifecycle-version mismatch. It is never logged with sensitive
fields, persisted as durable authority, accepted from API/model/tool input or
returned to a caller. A process restart invalidates all outstanding in-memory
grants; no distributed capability service is introduced.

The authorized read protocol is:

1. Receive trusted Workspace/Environment context, SourceRevision identity and
   read purpose.
2. Resolve the canonical PostgreSQL receipt/reference association and evaluate
   current Access Policy, SourceRevision disposition, tombstone and disclosure
   state.
3. Capture the exact policy/lifecycle decision version.
4. Immediately before stream creation, re-read or atomically validate that the
   current policy/version, scope, SourceRevision lifecycle and object generation
   still match. This check occurs in the trusted gateway's Unit of Work or an
   equivalent documented PostgreSQL snapshot/lock boundary.
5. Mint and consume the one-shot opaque grant as one trusted gateway operation.
6. Open the internal storage stream only after successful consumption; any
   mismatch, replay, expiry, scope substitution or ambiguous state fails closed
   before content bytes are returned.

This boundary prevents new stream creation after a policy or tombstone change
observed before the revalidation/consumption point. It cannot recall bytes
already delivered by an already-open stream; the stream therefore has bounded
duration/size and its application read lease ends at the configured limit.
The service returns only a bounded content stream and safe metadata; it never
returns physical paths, storage credentials, signed URLs or a reusable grant.

The internal port exposes the following behavior, without prescribing method
names or provider types:

1. Begin or resume an upload by `upload_id` and trusted scope.
2. Stream bounded chunks into staged storage while calculating digest and length.
3. Abort an upload, making its staged bytes inaccessible to ordinary reads.
4. Finalize exactly once after successful validation, with overwrite prevention.
5. Return a verified provisional `WriteReceipt` or a typed adapter-neutral
   failure.
6. Open a bounded asynchronous read stream for a successfully consumed
   `AuthorizedOriginalReadGrant`; never expose raw reference reads to untrusted
   callers or return an unbounded in-memory byte array.
7. Verify an existing reference's length and digest through a bounded stream.
8. Enumerate only sanitized reconciliation metadata for a controlled operator
   or worker operation; enumeration is not a read authorization bypass.
9. Mark a reference eligible for controlled physical deletion only when the
   application supplies an approved deletion command. The adapter does not
   decide retention.
10. Execute or report a controlled deletion idempotently, with a result that
    distinguishes deleted, already absent, protected, and failed.

The port may expose a reconciliation cursor and an opaque adapter inventory
entry, but must never expose physical paths or provider credentials across the
application boundary.

### Error taxonomy

The port translates provider failures into stable categories:

- `InvalidObjectReference`: malformed, unsupported or internally inconsistent
  reference.
- `UploadConflict`: upload identity or finalized object conflicts with a
  different observed content identity or physical generation after the Issue #8
  request digest has matched.
- `UploadNotFound`: staged upload cannot be resumed or finalized.
- `IntegrityMismatch`: observed bytes do not match expected digest/length.
- `ObjectNotFound`: finalized object is absent.
- `ObjectUnavailable`: temporary capacity, I/O or provider unavailability.
- `ObjectCorrupt`: stored bytes fail verification.
- `ObjectAlreadyExists`: overwrite or generation collision was attempted.
- `ObjectProtected`: deletion is not approved or is prevented by lifecycle
  state.
- `ObjectDeletionFailed`: controlled deletion did not complete.
- `UploadLimitExceeded`: size, duration, chunk or resource limit exceeded.
- `UnsupportedMediaType`: content validation rejected the declared/observed
  type; this is not a MIME trust decision.
- `StorageConfigurationError`: adapter configuration is invalid or unsafe.
- `StorageIntegrityIndeterminate`: restart/recovery cannot prove state.

Errors crossing the application boundary contain category, safe retryability,
trace ID and stable sanitized detail. They exclude bytes, path, bucket, URL,
credentials, filename when sensitive, and raw provider messages.

## Adapter Responsibilities

The adapter owns staging mechanics, immutable finalization, bounded streaming,
digest calculation, local resource limits, provider error translation,
physical integrity checks, cleanup mechanics and adapter-specific observability.
The authorized read service/trusted application gateway owns PostgreSQL
association resolution, current-version revalidation, grant issuance and
one-shot consumption before it calls the adapter. It is the only component
registered or injected with the adapter's raw read primitive. The adapter may
reject an expired, malformed or already-consumed opaque grant, but it does not
interpret ACL policy or make a disclosure decision. It does not own
authentication, Access Policy, SourceRevision state, tenant selection,
tombstones, current revision, canonical acceptance, retention approval or
publication state.

### Recommended filesystem adapter

The R1 adapter uses a dedicated storage root on a persistent local volume. It
uses separate staged and finalized namespaces, opaque generated names, and a
format version. User filenames never participate in path construction. The
implementation must use a platform-supported atomic no-replace publication
primitive, or a protocol proven equivalent under the approved filesystem
profile. A vulnerable check-then-act sequence is not sufficient. A finalized
object is published only after the complete byte stream and its digest have been
verified.

The externally observable no-replace contract is:

- an absent finalized destination may be won by exactly one concurrent finalize;
- a destination containing the same verified generation returns the stable
  idempotent result without replacement;
- a destination containing different bytes or a different generation returns
  `ObjectAlreadyExists`/`UploadConflict` without replacement;
- no concurrent attempt exposes a partial readable finalized object;
- staged and finalized locations satisfy the same-filesystem and other
  assumptions required by the selected publication primitive;
- file-data synchronization precedes publication, and directory-entry
  synchronization follows publication according to the approved profile;
- crash tests cover each externally meaningful step and classify uncertainty as
  indeterminate rather than success.

The recommendation is conditional on an explicit deployment profile:

- a supported POSIX-like platform and filesystem with documented rename and
  durability behavior;
- staged and finalized paths residing on the same filesystem when atomic rename
  is relied upon;
- sufficient permission to synchronize file contents and the parent directory;
- a persistent volume whose backup agent includes both namespaces and their
  metadata;
- a restart/recovery procedure tested against the selected filesystem;
- no claim that local `fsync` and `rename` alone survive every machine,
  controller, filesystem, RAID, kernel or power failure.

The implementation must analyze and test all of these separately:

1. **File-content synchronization**: whether successfully writing and syncing
   the file makes the byte content recoverable after process restart.
2. **Parent-directory synchronization**: whether syncing the containing
   directory makes the name-to-inode publication durable after restart.
3. **Atomic publication**: whether readers see either no finalized object or
   the complete finalized object, never a partial replacement.
4. **No replacement**: whether the selected atomic no-replace primitive or
   equivalent protocol prevents a retry or attacker from replacing an existing
   finalized object without a check-then-act race.
5. **Process and machine/filesystem failure**: what the selected platform
   guarantees after process crash, abrupt restart, power loss or filesystem
   recovery, and which cases remain `indeterminate`.
6. **Backup/restore**: whether backups capture complete finalized objects,
   receipts and the association needed to verify restored data; partial restore
   must produce degraded/unavailable state, not disclosure.

If the deployment cannot prove the required profile, the adapter must be marked
unsupported or degraded and the system must fail closed for affected reads. An
S3-compatible adapter may later replace this adapter; it must implement the same
port and conformance suite, including its own documented conditional guarantees.

## Lifecycle

### Upload and verification

1. A trusted application boundary validates Workspace/Environment context,
   upload limits, idempotency identity and admission purpose before opening a
   storage stream.
2. The application rejects path-like storage identifiers and sanitizes display
   metadata. The adapter receives an opaque generated upload identity.
3. The stream is bounded by configured byte, duration, chunk, concurrent-upload
   and inactivity limits. Backpressure is applied; the entire document is never
   required in memory.
4. The adapter writes staged bytes, calculates SHA-256 and records length.
5. The application performs allowed security/format checks at the admission
   boundary. Issue #6 does not define those checks or the Issue #3 pipeline.
6. The adapter verifies the expected digest/length and atomically publishes the
   immutable generation under the filesystem profile, or returns a typed failure.
7. Only a verified `WriteReceipt` can proceed to PostgreSQL observation
   receipt/reference persistence. Later admission disposition and canonical
   acceptance remain separate ADR 0018 lifecycle decisions.

### Request and observed-content retry protocol

The pre-write request digest and post-stream observed content identity have
different lifecycles:

1. **First command.** The Issue #8 idempotency key (`upload_id`) and pre-write
   request digest are claimed in a short tenant-scoped Unit of Work. Because
   Issue #8 does not allow an owned idempotency claim to remain incomplete over
   a long byte stream, that transaction completes the receipt with an opaque
   `UploadCommandState` result reference. The state is a storage command state
   linked to the Issue #8 receipt identity, not a second idempotency key or
   competing canonicalization system. A newly owned state may begin staging.
2. **Retry before finalization.** The same key and request digest resolve the
   same `UploadCommandState` and resume or
   inspect the same staged upload. A changed pre-write semantic input produces
   the Issue #8 typed idempotency conflict. A retry never silently begins an
   unrelated second staged upload under the same key.
3. **Finalization.** The adapter calculates `ObservedContentIdentity`, compares
   it with supplied expected checksum/length when present, binds it to the
   upload identity, physical generation and validation result, and returns
   provisional evidence. This does not claim SourceRevision admission or
   canonical acceptance.
4. **Retry after finalization.** The same key, request digest and observed
   content identity reuse the stable verified result. A new stream under the
   same finalized upload identity cannot overwrite existing bytes. If a retry
   presents another stream, the implementation may consume and hash it to
   detect a difference; different observed bytes produce a typed
   content/idempotency conflict and the existing finalized object remains
   unchanged. The specification does not require buffering the attempted stream
   in memory.
5. **PostgreSQL association.** The Issue #8 command receipt stores the pre-write
   request digest and opaque `UploadCommandState` result reference; the linked
   state and receipt/reference association store the verified observed content
   identity/length. Together they explicitly distinguish requested expectations
   from observed facts. A crash after observed digest calculation but before
   this transaction leaves a finalized orphan candidate that can be reused only
   when all identity and generation checks pass.

`UploadCommandState` is operational storage state associated with the completed
Issue #8 result reference. It records whether staging is available, finalized,
aborted or blocked by an integrity/content conflict, plus the provisional
receipt when one exists. Its state does not admit, reject, quarantine, accept or
publish a SourceRevision. A replay resolves this state reference and returns the
stable current result; it never creates a second command receipt.

### Read

The authorized original-content read service receives trusted
Workspace/Environment context and a SourceRevision identity. It resolves the
PostgreSQL receipt/reference association, verifies Workspace/Environment
isolation, current Access Policy, admission/disposition, tombstone/currentness,
retention and purpose, and fails closed on any ambiguity. Immediately before
stream creation it revalidates the current policy/lifecycle version, then mints
and consumes one `AuthorizedOriginalReadGrant` through the trusted gateway. The
internal port opens a bounded stream only for that successfully consumed opaque
grant. Authorization is not delegated to the adapter, a raw reference read is
not an application entry point, and a reference or grant alone is not
continuing authority after policy revocation.

### Verification

Verification may be performed during finalize, on receipt creation, after
restart, during reconciliation, before projection, during backup validation and
on an operator-requested integrity check. A mismatch or missing object fences
ordinary content use and yields a degraded diagnostic without exposing bytes.

### Retention and deletion

Logical tombstones are PostgreSQL SourceRevision state under ADR 0018. A
tombstone immediately fences ordinary disclosure and projection use; physical
bytes may remain for approved historical or compliance purposes. Physical
deletion is separate, explicit, idempotent and approval-gated. This
specification does not choose a retention duration, legal hold rule, or automatic
deletion schedule.

The application must prove that no current or retained historical association,
legal hold, backup obligation or active workflow requires the bytes before
issuing an approved deletion command. The adapter may refuse deletion when the
command lacks an approval proof or when state is ambiguous. Deletion results
are recorded through Issue #4 audit semantics and Issue #7 lifecycle metadata;
this specification does not redesign either subsystem.

## Interaction with PostgreSQL and SourceRevision

The intended sequence is:

1. Issue #3's future orchestration receives a source payload and asks Issue #6
   to stage and verify the original bytes.
2. Issue #6 returns a verified provisional `WriteReceipt`.
3. Issue #7's future repository persists the immutable SourceRevision
   observation, the receipt/reference association and the PostgreSQL command
   receipt in a Unit of Work. This durable observation may remain unadmitted;
   storage does not choose its admission disposition.
4. The canonical owner records the append-only admission disposition (for
   example accepted, rejected or quarantined) according to ADR 0018. The
   storage receipt does not imply or mutate that disposition.
5. Only the ADR 0018 canonical-acceptance transaction may advance the
   SourceObject current revision. Where downstream work is required, that same
   canonical transaction records the required minimal outbox intent under ADR
   0004. The intent contains identifiers and minimal metadata, never private
   bytes or storage credentials.
6. Commit makes only the effects of that PostgreSQL transaction visible.
   Parsing, projection and delivery remain asynchronous and cannot make storage
   or a SourceRevision canonical.

The physical object write and PostgreSQL transactions are not atomically
committed together. Physical finalize therefore precedes only the PostgreSQL
receipt/reference association that requires a retrievable original; it does not
precede or control every later SourceRevision lifecycle transition. A failed
observation transaction rolls back its command receipt, receipt/reference
association and observation effects together. A failed canonical-acceptance
transaction rolls back only the canonical acceptance/current-revision/outbox
effects in that transaction, leaving any previously committed observation and
its approved disposition governed by ADR 0018.

PostgreSQL is canonical for whether a reference is associated, the observation's
admission disposition and which SourceRevision is current. Object storage never
admits, accepts, publishes or selects a current revision. A finalized object
without a committed receipt is an orphan candidate, not a readable document. A
receipt without a verified object is an integrity failure, not permission to
retry arbitrary reads. Rejected or quarantined observations follow their
canonical retention and disclosure rules; their state is never inferred from
physical storage presence.

## Atomicity, Retry and Recovery Model

### State classes

| State | Canonical authority | Recoverable action | Ordinary content read |
| --- | --- | --- | --- |
| Upload command accepted | PostgreSQL Issue #8 command/idempotency receipt with pre-write request digest | Resume or abort by upload ID | No |
| UploadCommandState | PostgreSQL result reference linked to the Issue #8 receipt | Resolve staging/finalization/conflict state; never infer SourceRevision admission | No |
| Staged bytes | Object adapter, bounded by upload identity | Resume, abort or reconcile | No |
| Finalized but unassociated object | Adapter plus reconciliation inventory | Associate only through a matching canonical command, otherwise approved cleanup | No |
| Durable SourceRevision observation plus receipt/reference | PostgreSQL observation and command receipt | Reconcile against object and lifecycle records | Only after authorized read service |
| Admission disposition | Issue #7 canonical owner, append-only under ADR 0018 | Follow its approved disposition rules | Only if policy/disposition allows |
| Canonical SourceRevision acceptance/currentness | PostgreSQL canonical-acceptance transaction | Follow ADR 0018 lifecycle; never infer from storage | Only if current policy/state allows |
| Tombstoned association | PostgreSQL | Preserve history; approved deletion only | No ordinary read |
| Missing/corrupt referenced object | PostgreSQL says reference exists; adapter says unavailable | Repair from approved backup or rebuild association; otherwise degraded/fail closed | No |

### Failure/retry/recovery matrix

| Boundary/failure | Visible result | Safe retry | Recovery owner |
| --- | --- | --- | --- |
| Validation fails before staging | No object; typed rejection | Retry only with a new valid command or same idempotency key/content | Application/admission boundary |
| New command with no expected checksum | Issue #8 request receipt is claimed before staging; observed identity is unknown | Begin one staged upload; do not treat request digest as content identity | Application boundary |
| Client disconnects during staging | Staged or absent | Resume same upload ID if state is provable; otherwise abort/reconcile | Issue #6 worker/adapter |
| Retry before finalization, same request key/digest | Existing staged command state | Resume or inspect the same staged upload; never create an unrelated second upload | Application boundary |
| Retry before finalization, changed request digest | No new logical effect | Return Issue #8 idempotency conflict | Application boundary |
| Storage write fails | No successful receipt | Retry bounded transient failures; do not claim success | Issue #6 |
| Digest/length mismatch | Staged bytes quarantined/aborted; no receipt | Same command is a stable mismatch; changed content requires a new identity | Application and adapter |
| Process crashes before finalize | Staged bytes or unknown state | Reconcile by upload ID; never publish an unverified object | Issue #6 |
| Process crashes after finalize before receipt persistence | Finalized orphan candidate | Reuse only when digest, length, upload ID and generation all match | Issue #6 plus PostgreSQL reconciliation |
| Observation/receipt transaction rolls back after object finalize | Finalized orphan candidate; no durable observation or association | Retry the PostgreSQL observation operation with same idempotency key; do not rewrite bytes | Issue #7/application |
| PostgreSQL commits receipt/reference but outbox delivery fails | Durable observation/reference remains; any committed intent is pending | Replay outbox; no second object write | Issue #4/outbox worker |
| Canonical acceptance/current-revision transaction rolls back | Prior observation/disposition remains; no new current/outbox effect | Retry according to ADR 0018; object remains non-canonical | Issue #7 |
| Duplicate command, same pre-write request digest | Existing verified result | Return the stable receipt after authorization/revalidation | Application boundary |
| Retry after finalization, same request digest and observed identity | Existing verified provisional/canonical result | Return the stable result; do not rewrite bytes | Application boundary |
| Retry after finalization, same request digest but different observed identity | Content/idempotency conflict | Consume/hash attempted stream if needed, preserve existing bytes, require new command identity | Application boundary |
| Duplicate command, different request digest or any changed semantic field | Issue #8 idempotency conflict | Do not overwrite; require a new command identity | Application boundary |
| Object missing while receipt exists | Degraded integrity state | Restore/repair only through an approved recovery manifest; otherwise fail closed | Operations plus Issue #6 |
| Object digest corrupts after receipt | Corruption result | Restore from backup or create a new revision; never serve bytes | Operations plus Issue #6/#7 |
| Restart during reconciliation | Cursor/checkpoint resumes | Repeat idempotently | Issue #6 worker |
| Machine/filesystem failure leaves state indeterminate | No proof of complete object or recovery generation | Mark unavailable; require manifest-based backup/restore or operator decision | Operations |
| Crash after observed hashing before PostgreSQL observation commit | Finalized orphan candidate with provisional observed identity | Reuse only when request digest, observed identity, generation and command identity all match | Issue #6 plus PostgreSQL reconciliation |
| Policy/lifecycle version changes before grant consumption | Grant rejected; no content byte returned | Re-run authorization from the SourceRevision boundary | Authorized read gateway |
| One-shot grant replay or expiry | Grant rejected; no content byte returned | Require a new authorization evaluation and grant | Authorized read gateway |
| Cross-Workspace/Environment/SourceRevision/generation substitution | Scope or generation mismatch; fail closed | No retry with substituted identity | Authorized read gateway |
| Process restart with outstanding in-memory grants | All outstanding grants invalidated | Require a new authorization evaluation and grant | Authorized read gateway |
| Tombstone accepted | Historical association retained; ordinary reads fenced | No content retry | Issue #7 |
| Approved physical deletion repeats | Deleted or already absent | Return stable deletion result | Issue #6 |
| Unapproved deletion requested | Protected failure | No retry without approval | Application boundary |

Fail closed whenever authorization, tenant scope, reference integrity,
canonical association, tombstone status or post-failure object state cannot be
proved.

## Reconciliation and Cleanup

Issue #6 owns the technical reconciliation mechanism; Issue #7 and operations
own the canonical decision about whether an object should exist. Reconciliation
must:

- list sanitized adapter inventory entries without exposing physical paths;
- compare finalized generations against PostgreSQL receipt/reference metadata;
- identify staged uploads past a bounded operational age;
- identify finalized objects with no matching committed receipt;
- identify receipts with missing, length-mismatched or digest-mismatched objects;
- be restartable, bounded, observable and idempotent;
- quarantine or report ambiguity rather than deleting automatically;
- apply physical cleanup only after an explicit approved retention/deletion rule;
- emit safe diagnostics and audit references without private content.

Repair may associate a finalized object only when the command identity,
pre-write request digest, observed content identity, generation, object identity
and intended tenant/revision are all provable. It must not guess a SourceObject
or merge equal bytes across provenance. A post-stream validation result that
conflicts with the recorded observation remains quarantined/rejected under its
canonical owner; reconciliation cannot rewrite the request digest or infer
admission.

## Security, Integrity and Privacy Invariants

1. Every application operation carries trusted Workspace and, where applicable,
   Environment context.
2. A reference cannot select or widen tenant scope.
3. Only the authorized original-content read service accepts a
   SourceRevision identity for content reads; raw adapter read operations are
   unreachable from API handlers, tools, model-facing code, citations and
   arbitrary callers.
4. The trusted gateway revalidates Access Policy, current authorization,
   SourceRevision lifecycle and authorization-version validity immediately
   before consuming a one-shot internal grant; the adapter is never an ACL
   authority.
5. Grants are opaque, non-serializable, one-shot, scope/generation-bound,
   expiry-bound and invalidated on process restart. An ObjectReference or
   expired/revoked/replayed grant alone cannot disclose content or confer
   continuing authority.
6. Tombstoned, rejected, quarantined, stale or unauthorized content is not
   disclosed through reads, projections, citations, logs or errors.
7. SHA-256 and byte length are calculated from streamed bytes; client MIME,
   filename and digest declarations are untrusted expectations.
8. Finalized objects are immutable and no-replace publication prevents
   concurrent overwrite attempts.
9. Equal bytes do not merge SourceObjects, SourceRevisions, policies or tenants.
10. User filenames are bounded display data only; they cannot cause path
   traversal, separator injection, symlink traversal or unsafe path creation.
11. Upload/download sizes, chunk sizes, concurrent streams, durations, idle
   timeouts and buffers are bounded and backpressured.
12. Adapter credentials have least privilege and cannot alter PostgreSQL
    canonical state or bypass application authorization.
13. APIs, logs, citations, audit payloads, model context and errors exclude
    physical paths, bucket details, credentials, signed URLs, private bytes and
    sensitive references.
14. Authorization changes take effect on the next application read; a previously
    issued reference, receipt or stream does not grant continuing authority.
15. Missing or unverifiable integrity fails closed rather than returning a
    partial or stale object.
16. Backup/restore validation checks both PostgreSQL metadata, physical bytes
   and the coordinated recovery manifest before restored content becomes
   readable.

## Backend Alternatives and Recommendation

| Option | Strengths | Risks/costs | R1 decision |
| --- | --- | --- | --- |
| Dedicated local filesystem volume | Smallest deployment change; true streaming; easy temporary integration tests; no new service; supports atomic same-filesystem publication when profiled | Durability depends on platform/volume/backup; machine failure and multi-replica use require explicit deployment constraints; directory/file sync details are platform-specific | Recommended conditional R1 adapter |
| PostgreSQL `bytea`/large-object storage | One canonical transaction domain; backup follows database | Couples large bytes to database resources and backups; complicates streaming/resource limits; violates the intended replaceable object boundary in practice | Rejected for R1 |
| S3-compatible object store | Strong provider ecosystem, streaming, versioning and remote durability options; migration path | Adds service/credentials/network dependency and operational complexity not required by single deployment; semantics still need receipts and authorization | Future adapter, not required |
| Distributed object-storage cluster | Potential scale and failure-domain control | Infrastructure, quorum, operations and deployment scope far exceed R1 | Rejected |

The recommendation is not a claim that local filesystem durability is universal.
Before production use, deployment must approve a concrete platform/volume,
backup and restore profile and pass the filesystem integration/recovery tests.
If that approval is unavailable, select an S3-compatible adapter through a
separate architectural decision. A new ADR is likely required if the selected
backend changes deployment durability assumptions, but this specification does
not create or approve that ADR.

## Backup and Restore

Backups must include PostgreSQL receipt/reference metadata and all finalized
object bytes needed by the approved retention and historical-revision policy.
Staged uploads may be excluded only under an explicit operational rule and must
not be treated as canonical. The backup system must produce an adapter-neutral
coordinated recovery set rather than relying on two unrelated backup timestamps.

The conceptual manifest is:

```text
StorageRecoveryManifest
  schema_version: positive integer
  recovery_set_id: opaque UUID
  postgres_boundary: recovery position or snapshot identity
  object_volume_generation: opaque snapshot/generation identity
  creation_state: preparing | finalized | invalidated
  integrity_algorithm: sha256
  manifest_digest: lowercase hex
  scope: non-sensitive retention/scope identifier
  created_at: server timestamp
  completed_at: server timestamp | null
```

The manifest contains no private content, physical credentials or reusable
storage authority. Its digest covers the normalized manifest fields and the
recorded component identities. It is finalized only after both the PostgreSQL
recovery boundary and object-volume snapshot/generation are identified and
durable. This is a coordination contract, not a claim that PostgreSQL and a
filesystem can be atomically snapshotted together.

The approved backup coordinator must:

1. establish and record the PostgreSQL recovery boundary;
2. create or select the corresponding object-volume snapshot/generation;
3. finalize the manifest only when both components are identified and durable;
4. store and protect the manifest under the approved backup system;
5. restore the components according to the manifest into an isolated context;
6. reconcile receipts, SourceRevision observations, admission dispositions,
   currentness and physical objects;
7. verify digest, length, generation and required tombstone/policy state without
   disclosing bytes;
8. keep readiness closed until required integrity validation and operational
   approval succeed.

Mixed or incomplete generations, invalidated manifests, missing snapshots and
partial restores are degraded recovery states requiring reconciliation and
operator approval. A finalized object without a receipt remains an orphan
candidate. A receipt without a verified object remains unavailable and cannot be
disclosed. Restore must never guess tenant, SourceObject or SourceRevision
provenance.

Backup product, recovery point/recovery time objectives, retention, legal holds,
cross-region copies and destructive cleanup are open
operational/compliance decisions, not implied by this specification.

## Test Strategy and Confirmed Test Seams

Tests assert observable contracts and security invariants, not private call
order, SQLAlchemy mappings, directory names or provider SDK behavior.

1. **Shared conformance suite.** The same observable contract suite runs against
   an in-memory fake and the real adapter. It covers immutable upload/finalize,
   streaming read, digest/length, duplicate commands, idempotency, abort,
   typed errors and controlled deletion. The fake is explicitly documented as a
   policy/contract test double: it does not prove physical durability, fsync,
   crash behavior, filesystem atomicity, backup integrity or provider failure
   semantics.
2. **Application-level orchestration seam.** A fake object store and fake Unit
   of Work exercise the Issue #6 boundary: Issue #8 canonical request digest,
   staged write, provisional receipt, PostgreSQL receipt handoff, observation
   rollback, duplicate command, changed-semantic-field conflict, uploads with
   no expected checksum, same/different observed bytes, retry before and after
   finalization, crash after observed hashing and reconciliation outcomes. This
   seam does not implement Issue #3's ingestion pipeline or Issue #7's
   canonical SourceRevision repository; it consumes narrow ports representing
   those boundaries.
3. **Filesystem integration seam.** Temporary storage on the supported platform
   and filesystem proves bounded streaming, exclusive immutable publication,
   process-restart recovery, partial upload handling, orphan reconciliation,
   corruption/missing-object behavior and controlled deletion. The suite must
   state the exact supported platform/filesystem assumptions and must not label
   `fsync + rename` a universal guarantee. Where safe and practical, separate
   tests cover file-content synchronization, parent-directory synchronization,
   atomic publication, no-replace behavior, abrupt process restart and backup/
   restore validation. Machine/power-loss guarantees remain deployment evidence,
   not a claim made by the fake or ordinary unit tests.
4. **PostgreSQL integration seam.** The existing real PostgreSQL harness proves
   receipt/reference transaction behavior, tenant isolation, rollback and
   consistency checks. It does not duplicate filesystem durability tests.
5. **Authorization/security seam.** An application-facing read boundary is
   exercised with two Workspaces and Environments, allowed and denied Access
   Policy outcomes, reference possession without authority, policy revocation,
   policy revocation between initial authorization and stream creation,
   tombstones in that window, expired/version-mismatched grants, replayed grants,
   cross-Workspace/Environment and SourceRevision/ObjectReference/generation
   substitution, process restart invalidation and sanitized failures. The test
   proves authorization fails before any content byte is returned, raw adapter
   reads cannot be reached from untrusted entry points, and reads are invoked
   only through the authorized application boundary; it must not make the
   storage adapter an ACL or policy owner.
6. **Operational/backup seam.** A restore fixture validates receipt/object
   consistency against a `StorageRecoveryManifest`, recovery-boundary/generation
   matching, checksum verification, missing-object degradation and readiness
   gating. Retention and physical deletion tests verify approval and idempotency,
   but do not assert an unapproved retention duration.

Required scenarios include oversized input, unsafe filename, path traversal,
client MIME spoofing, timeout, bounded memory/backpressure, checksum mismatch,
duplicate/retried command, changed semantic command fields, identical bytes
across tenants and revisions,
partial upload, process restart, orphan cleanup, missing/corrupt object,
tombstone, approved and unapproved deletion, concurrent no-replace finalize,
publication-step crashes, uploads without an expected checksum, same-key
different-byte retries, pre/post-finalization retries, observed-hash crash
windows, one-shot grant replay/revocation/substitution and manifest-based
backup/restore.

Repository gates remain:

```text
uv sync --extra dev
uv run pytest -q
uv run python -m compileall -q src tests
```

For documentation-only work, the implementation-specific storage tests are
specified but are not present until a later implementation ticket. Existing
tests must continue to prove the domain/application framework boundary.

## User Stories

1. As an ingestion application, I want to preserve exact original bytes, so that every later observation has durable provenance.
2. As a source-revision owner, I want an immutable object reference, so that a revision cannot silently change underneath its evidence.
3. As a Workspace owner, I want original content isolated from other Workspaces, so that tenant data cannot cross boundaries.
4. As an Environment owner, I want environment scope preserved, so that staging and production content cannot be confused.
5. As an application developer, I want a framework-independent storage port, so that storage can be replaced without changing domain policy.
6. As an application developer, I want typed write receipts, so that successful storage is distinguishable from an attempted upload.
7. As an application developer, I want streamed writes, so that large documents do not consume unbounded memory.
8. As an application developer, I want streamed reads, so that parsers and validators can apply backpressure.
9. As an integrity engineer, I want SHA-256 and byte length verified, so that corruption and truncation are detectable.
10. As an integrity engineer, I want checksum mismatch to fail closed, so that invalid bytes never become a SourceRevision.
11. As an application developer, I want immutable finalization, so that retries cannot overwrite an existing object.
12. As an application developer, I want upload commands idempotent, so that transport retries do not create duplicate logical effects.
13. As an application developer, I want changed content under one idempotency key rejected, so that command identity remains unambiguous.
14. As a provenance owner, I want equal bytes to remain separate across SourceObjects, so that source identity is not replaced by content identity.
15. As a security engineer, I want possession of a reference to confer no read authority, so that references cannot bypass Access Policy.
16. As a security engineer, I want authorization evaluated before stream creation, so that denied content is never disclosed.
17. As a security engineer, I want the adapter ignorant of ACL policy, so that one storage implementation cannot become a policy bypass.
18. As a privacy engineer, I want filenames treated as untrusted display data, so that path construction cannot be attacked.
19. As a privacy engineer, I want physical paths, bucket names and credentials excluded from diagnostics, so that operational metadata cannot disclose infrastructure.
20. As an operator, I want failed uploads resumable or abortable, so that process crashes do not require manual filesystem surgery.
21. As an operator, I want orphaned staged objects detectable, so that abandoned uploads do not consume storage forever.
22. As an operator, I want finalized-but-unassociated objects detectable, so that PostgreSQL rollback does not create invisible leaks.
23. As an operator, I want receipts checked against physical objects, so that missing or corrupt bytes become visible.
24. As an operator, I want reconciliation restartable, so that a crash during cleanup does not lose progress or create unsafe deletion.
25. As an operator, I want machine/filesystem uncertainty to fail closed, so that ambiguous durability is not reported as success.
26. As a PostgreSQL owner, I want receipt metadata canonical in PostgreSQL, so that storage cannot select current revisions.
27. As an Issue #7 implementer, I want a narrow association contract, so that SourceRevision repositories can consume receipts without adopting provider details.
28. As an Issue #3 implementer, I want a storage admission boundary, so that ingestion orchestration does not own physical storage mechanics.
29. As an Issue #4 implementer, I want sanitized storage diagnostics, so that audit and delivery telemetry do not contain private bytes.
30. As a projection operator, I want a missing object to block projection, so that derived evidence cannot outlive its original.
31. As a retrieval operator, I want tombstones to fence ordinary reads, so that deleted source content cannot return from a cache or projection.
32. As a compliance owner, I want physical deletion approval-gated, so that retention and legal obligations are not silently violated.
33. As a compliance owner, I want historical retention decisions explicit, so that this technical boundary does not invent a legal policy.
34. As an operator, I want backups to include receipts and bytes, so that restore preserves provenance and integrity.
35. As an operator, I want restore validation before readiness, so that partially restored content is not served.
36. As a test author, I want a fake conformance adapter, so that application behavior is fast and deterministic.
37. As a test author, I want the fake clearly limited, so that it is not mistaken for proof of physical durability.
38. As a test author, I want the real filesystem adapter tested at its external seam, so that platform assumptions are visible.
39. As a test author, I want two tenants and revisions using identical bytes, so that deduplication cannot erase provenance.
40. As a maintainer, I want a provider-neutral error taxonomy, so that callers do not parse filesystem or SDK exceptions.
41. As a maintainer, I want a future S3-compatible adapter possible, so that R1 storage does not create irreversible provider coupling.
42. As a deployment owner, I want the selected filesystem profile documented, so that fsync, rename, backup and restart claims are testable.
43. As a security engineer, I want bounded upload concurrency and timeouts, so that malicious inputs cannot exhaust resources.
44. As a source-history owner, I want a finalized object to be associated only after verified storage, so that SourceRevision never points at partial bytes.
45. As a recovery operator, I want a retry after PostgreSQL rollback to reuse verified bytes safely, so that recovery does not duplicate content or overwrite data.
46. As an audit consumer, I want deletion and integrity outcomes typed, so that operational history distinguishes approved deletion from failure.
47. As a Q&A operator, I want degraded storage health visible without private content, so that I can diagnose unanswerability safely.
48. As a platform maintainer, I want object storage replaceable, so that Cognee, filesystem and future providers remain behind application contracts.
49. As an application security engineer, I want raw adapter reads hidden behind an authorized read service, so that API handlers and model-facing code cannot turn a reference into disclosure.
50. As an authorization service, I want a short-lived read capability bound to Workspace, Environment, SourceRevision, purpose and authorization version, so that a capability cannot be reused after policy revocation.
51. As an idempotency owner, I want the Issue #8 pre-write request digest, so that retries with changed scope, purpose or expected content are rejected consistently.
52. As a persistence owner, I want PostgreSQL to own the canonical command receipt, so that provisional adapter evidence cannot be mistaken for canonical SourceRevision state.
53. As a source-history owner, I want observed, dispositioned and canonically accepted revisions separated, so that storage finalization cannot alter ADR 0018 lifecycle decisions.
54. As a backup operator, I want one recovery manifest binding PostgreSQL and object-volume identities, so that restore validation can detect mixed generations.
55. As a backup operator, I want readiness closed for an incomplete or mixed recovery set, so that partial restoration cannot disclose content.
56. As a storage adapter developer, I want no-replace publication outcomes defined for concurrent finalization, so that a race cannot overwrite an immutable generation.
57. As a storage adapter developer, I want generation identity semantics explicit, so that exact restore, repair and orphan reassociation cannot confuse physical generations.
58. As an idempotency owner, I want the pre-write request digest to follow Issue #8 canonicalization, so that storage commands share one conflict and receipt model.
59. As an ingestion operator, I want observed byte identity stored separately from request identity, so that uploads without expected checksums remain safe and auditable.
60. As an ingestion operator, I want same-key different-byte retries rejected after hashing without replacing finalized bytes, so that retries cannot corrupt an original.
61. As an authorization gateway, I want policy and lifecycle versions revalidated immediately before one-shot grant consumption, so that revocation prevents new stream creation.
62. As an authorization gateway, I want process restart to invalidate outstanding grants, so that in-memory authority cannot survive an uncertain process state.
63. As a persistence owner, I want the Issue #8 idempotency receipt completed in a short transaction with an opaque upload-state result, so that a long byte stream never holds an incomplete database claim.
64. As an ingestion operator, I want the upload state linked to the Issue #8 receipt rather than a second idempotency key, so that staged recovery and replay remain one canonical command.

## Numbered Acceptance Criteria

1. The specification defines a framework-independent immutable original-object
   storage port with upload, abort/resume, finalize, streaming read, verify,
   reconciliation and controlled deletion behavior.
2. The specification defines typed `ObjectReference`, `OriginalUpload`,
   `OriginalUploadCommandPayload`, `PreWriteRequestDigest`,
   `ObservedContentIdentity`, `ValidatedContentEvidence`, `WriteReceipt`,
   `UploadCommandState`, `IntegrityResult`,
   `AuthorizedOriginalReadGrant`, `StorageRecoveryManifest` and adapter-neutral
   error categories.
3. Object references contain no physical path, bucket, provider credential,
   signed URL or private byte content.
4. SHA-256 and byte length are computed from streamed bytes and verified before
   a receipt can be associated with a SourceRevision.
5. Finalized objects cannot be overwritten: concurrent no-replace publication
   has one stable winner, same-generation retries reuse the result, different
   generations return a typed conflict, and no partial object is readable.
6. Identical bytes can be represented independently for different tenants,
   SourceObjects and SourceRevisions; digest equality does not merge provenance.
7. The application protocol reuses Issue #8's versioned canonical SHA-256
   request digest and canonicalization rules for pre-write semantic inputs,
   excluding observed content and post-stream validation; it separately records
   the SHA-256 observed content identity and reuses identical results while
   rejecting any changed request field or finalized observed byte identity.
8. The specification states that PostgreSQL receipt/reference and canonical
   SourceRevision state remain authoritative; object storage does not own source
   identity, history, ACL, tombstone, projection or current-revision state.
9. The specification separates durable SourceRevision observation and receipt
   association, append-only admission disposition, and ADR 0018
   canonical-acceptance/current-revision publication; storage controls none of
   those lifecycle decisions.
10. The specification defines the no-distributed-transaction sequence and every
    crash boundary from validation through observation receipt persistence and
    canonical acceptance. Observation rollback and canonical-acceptance rollback
    have distinct effects.
11. Failed PostgreSQL observation transactions leave no valid observation
    association, while finalized unassociated objects are classified as
    recoverable orphan candidates rather than silently deleted or served.
12. Missing, corrupt, mismatched, stale, tombstoned or authorization-ambiguous
    objects fail closed for ordinary content reads and projection.
13. Staged uploads and finalized orphan candidates have restartable,
    idempotent reconciliation behavior with deletion approval requirements.
14. The filesystem recommendation is explicitly conditional on a supported
    platform/filesystem and separately analyzes file sync, directory sync,
    atomic publication, no-replace behavior, restart/failure behavior and
    backup/restore; it does not claim universal `fsync + rename` durability.
15. The adapter boundary excludes ACL and policy decisions; raw adapter reads
   are unreachable from untrusted entry points and content reads require a
   one-shot opaque grant minted and consumed by the authorized service after an
   immediate policy/lifecycle-version revalidation.
16. Workspace/Environment isolation, reference possession versus authority,
    filename safety, MIME distrust, path traversal prevention, size limits,
    bounded buffers, backpressure, timeout and credential least privilege are
    explicit invariants.
17. APIs, logs, citations, audit payloads, model context and errors exclude
    private bytes and sensitive physical storage details.
18. Logical tombstones and physical deletion are separate; physical deletion
    and retention remain approval-gated and no unapproved duration is selected.
19. Backup/restore uses a finalized `StorageRecoveryManifest` binding the
   PostgreSQL recovery boundary and object-volume generation; mixed or incomplete
   recovery remains degraded and unavailable until reconciliation and approval.
20. The test strategy includes the confirmed six seams, shared fake/real
    conformance, bounded streaming, duplicate/retry, cross-tenant identical
    bytes, corruption, restart, orphan, missing-object, tombstone, deletion,
    authorization-before-byte, grant replay/revocation/substitution,
    no-expected-checksum same/different-byte retries, observed-hash crash,
    filename/MIME/path/size/timeout and backup/restore cases.
21. The specification names Issue #6 ownership and the exact coordination
    boundaries with Issue #7, Issue #3, Issue #4 and Issue #8.
22. The recommendation compares local filesystem, PostgreSQL bytes,
    S3-compatible storage and distributed storage, and makes local filesystem
    conditional R1 recommendation with S3-compatible migration path.
23. `storage_generation` has defined uniqueness, stability, restoration/repair,
    equality, retry and orphan-reassociation semantics and never reveals provider
    details.
24. The document contains no production implementation, migration, public API,
    ingestion pipeline, audit delivery, cloud deployment, ticket, PR or GitHub
    mutation.
25. The pre-write request digest is calculated before streaming using Issue #8's
   canonical request algorithm and completed in a short idempotency transaction;
   the observed SHA-256 digest and byte length are calculated only from the
   actual stream and are persisted separately through the linked upload state.
26. Uploads without an expected checksum have explicit same-key/same-byte,
    same-key/different-byte, pre-finalization retry, post-finalization retry and
    post-hash/pre-PostgreSQL-crash behavior without overwriting finalized bytes.
27. The authorized read gateway revalidates policy/lifecycle immediately before
    consuming a one-shot opaque grant; replay, expiry, restart, scope,
    generation, tombstone and version mismatch fail before any content byte is
    returned.

## Suggested Implementation Slices

1. Add direct typed application/domain contracts and the error taxonomy; prove
   they import without infrastructure frameworks.
2. Add the in-memory fake and shared conformance suite, explicitly labeling the
   durability limits of the fake.
3. Add the filesystem adapter behind a deployment profile with staged/finalized
   namespaces, streaming, digest verification and overwrite prevention.
4. Add application orchestration for receipt handoff and idempotency using the
   existing Issue #8 Unit of Work, without implementing Issue #7 repositories.
5. Add filesystem restart, crash-window, orphan and restore integration tests.
6. Add PostgreSQL receipt/reference persistence when Issue #7 defines its
   canonical records and migration ownership.
7. Add reconciliation and controlled deletion only after retention/approval
   rules are supplied by product, operations and compliance owners.
8. Integrate the Issue #3 ingestion pipeline and Issue #4 audit/delivery
   diagnostics and safe operational signals through their approved contracts;
   preserve legacy compatibility paths until tested replacements exist. Any UI
   slice consuming those signals is separate from Issue #6 and Issue #4.

## Dependency Ordering

```text
Issue #8 persistence kernel
        ↓
Issue #6 storage contracts + fake/conformance
        ↓
Issue #6 filesystem adapter + recovery tests
        ↓
Issue #7 SourceObject/SourceRevision/AccessPolicy repositories
        ↓
Issue #3 ingestion orchestration
        ↓
Issue #4 audit-event, delivery-diagnostics and safe-operational-signal contracts
```

Issue #6 may define and test its boundary before Issue #7 repositories exist,
but must not invent their canonical schemas. Issue #14/ADR 0018 remains
authoritative throughout. General R1 UI work is a separate interface slice; it
may consume Issue #4 diagnostics but is not owned by Issue #4 or required to
complete Issue #6 durable storage.

## Risks and Mitigations

- **Filesystem durability overclaim**: require a named platform/filesystem
  profile, recovery tests, backup validation and explicit degraded behavior.
- **Dual-write inconsistency**: use verified finalize before PostgreSQL receipt,
  stable idempotency, reconciliation and fail-closed reads; do not use a
  distributed transaction.
- **Cross-tenant leakage through references**: reauthorize at the application
  boundary and bind every PostgreSQL operation to trusted context.
- **Accidental deduplication**: never use digest as source identity; if a future
  adapter physically deduplicates, it must use tenant/security-domain isolation
  and preserve independent logical references.
- **Resource exhaustion**: bound sizes, chunks, streams, timeouts, concurrency
  and buffers; exercise adversarial cases.
- **Retention/compliance error**: keep physical deletion approval-gated and leave
  policy duration/legal-hold decisions open.
- **Provider lock-in**: keep opaque references and provider-neutral errors; run
  conformance tests against every adapter.
- **Prototype bypass**: do not silently route the legacy `/ingest` endpoint or
  compatibility loader through incomplete new behavior.
- **Backup split-brain**: validate PostgreSQL receipts and object bytes together
  against a finalized `StorageRecoveryManifest`, and keep readiness closed for
  mixed or incomplete generations.

## Out of Scope

- Production implementation of the port or any adapter.
- Database migrations or SourceObject/SourceRevision/AccessPolicy repositories.
- The Issue #3 ingestion pipeline, parsing, malware scanning or semantic
  extraction policy.
- Issue #4 audit delivery, dispatcher, telemetry backend or external diagnostics.
- Public API, UI, generated frontend types or cloud deployment.
- Distributed object-storage infrastructure or unsupported distributed
  transactions.
- Automatic retention duration, legal hold policy or unapproved physical
  deletion.
- Making Cognee, a filesystem, a bucket or an object reference canonical.
- Creating implementation tickets, publishing to GitHub, changing labels,
  opening a PR, pushing, merging or closing any issue.

## Open Questions Requiring Approval

1. Which exact R1 deployment platform and filesystem are approved for the local
   adapter, and what failure guarantees does the volume provider document?
2. Are machine/power-loss tests available in the deployment environment, or must
   readiness depend on a stronger S3-compatible adapter?
3. Which backup product and restore point objectives cover PostgreSQL receipts
   and finalized objects as one recoverable set?
4. What retention durations, legal holds, historical-revision obligations and
   deletion approvals apply to original bytes, staged objects, backups and
   quarantined/rejected content?
5. Is any physical deduplication allowed in a future adapter? If yes, which
   security-domain boundary and lifecycle rules prevent cross-tenant leakage?
6. Which content validation and media-type policy does Issue #3 own, and which
   validation failures must remain quarantined rather than deleted?
7. What exact Issue #7 schema and repository operation records the receipt and
   binds it to a SourceRevision, including historical retention and tombstone
   behavior?
8. What minimum sanitized storage diagnostics and audit events does Issue #4
   require, without copying private content or infrastructure details?
9. Which operational role may approve physical deletion, and how is that
   approval represented and independently verified at execution time?
10. Does a future S3-compatible deployment require a new ADR before selection,
    or can the architecture group approve it under this port once the durability
    and backup evidence is available?
11. Which exact Issue #3 validator/version produces `ValidatedContentEvidence`,
    and which bounded fields become Issue #7 observation metadata? The result
    must remain post-stream evidence and must not be added to the Issue #8
    pre-write request digest without a versioned command-contract decision.
12. What exact Issue #8 operation name/schema version and result-reference type
    will Issue #7 use for the upload command receipt? The implementation must
    reuse the kernel canonicalizer and digest algorithm rather than define a
    storage-specific idempotency system.
13. What concrete trusted gateway composition and Unit of Work/snapshot boundary
    will perform the immediate policy/lifecycle-version recheck before one-shot
    grant consumption? The gateway must remain in-process and must not become a
    new distributed capability service.

## Further Notes

The local filesystem recommendation is intentionally the smallest adapter for
the approved single-deployment target, not a claim that a filesystem is a
universal object-storage service. The contract is designed so that a future
S3-compatible implementation can replace it without changing SourceRevision,
Access Policy, canonical acceptance, projection or citation semantics.

Issue #6 requires immutable checksum-addressed originals, atomic
write/receipt behavior, safe retries, integrity checks, bounded streaming,
secure metadata handling, backup/restore participation, and explicit separation
of tombstones from physical retention.
