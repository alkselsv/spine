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
into isolated staged storage, verifies a declared SHA-256 digest and returns a
typed write receipt. A successful finalization publishes the object exactly
once under an immutable reference. Reads are streams obtained only after the
application boundary has verified current Workspace/Environment authorization
and SourceRevision state. The adapter never evaluates ACLs, chooses a current
revision, interprets tombstones, or grants disclosure authority.

For the approved R1 single-deployment target, the recommended adapter is a
dedicated local filesystem storage root on a persistent volume, provided the
deployment accepts and verifies the platform-specific durability assumptions
listed below. PostgreSQL remains the canonical source for receipt/reference
metadata, SourceRevision association, authorization and lifecycle state. An
S3-compatible adapter is a later migration path, not an R1 infrastructure
requirement.

The protocol deliberately avoids a distributed transaction. The application
persists an object receipt and canonical SourceRevision in PostgreSQL only after
the object has been durably finalized and verified. If PostgreSQL rolls back,
the object is recoverable as an unassociated finalized object and reconciliation
may remove it only under an approved cleanup policy. If finalization has not
completed, no PostgreSQL reference is valid. Any missing, mismatched or
ambiguous object causes fail-closed behavior for content reads and projection.

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
content disclosure.

## Terminology

- **Original object**: the immutable byte sequence stored for one observed
  source revision.
- **Staged object**: an incomplete, non-readable-to-ordinary-consumers upload
  identified by a temporary upload identity.
- **Finalized object**: a verified immutable byte object available to an
  authorized application read.
- **ObjectReference**: a provider-neutral opaque locator for one finalized
  object, not a path, URL, bucket key or filesystem detail.
- **WriteReceipt**: immutable evidence returned by the store after a successful
  staged write/finalization, including object identity, byte count and verified
  digest.
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
  storage_generation: opaque UUID  # identifies one finalized physical generation
  digest_algorithm: "sha256"
  digest_hex: 64 lowercase hex characters
  byte_length: positive integer or zero
```

`object_id` is the logical identity assigned by the application. The reference
must not expose a physical path, storage root, provider name, bucket, signed
URL, access token or content bytes. `storage_generation` prevents an old receipt
from being mistaken for a later physical object if a recovery process recreates
an object. A reference is usable only when its complete value matches a
PostgreSQL receipt and the application has authorized the associated revision.

The reference is not a SourceObject ID and is not a SourceRevision ID. The same
digest may appear in many references for different tenants, SourceObjects and
SourceRevisions. No digest-only lookup may merge provenance or authorization.

### Upload command and write receipt

The application-facing upload command contains:

```text
OriginalUpload
  upload_id: UUID                  # stable idempotency identity
  object_id: UUID                  # server-issued logical object identity
  workspace_id: UUID               # trusted context, not caller authority
  environment_id: UUID | null
  expected_sha256: lowercase hex | null
  expected_byte_length: integer | null
  declared_media_type: bounded string | null
  declared_filename: bounded display metadata | null
  purpose: bounded server-selected identifier
```

Filename and media type are hints for validation and display; neither becomes a
storage path or proof of content. The application calculates the digest while
streaming and treats a client declaration as an expectation to verify, not as
trusted metadata.

The immutable receipt is:

```text
WriteReceipt
  schema_version: positive integer
  upload_id: UUID
  object_reference: ObjectReference
  observed_byte_length: non-negative integer
  observed_sha256: lowercase hex
  verification: IntegrityResult
  finalized_at: server timestamp
  adapter_generation: opaque non-sensitive value
```

Receipt persistence is separate from physical writing. A receipt is not
successful until the adapter has finalized the object, verified its byte count
and digest, and returned a stable reference. `adapter_generation` may identify a
storage format generation but must not expose provider or infrastructure
secrets.

### Integrity result

```text
IntegrityResult
  status: verified | mismatch | unavailable | indeterminate
  algorithm: sha256
  expected_digest: digest | null
  observed_digest: digest | null
  expected_length: integer | null
  observed_length: integer | null
```

Only `verified` permits a finalized receipt to be associated with a content
SourceRevision. `mismatch`, `unavailable` and `indeterminate` are non-success
outcomes. The application must not infer that unavailable verification means
verified.

### Storage port

The port exposes the following behavior, without prescribing method names or
provider types:

1. Begin or resume an upload by `upload_id` and trusted scope.
2. Stream bounded chunks into staged storage while calculating digest and length.
3. Abort an upload, making its staged bytes inaccessible to ordinary reads.
4. Finalize exactly once after successful validation, with overwrite prevention.
5. Return a durable `WriteReceipt` or a typed adapter-neutral failure.
6. Open a bounded asynchronous read stream for an already-finalized
   `ObjectReference`; never return an unbounded in-memory byte array.
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
  different digest/length.
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
It does not own authentication, Access Policy, SourceRevision state, tenant
selection, tombstones, current revision, canonical acceptance, retention
approval or publication state.

### Recommended filesystem adapter

The R1 adapter uses a dedicated storage root on a persistent local volume. It
uses separate staged and finalized namespaces, opaque generated names, and a
format version. User filenames never participate in path construction. The
implementation must use exclusive creation and refuse replacement of a
finalized name. A finalized object is published only after the complete byte
stream and its digest have been verified.

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
4. **No replacement**: whether exclusive create/link semantics prevent a retry
   or attacker from replacing an existing finalized object.
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
7. Only a verified `WriteReceipt` can proceed to PostgreSQL receipt/reference
   persistence and later SourceRevision admission.

### Read

The application resolves the requested SourceRevision and current Access Policy
in PostgreSQL, verifies Workspace/Environment isolation, tombstone/currentness,
retention and purpose, then asks the port to open a stream using the opaque
reference. The adapter rechecks reference integrity as configured, applies
bounded read and timeout limits, and streams with backpressure. Authorization is
not delegated to the adapter and a reference alone is insufficient.

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
2. Issue #6 returns a verified `WriteReceipt`.
3. Issue #7's future repository persists the receipt/reference metadata and the
   immutable SourceRevision association in one PostgreSQL Unit of Work, with
   tenant keys and the applicable idempotency receipt.
4. The same canonical transaction records any required outbox intent under ADR
   0004. The intent contains identifiers and minimal metadata, never private
   bytes or a storage credential.
5. Commit makes canonical SourceRevision state and its intent visible. Parsing,
   projection and delivery remain asynchronous and cannot make storage
   canonical.

The object write and PostgreSQL transaction are not atomically committed
together. PostgreSQL is canonical for whether a reference is associated and
which SourceRevision is current. A finalized object without a committed receipt
is an orphan candidate, not a readable document. A receipt without a verified
object is an integrity failure, not permission to retry arbitrary reads.

## Atomicity, Retry and Recovery Model

### State classes

| State | Canonical authority | Recoverable action | Ordinary content read |
| --- | --- | --- | --- |
| Upload command accepted | PostgreSQL idempotency/application boundary | Resume or abort by upload ID | No |
| Staged bytes | Object adapter, bounded by upload identity | Resume, abort or reconcile | No |
| Finalized but unassociated object | Adapter plus reconciliation inventory | Associate only through a matching canonical command, otherwise approved cleanup | No |
| Verified receipt/reference | PostgreSQL | Reconcile against object and SourceRevision | Only after policy check |
| Canonical SourceRevision association | PostgreSQL | Follow ADR 0018 lifecycle; never infer from storage | Only if current policy/state allows |
| Tombstoned association | PostgreSQL | Preserve history; approved deletion only | No ordinary read |
| Missing/corrupt referenced object | PostgreSQL says reference exists; adapter says unavailable | Repair from approved backup or rebuild association; otherwise degraded/fail closed | No |

### Failure/retry/recovery matrix

| Boundary/failure | Visible result | Safe retry | Recovery owner |
| --- | --- | --- | --- |
| Validation fails before staging | No object; typed rejection | Retry only with a new valid command or same idempotency key/content | Application/admission boundary |
| Client disconnects during staging | Staged or absent | Resume same upload ID if state is provable; otherwise abort/reconcile | Issue #6 worker/adapter |
| Storage write fails | No successful receipt | Retry bounded transient failures; do not claim success | Issue #6 |
| Digest/length mismatch | Staged bytes quarantined/aborted; no receipt | Same command is a stable mismatch; changed content requires a new identity | Application and adapter |
| Process crashes before finalize | Staged bytes or unknown state | Reconcile by upload ID; never publish an unverified object | Issue #6 |
| Process crashes after finalize before receipt persistence | Finalized orphan candidate | Reuse only when digest, length, upload ID and generation all match | Issue #6 plus PostgreSQL reconciliation |
| Receipt transaction rolls back after object finalize | Finalized orphan candidate | Retry the PostgreSQL operation with same idempotency key; do not rewrite bytes | Issue #7/application |
| PostgreSQL commits receipt but outbox delivery fails | Canonical reference remains | Replay outbox; no second object write | Issue #4/outbox worker |
| Canonical acceptance rolls back | Receipt may remain unassociated or transaction-local | Retry according to Issue #7; object remains non-canonical | Issue #7 |
| Duplicate command, same payload digest | Existing verified result | Return the stable receipt after authorization/revalidation | Application boundary |
| Duplicate command, different digest/length | Integrity/idempotency conflict | Do not overwrite; require a new command identity | Application boundary |
| Object missing while receipt exists | Degraded integrity state | Restore/repair only through approved recovery; otherwise fail closed | Operations plus Issue #6 |
| Object digest corrupts after receipt | Corruption result | Restore from backup or create a new revision; never serve bytes | Operations plus Issue #6/#7 |
| Restart during reconciliation | Cursor/checkpoint resumes | Repeat idempotently | Issue #6 worker |
| Machine/filesystem failure leaves state indeterminate | No proof of complete object | Mark unavailable; require backup/restore or operator decision | Operations |
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

Repair may associate a finalized object only when the command identity, digest,
length, object identity and intended tenant/revision are all provable. It must
not guess a SourceObject or merge equal bytes across provenance.

## Security, Integrity and Privacy Invariants

1. Every application operation carries trusted Workspace and, where applicable,
   Environment context.
2. A reference cannot select or widen tenant scope.
3. Access Policy and current authorization are evaluated before opening a read
   stream; the adapter is never an ACL authority.
4. Tombstoned, rejected, quarantined, stale or unauthorized content is not
   disclosed through reads, projections, citations, logs or errors.
5. SHA-256 and byte length are calculated from streamed bytes; client MIME,
   filename and digest declarations are untrusted expectations.
6. Finalized objects are immutable and overwrite attempts fail.
7. Equal bytes do not merge SourceObjects, SourceRevisions, policies or tenants.
8. User filenames are bounded display data only; they cannot cause path
   traversal, separator injection, symlink traversal or unsafe path creation.
9. Upload/download sizes, chunk sizes, concurrent streams, durations, idle
   timeouts and buffers are bounded and backpressured.
10. Adapter credentials have least privilege and cannot alter PostgreSQL
    canonical state or bypass application authorization.
11. APIs, logs, citations, audit payloads, model context and errors exclude
    physical paths, bucket details, credentials, signed URLs, private bytes and
    sensitive references.
12. Authorization changes take effect on the next application read; a previously
    issued reference, receipt or stream does not grant continuing authority.
13. Missing or unverifiable integrity fails closed rather than returning a
    partial or stale object.
14. Backup/restore validation checks both PostgreSQL metadata and physical bytes
    before restored content becomes readable.

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
not be treated as canonical. Restore is not complete when only one side is
restored.

The restore procedure must:

1. restore PostgreSQL and the object volume into an isolated validation context;
2. verify reference identity, digest and byte length for retained associations;
3. classify missing/corrupt objects as degraded and inaccessible;
4. verify tombstones, policy state and current-revision selection remain
   PostgreSQL-controlled;
5. record the validation result without exposing bytes;
6. make the restored service ready only after required consistency thresholds
   and approval pass.

Backup retention, legal holds, cross-region copies and destructive cleanup are
open operational/compliance decisions, not implied by this specification.

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
   of Work exercise the Issue #6 boundary: staged write, verified receipt,
   PostgreSQL receipt handoff, rollback, duplicate command, crash windows and
   reconciliation outcomes. This seam does not implement Issue #3's ingestion
   pipeline or Issue #7's canonical SourceRevision repository; it consumes
   narrow ports representing those boundaries.
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
   tombstones and sanitized failures. The test proves reads are invoked only
   through the authorized application boundary; it must not make the storage
   adapter an ACL or policy owner.
6. **Operational/backup seam.** A restore fixture validates receipt/object
   consistency, checksum verification, missing-object degradation and readiness
   gating. Retention and physical deletion tests verify approval and idempotency,
   but do not assert an unapproved retention duration.

Required scenarios include oversized input, unsafe filename, path traversal,
client MIME spoofing, timeout, bounded memory/backpressure, checksum mismatch,
duplicate/retried command, identical bytes across tenants and revisions,
partial upload, process restart, orphan cleanup, missing/corrupt object,
tombstone, approved and unapproved deletion, and backup/restore.

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

## Numbered Acceptance Criteria

1. The specification defines a framework-independent immutable original-object
   storage port with upload, abort/resume, finalize, streaming read, verify,
   reconciliation and controlled deletion behavior.
2. The specification defines typed `ObjectReference`, `OriginalUpload`,
   `WriteReceipt`, `IntegrityResult` and adapter-neutral error categories.
3. Object references contain no physical path, bucket, provider credential,
   signed URL or private byte content.
4. SHA-256 and byte length are computed from streamed bytes and verified before
   a receipt can be associated with a SourceRevision.
5. Finalized objects cannot be overwritten, and retries cannot replace a
   generation with different bytes.
6. Identical bytes can be represented independently for different tenants,
   SourceObjects and SourceRevisions; digest equality does not merge provenance.
7. The application protocol defines idempotent duplicate behavior and rejects a
   changed digest or length under an existing command identity.
8. The specification states that PostgreSQL receipt/reference and canonical
   SourceRevision state remain authoritative; object storage does not own source
   identity, history, ACL, tombstone, projection or current-revision state.
9. The specification defines the no-distributed-transaction sequence and every
   crash boundary from validation through receipt persistence and canonical
   acceptance.
10. Failed PostgreSQL transactions leave no valid canonical association, while
    finalized unassociated objects are classified as recoverable orphan
    candidates rather than silently deleted or served.
11. Missing, corrupt, mismatched, stale, tombstoned or authorization-ambiguous
    objects fail closed for ordinary content reads and projection.
12. Staged uploads and finalized orphan candidates have restartable,
    idempotent reconciliation behavior with deletion approval requirements.
13. The filesystem recommendation is explicitly conditional on a supported
    platform/filesystem and separately analyzes file sync, directory sync,
    atomic publication, no-replace behavior, restart/failure behavior and
    backup/restore; it does not claim universal `fsync + rename` durability.
14. The adapter boundary excludes ACL and policy decisions; reads are reachable
    only through an authorized application boundary.
15. Workspace/Environment isolation, reference possession versus authority,
    filename safety, MIME distrust, path traversal prevention, size limits,
    bounded buffers, backpressure, timeout and credential least privilege are
    explicit invariants.
16. APIs, logs, citations, audit payloads, model context and errors exclude
    private bytes and sensitive physical storage details.
17. Logical tombstones and physical deletion are separate; physical deletion
    and retention remain approval-gated and no unapproved duration is selected.
18. Backup/restore validation checks PostgreSQL metadata and stored bytes before
    restored content is made available.
19. The test strategy includes the confirmed six seams, shared fake/real
    conformance, bounded streaming, duplicate/retry, cross-tenant identical
    bytes, corruption, restart, orphan, missing-object, tombstone, deletion,
    authorization, filename/MIME/path/size/timeout and backup/restore cases.
20. The specification names Issue #6 ownership and the exact coordination
    boundaries with Issue #7, Issue #3, Issue #4 and Issue #8.
21. The recommendation compares local filesystem, PostgreSQL bytes,
    S3-compatible storage and distributed storage, and makes local filesystem
    conditional R1 recommendation with S3-compatible migration path.
22. The document contains no production implementation, migration, public API,
    ingestion pipeline, audit delivery, cloud deployment, ticket, PR or GitHub
    mutation.

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
8. Integrate the Issue #3 ingestion pipeline and Issue #4 diagnostics through
   their approved contracts; preserve legacy compatibility paths until tested
   replacements exist.

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
Issue #4 audit/delivery diagnostics and R1 UI integration
```

Issue #6 may define and test its boundary before Issue #7 repositories exist,
but must not invent their canonical schemas. Issue #14/ADR 0018 remains
authoritative throughout.

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
  before readiness.

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

## Further Notes

The local filesystem recommendation is intentionally the smallest adapter for
the approved single-deployment target, not a claim that a filesystem is a
universal object-storage service. The contract is designed so that a future
S3-compatible implementation can replace it without changing SourceRevision,
Access Policy, canonical acceptance, projection or citation semantics.

No GitHub issue data could be retrieved in this session because the configured
GitHub API connection was unavailable. The repository sources and the user
provided Issue #6 scope were used; the issue body/comments were not modified.
