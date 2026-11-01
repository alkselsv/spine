# Issue #795 private ASR storage: port-to-adapter mapping

Initial implementation base: `fdf3381e5a7b666c41b92edaca38ddd205490fab`.
Rebased onto current main: `7ea1b230dc57fe917667e5882386df8fc0551c46`.

This mapping is the implementation boundary for the synthetic qualification
store. Opaque `PrivateReference` values are identifiers only; they never grant
read or write authority.

| Existing port or contract | Current scope inputs | Concrete mapping | Authorization and atomicity |
| --- | --- | --- | --- |
| `OriginalReferencePort.bind_immutable_original` | admitted source; digest/length; no context | `PostgreSQLPrivateASRStorage` immutable source object row | Refine to carry trusted context and staging identity; resolve admitted scope and source lineage against persisted rows before copying verified bytes. |
| `DerivedStagingPort` | admitted source on begin; opaque ref only on append/finalize/discard | Same store, staging rows and bounded payload | Refine every operation to carry trusted context. Resolve reference in database under scope before touching payload. |
| `PrivateAnnotationStoragePort` | record id/version/payload; no source, scope or context | Same store, immutable annotation row | Refine to bind exact admitted source, trusted context, digest, retention and audit evidence. Existing shape cannot safely implement the issue. |
| `PrivateReportStoragePort.store_private_report` | payload, retention ref, audit ref, zero-argument async callback | Same store, one PostgreSQL transaction | Scoped refinement carries trusted context, stable report/idempotency key and case-to-exact-revision bindings. Prepare as uncommitted row, lock source/policy/revocation state, invoke callback, commit payload/manifest/receipt together. |
| `CleanupPort` | reference only | Tombstone state in same store | Refine to carry trusted context and operation purpose; cleanup is idempotent and non-disclosing. |
| `RevocationFencePort` | revision id/reason; no context | Source fence rows in same PostgreSQL schema | Refine with trusted context and scope. Fence updates serialize with report commit locks. |
| `RetentionExpirationPort` | reference and policy ref; no context | Expiry and tombstone fields in same store | Refine with trusted context and current approved-profile resolver; reject expired profiles before data access. |
| `AuditReceiptPort` | operation/revision/outcome; no scope/context | `PostgreSQLPrivateASRAuditReceipts` through Spine's existing UoW `audit` writer | Refine with trusted context. Receipt uses the canonical immutable audit event and contains no payload or content digest. |
| Existing generic object store | workspace/environment-scoped original object references and filesystem lifecycle | Not used as the report commit backend | Its filesystem finalization and PostgreSQL reference transaction cannot commit atomically. Retained for other source originals; no cross-store atomicity claim. |
| Existing PostgreSQL UoW and canonical persistence | verified `TrustedPersistenceContext`, policy and source records | PostgreSQL async engine/session and migration schema | PostgreSQL is the single visibility and recovery boundary for these restricted artifacts. Scope queries remain explicit and context is verified before each operation. |
| Existing retention applicability and audit | approved retention applicability; canonical audit writer | Injected current-profile reader plus canonical UoW audit writer | Applicability alone does not supply a duration or retention expiry. The adapter requires an approved current-profile reader; deployment wiring and commit-time locking are qualification gates. |

## Contract findings

The #771 interfaces listed above do not uniformly accept
`TrustedPersistenceContext`. Several accept only `PrivateReference`, whose
contract explicitly says it is non-resolving. Implementing them as-is would
make workspace/environment authorization unprovable. The implementation adds
context-aware scoped protocols and keeps the frozen protocols and signatures
available for existing callers. The #772 private-real runner uses only the
scoped report and aggregate protocols. The synthetic-only runner is unchanged.
Existing application tests exercise the scoped call shape; a direct legacy
implementation contract test remains outstanding.

The report callback is awaited inside the PostgreSQL transaction after staging
and verification. It is immediately followed by commit, while the current
policy pointer and source-fence rows are locked. The scoped source object is
checked in the same transaction; the immutable exact-revision record is
rechecked through the current-authority contract. The durable visibility
linearization point is PostgreSQL transaction commit. A source revocation that
acquires its shared fence first wins and prevents report visibility; a report
commit that acquires the fence first wins before the revocation. This guarantee
requires storage revocation and AccessPolicy activation to use the same
PostgreSQL locks. The current retention-profile reader and source-revision head
reader do not expose transaction-lock participation; their updates remain a
contract gap for strict commit-time TOCTOU proof. An external authority that
does not participate in the protocol remains outside the guarantee and must
not be represented as transactionally fenced.

Original bytes are checked against `SourceRevision.original_sha256`; the canonical
`revision_digest` identifies the revision record and is not the audio-byte digest.
Report source rows retain a record-id to revision-id pair, and the adapter rejects
bindings that do not match report outcome record references. Aggregate export
rechecks its callback inside the PostgreSQL insertion transaction, but the current
callback does not supply a lock handle for an independent privacy-policy authority.
Its audit receipt is emitted by the canonical UoW on a separate transaction. The
aggregate callback and receipt are therefore not a single cross-port atomic unit;
that remains a contract gap for a receipt whose meaning must prove successful
publication.

## Qualification limits

## NEEDS INFO — acceptance incomplete

This branch provides a concrete PostgreSQL adapter, but it does not complete
Issue #795 acceptance. Current SourceRevision-head and retention-profile
authorities cannot enlist in the adapter transaction or expose shared lock
handles. Aggregate privacy authorization likewise has no shared transaction
interface, and the canonical audit UoW owns a separate transaction. A bounded
contract refinement is needed for those authorities before the callback can
prove current authorization at the visibility point. The runner supplies
case-to-revision bindings from its validated manifest; the storage adapter
checks that every pair matches a report outcome and an admitted original, but
there is no independently queryable manifest authority at the storage boundary.
The PostgreSQL acceptance test also substitutes a no-op for the canonical
source-row lock. These gaps mean exact stale-head/retention and aggregate
authorization races remain unproven. Keep #795 open/in-progress and do not use
this adapter for real recordings until the contract and named-environment
qualification gates are met.

The focused PostgreSQL test proves report rollback on callback denial/cancellation,
stable duplicate identity, lost-response replay, case-to-revision link persistence,
wrong content digest rejection and tenant-scope rejection on the pinned test
database. The test replaces the canonical source-row lock with a no-op, so it does
not prove AccessPolicy lock privileges or remote authority serialization. It also
does not exercise aggregate export. The adapter does not prove production
encryption-at-rest, backup deletion, physical erasure, operator access controls,
or named-environment wiring. Those remain `NOT_VERIFIED` pending operator
evidence.
