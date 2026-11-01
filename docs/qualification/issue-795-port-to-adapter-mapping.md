# Issue #795 private ASR storage: port-to-adapter mapping

Initial implementation base: `fdf3381e5a7b666c41b92edaca38ddd205490fab`.
Reviewed main base: `ddf676bad8e9f56e9e001ea122b19dba57ab4a97`.

This mapping is the implementation boundary for the synthetic qualification
store. Opaque `PrivateReference` values are identifiers only; they never grant
read or write authority.

| Existing port or contract | Current scope inputs | Concrete mapping | Authorization and atomicity |
| --- | --- | --- | --- |
| `OriginalReferencePort.bind_immutable_original` | admitted source; digest/length; no context | `PostgreSQLPrivateASRStorage` immutable source object row | Refine to carry trusted context and staging identity; resolve admitted scope and source lineage against persisted rows before copying verified bytes. |
| `DerivedStagingPort` | admitted source on begin; opaque ref only on append/finalize/discard | Same store, staging rows and bounded payload | Refine every operation to carry trusted context. Resolve reference in database under scope before touching payload. |
| `PrivateAnnotationStoragePort` | record id/version/payload; no source, scope or context | Same store, immutable annotation row | Refine to bind exact admitted source, trusted context, digest, retention and audit evidence. Existing shape cannot safely implement the issue. |
| `PrivateReportStoragePort.store_private_report` | payload, retention ref, audit ref, zero-argument async callback | Same store, report row and source links in one PostgreSQL transaction | Scoped refinement carries trusted context and stable identity. The runner currently creates the audit receipt before this call, so callback refusal does not roll that receipt back. Report plus successful receipt atomicity remains `NEEDS_INFO`. |
| `CleanupPort` | reference only | Tombstone state in same store | Refine to carry trusted context and operation purpose; cleanup is idempotent and non-disclosing. |
| `RevocationFencePort` | revision id/reason; no context | Source fence rows in same PostgreSQL schema | Refine with trusted context and scope. Fence updates serialize with report commit locks. |
| `RetentionExpirationPort` | reference and policy ref; no context | Expiry and tombstone fields in same store | Refine with trusted context and current approved-profile resolver; reject expired profiles before data access. |
| `AuditReceiptPort` | operation/revision/outcome; no scope/context | `PostgreSQLPrivateASRAuditReceipts` through Spine's existing UoW `audit` writer | Refine with trusted context. Receipt uses the canonical immutable audit event and contains no payload or content digest. |
| Existing generic object store | workspace/environment-scoped original object references and filesystem lifecycle | Not used as the report commit backend | Its filesystem finalization and PostgreSQL reference transaction cannot commit atomically. Retained for other source originals; no cross-store atomicity claim. |
| Existing PostgreSQL UoW and canonical persistence | verified `TrustedPersistenceContext`, policy and source records | PostgreSQL async engine/session and migration schema | Report rows and source links share a PostgreSQL transaction. The canonical audit writer uses a separate UoW today, so audit receipt creation and artifact publication are not one transaction. |
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
and verification. PostgreSQL transaction commit is the visibility
linearization point for the report row and its source links. The adapter locks
the current AccessPolicy pointer and its own source-revocation fence, but its
revision advisory lock differs from #800's canonical admission lock. It does
not lock or verify `current_source_heads`. AccessPolicy activation does lock
the current policy pointer, so that update path serializes with the adapter.
Source-head replacement, lifecycle withdrawal, and report commit do not yet
share a proven lock order. The #795 PostgreSQL test previously replaced
`_locked_source` with a no-op and does not prove the real source path. D1 and D5
remain `NEEDS_INFO` until a shared source fence is used and exercised under the
restricted runtime role.

Original bytes are checked against `SourceRevision.original_sha256`; the canonical
`revision_digest` identifies the revision record and is not the audio-byte digest.
Report source rows retain a record-id to revision-id pair, and the adapter rejects
bindings that do not match report outcome record references. It does not resolve
the report's manifest digest to an authoritative manifest or independently verify
record membership, split, dataset identity, or source binding against that
manifest (D4, `NEEDS_INFO`).

The concrete aggregate export path is disabled. It previously invoked an
independent privacy callback, committed a canonical audit receipt through a
separate UoW, and then inserted the aggregate in another transaction. That did
not provide one atomic authorization/audit/publication boundary. A new
PostgreSQL negative test asserts that no aggregate row is published and the
authorization callback is not invoked while the adapter is blocked (D3).

## Qualification limits

## D1–D6 decision matrix

| Decision | Existing contract and implementation | Exact gap | Minimal correction and compatibility | Required evidence | Status |
| --- | --- | --- | --- | --- | --- |
| D1 Source / AccessPolicy | #800 canonical acceptance takes the workspace/environment generation counter, `_lock_admission_revision`, current source head, then current AccessPolicy pointer. Policy activation locks that pointer row. #795 currently locks a separate revision advisory key and current policy row. | #795 neither uses the canonical source lock key nor locks/checks the current source head. Source-head and lifecycle writers are not proven to serialize with report commit. | Factor the canonical source lock and lock ordering into a shared infrastructure helper; lock the generation counter, exact revision admission key, source head, then current AccessPolicy pointer in #795. Keep frozen #771 signatures unchanged; the #795 scoped adapter remains additive. | Restricted-role lock/read test; barriers for report-vs-head replacement, policy activation, and lifecycle withdrawal; confirm all writers use that order. | `NEEDS_INFO` |
| D2 Retention | Spine has immutable `RetentionApplicability` plus a current pointer updated with CAS/audit in one UoW. #795 injects a `PrivateASRRetentionProfileReader`; integration uses a fake. | Generic applicability does not define retention duration or supply the ASR profile expiry. The profile reader has no transaction or fence token, so current/revoked checks race commit. | Reuse the existing retention authority and have its approved ASR profile resolution expose applicability identity/generation, expiry and a transaction-visible current pointer. Do not add another profile registry. Confirm the approved profile-to-applicability binding. | Real reader under restricted RLS role; lock current pointer in report transaction; expiry and concurrent pointer-revocation barriers. | `NEEDS_INFO / PROJECT OWNER DECISION` |
| D3 Aggregate / audit | Runner creates the detailed report audit receipt before report storage; aggregate export also wrote its canonical receipt in a separate UoW before aggregate insertion. | Callback refusal leaves the pre-created report receipt. There is no shared transaction or durable pending/publication protocol for either report/receipt or aggregate/receipt. | Preferred bounded correction: append canonical audit through the same PostgreSQL UoW/session as the report or aggregate row; make the receipt identity available to the payload before commit and publish only after that commit. Concrete aggregate export now fails closed. | Callback denial/exception/cancellation and audit insert failure; assert no report, successful receipt, or aggregate is visible. Also stale/revoked privacy approval and concurrent release revocation. | `BLOCKED / NOT_VERIFIED` |
| D4 Manifest binding | #771 runner validates manifest digest and record admission; #795 persists report digest and caller-provided record/revision pairs and checks outcome membership. | Storage has no authoritative manifest resolver and cannot establish that a supplied pair belongs to the approved dataset/split or exact source digest. | Pass the immutable manifest snapshot plus a trusted resolver result to the scoped report port; independently verify digest, dataset/split, unique record membership and exact revision/digest before staging. Resolver authority must come from the existing governed dataset admission path. | Forged/substituted manifest, duplicate record, wrong split, wrong revision/digest and cross-scope manifest cases. | `NEEDS_INFO` |
| D5 Runtime role/source lock | #795 PostgreSQL setup uses `spine_runtime` and forced-RLS tables. The existing report test monkeypatches `_locked_source` to a no-op. | It does not demonstrate canonical source-head locking or source/policy metadata reads under the runtime role. | Remove the no-op test substitution after creating the real canonical source fixture. Runtime must be non-superuser and `rolbypassrls = false`. | Actual current head, revision and policy fixture; `FOR UPDATE` lock path under runtime role; assert RLS and required grants. | `NEEDS_INFO` |
| D6 Recovery / idempotency | Transaction rollback, repeated key, concurrent duplicate and replay after a lost response are covered by the adapter test. Staging retry clears prior bytes. | No process kill/restart or restore reconciliation is tested. Current retry must re-resolve current source, retention and policy; those authority races are not covered. | Preserve stable artifact/report identity and re-run current authority/fence checks before returning a replay. Keep backup/restore reconciliation as an operator gate. | Process crash before/after commit when isolated PostgreSQL is available; restore-time tombstone and receipt reconciliation. | `TESTED` for DB transaction retry; `OPERATOR_NOT_VERIFIED` for process crash/restore |

## NEEDS INFO — acceptance incomplete

This branch provides a concrete PostgreSQL adapter, but it does not complete
Issue #795 acceptance. Source-head fencing, retention profile transaction
participation, trusted manifest binding, and aggregate authorization/audit
atomicity remain incomplete. Aggregate export now fails closed. Keep #795
open/in-progress and the PR draft until the source, retention and manifest
contracts are verified and aggregate publication either shares the canonical
transaction boundary or remains unavailable.

The focused PostgreSQL test proves report rollback on callback denial/cancellation,
stable duplicate identity, lost-response replay, case-to-revision link persistence,
wrong content digest rejection, tenant-scope rejection, and blocked aggregate
publication on the pinned test database. Its `_locked_source` no-op means it does
not prove canonical source locking or AccessPolicy lock privileges. The adapter does not prove production
encryption-at-rest, backup deletion, physical erasure, operator access controls,
or named-environment wiring. Those remain `NOT_VERIFIED` pending operator
evidence.
