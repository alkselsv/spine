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
| `RetentionExpirationPort` | reference and policy ref; no context | Tombstone fields in same store | Refine with trusted context and canonical transaction-aware applicability/terms authority. Current concrete `expire` denies before querying or mutating because no such authority exists. |
| `AuditReceiptPort` | operation/revision/outcome; no scope/context | `PostgreSQLPrivateASRAuditReceipts` through Spine's existing UoW `audit` writer | Refine with trusted context. Receipt uses the canonical immutable audit event and contains no payload or content digest. |
| Existing generic object store | workspace/environment-scoped original object references and filesystem lifecycle | Not used as the report commit backend | Its filesystem finalization and PostgreSQL reference transaction cannot commit atomically. Retained for other source originals; no cross-store atomicity claim. |
| Existing PostgreSQL UoW and canonical persistence | verified `TrustedPersistenceContext`, policy and source records | PostgreSQL async engine/session and migration schema | Report rows and source links share a PostgreSQL transaction. The canonical audit writer uses a separate UoW today, so audit receipt creation and artifact publication are not one transaction. |
| Existing retention applicability and audit | Approved exact-source/source-class binding, exact policy IDs/version/digest, applicability generation, authority approval evidence and current pointer | Canonical `RetentionApplicability` and its PostgreSQL current pointer are the only permitted applicability authority. No ASR retention adapter exists; the concrete ASR store denies retention-dependent reads, writes and expiry until one is implemented. | The #771/#772 `profile_ref` remains an opaque UUID in application request/report contracts and ASR schema. There is no FK, resolver, or verified mapping to policy ID/version/digest, applicability ID/generation, scope, source lineage, artifact class, or policy terms. Equality of UUIDs proves nothing. Applicability has no retention duration or artifact deletion deadline. No second registry is allowed. |

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

The former report implementation awaited the callback inside the PostgreSQL
transaction after staging and verification. Its transaction commit was the
visibility linearization point for the report row and its source links. Those
commit/idempotency paths are currently unreachable because retention authority
fails closed, and their previous fake-reader tests were removed. The current
implementation shares canonical source lifecycle and revision lock helpers in
`postgresql_source_locks.py`. Canonical admission and lifecycle mutation use
these helpers. ASR locks the lifecycle shared key, generation counter, exact
revision key, current source head, current lifecycle authority when present,
and current AccessPolicy pointer; its source revocation fence uses the same
lifecycle/generation/revision serialization path. Payload and source metadata
rows are read before obtaining row locks to avoid reversing the canonical
source-lock order. A missing or stale content head is denied.
The intended order is lifecycle shared lock, workspace/environment generation
counter, exact revision admission lock, current source head, current retention
applicability pointer, then current AccessPolicy pointer. D1 source-lock code is
**IMPLEMENTED, but PostgreSQL concurrency is NOT_VERIFIED**: the focused test
container could not start PostgreSQL 17.6, so head replacement, lifecycle
withdrawal, policy activation and opposite-order deadlock barriers have not run.
The previous no-op `_locked_source` substitution is not used by this adapter.

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
| D1 Source / AccessPolicy | Shared lifecycle and exact admission lock identities are now extracted into `postgresql_source_locks.py` and used by canonical admission, lifecycle mutation and ASR. ASR locks generation counter, exact revision, current head, lifecycle authority and AccessPolicy; stale/missing heads deny. | PostgreSQL lock ordering and races have not been exercised in this environment. | No port change. Preserve lifecycle shared/exclusive key and canonical source order. | Existing source/lifecycle tests pass; restricted-role barriers for report-vs-head replacement, withdrawal, policy activation, opposite entry order and deadlock absence remain required. | `IMPLEMENTED / POSTGRESQL NOT VERIFIED` |
| D2 Retention | Canonical `RetentionApplicability` and its current-pointer CAS remain the sole applicability authority. | No transaction-aware ASR resolver, approved policy-terms source, or expiry calculator exists. `profile_ref` is an opaque UUID and is not mapped to canonical applicability or policy identity. | Keep production fail-closed. A synthetic terms provider may only be test-only and must accompany a current canonical binding; no duration or approval is inferred. | PostgreSQL tests for current pointer, policy/binding approval, exact lineage/class, deterministic expiry from approved terms and concurrent update/commit. No authorized success path has yet been verified. | `NEEDS_INFO / BLOCKED BY POLICY TERMS AUTHORITY` |
| D3 Aggregate / audit | Runner creates the detailed report audit receipt before report storage; aggregate export also wrote its canonical receipt in a separate UoW before aggregate insertion. | Callback refusal leaves the pre-created report receipt. There is no shared transaction or durable pending/publication protocol for either report/receipt or aggregate/receipt. | Preferred bounded correction: append canonical audit through the same PostgreSQL UoW/session as the report or aggregate row; make the receipt identity available to the payload before commit and publish only after that commit. Concrete aggregate export now fails closed. | Callback denial/exception/cancellation and audit insert failure; assert no report, successful receipt, or aggregate is visible. Also stale/revoked privacy approval and concurrent release revocation. | `BLOCKED / NOT_VERIFIED` |
| D4 Manifest binding | #771 runner validates manifest digest and record admission; #795 persists report digest and caller-provided record/revision pairs and checks outcome membership. | Storage has no authoritative manifest resolver and cannot establish that a supplied pair belongs to the approved dataset/split or exact source digest. | Pass the immutable manifest snapshot plus a trusted resolver result to the scoped report port; independently verify digest, dataset/split, unique record membership and exact revision/digest before staging. Resolver authority must come from the existing governed dataset admission path. | Forged/substituted manifest, duplicate record, wrong split, wrong revision/digest and cross-scope manifest cases. | `NEEDS_INFO` |
| D5 Runtime role/source lock | The adapter now uses the actual shared lock helper and source/head/lifecycle/policy mappings. Existing test verifies forced RLS and no BYPASSRLS but exits at missing retention authority before source lock. | Lock grants, source/head reads and RLS behavior on the concrete path have not run under `spine_runtime`. | Do not mock or replace `_locked_source`. Add real synthetic canonical source/head/policy fixtures and call the concrete path. | Restricted-role current-head lock, role flags, cross-scope denial and proof rejected calls do not SELECT payload. | `IMPLEMENTED / POSTGRESQL NOT VERIFIED` |
| D6 Recovery / idempotency | The prior head tested transaction rollback, repeated key, concurrent duplicate and replay with a detached fake retention reader. Those tests were removed because that authority path was unsafe. | At the current head, retention denial prevents testing report persistence, retry or lost-response recovery through the concrete adapter. No process kill/restart or restore reconciliation is tested. | Re-enable these tests only against the canonical transaction-aware retention adapter; preserve stable identity and re-resolve current source, retention and policy before returning a replay. Keep restore reconciliation as an operator gate. | Process crash before/after commit when isolated PostgreSQL is available; restore-time tombstone and receipt reconciliation. | `NEEDS_INFO` for DB retry at current head; `OPERATOR_NOT_VERIFIED` for process crash/restore |

## NEEDS INFO — acceptance incomplete

This branch contains PostgreSQL storage schema and adapter mechanics, but it
does not complete Issue #795 acceptance. The adapter now refuses all ASR data
operations because no canonical transaction-aware retention and policy-terms
adapter exists. Source-head fencing, trusted manifest binding, and aggregate
authorization/audit atomicity also remain incomplete. Aggregate export fails
closed. Keep #795 open/in-progress and the PR draft until source, retention and
manifest contracts are verified and aggregate publication either shares the
canonical transaction boundary or remains unavailable.

### D2 canonical retention reconciliation

The only proven profile mapping today is **none**: `profile_ref` is an opaque
UUID in the #771/#772 application contracts and is persisted by the ASR schema.
No canonical applicability ID/generation or policy identity is stored
alongside it, and there is no adapter that binds the UUID to a
`RetentionApplicability`. The former synthetic profile reader was removed; it
was not canonical authority. The current concrete store unconditionally denies
retention-dependent reads and writes until a canonical transaction-aware
policy-terms adapter is implemented.

The domain taxonomy currently has general classes (`ORIGINAL`,
`PARSED_CONTENT`, `TRANSCRIPT`, `PROJECTION`, `CACHED_ANSWER`, `CITATION`).
ASR uses distinct additive candidates: source audio original, derived staging,
annotation, detailed report, sanitized aggregate and audit evidence. No
existing governed policy is confirmed to cover any of these private-ASR
classes. In particular, audit receipts are currently content-free canonical
audit rows, not source-derived audio artifacts; the candidate audit class is
reserved for evidence that actually carries source lineage and has no current
binding. A policy must list each applicable class and an authoritative lineage
resolver must provide every exact `SourceRevision` behind each derived object.

No authoritative `expires_at` calculation exists. The current domain binding's
`effective_until` only ends applicability validity. The former ASR test reader's
`expires_at` was not approved terms evidence. The adapter previously wrote
`admitted.authority.expires_at` onto original bytes, conflating processing
authority expiry with retention expiry; that timestamp-based profile path has
been removed pending canonical integration. Missing, stale, conflicting or ambiguous
policy terms, applicability, lineage or approval must deny storage and reads.

The canonical pointer CAS takes `current_retention_applicabilities` row
`FOR UPDATE` in `PostgreSQLRetentionApplicabilityRepository`'s UoW. Its current
reader is separate and read-only; it does not accept the storage session or
lock the pointer. A missing binding currently means ineligible, so the ASR
path must not create a pointer or grant on absence. A future ASR commit must
take the existing pointer row lock in the same transaction, using the
canonical order: source lifecycle shared lock, generation counter, exact
revision admission lock, current source head, exact-source retention pointer,
then current AccessPolicy pointer. This does not fence policy approval if that
approval is held by an uncoordinated external authority. Concurrent behavior
for current policy/binding withdrawal is therefore **not proven**.

The focused PostgreSQL test at the current head proves missing canonical
retention authority denies `begin`, report storage, expiration and payload
reads under the restricted runtime role. It asserts no artifact row, no
payload SELECT, and identical errors for missing/existing opaque references.
Earlier adapter mechanics checks were removed because they used a synthetic
profile reader as if it were retention authority. The current test does not
prove report callback transaction, idempotency, source locking or AccessPolicy
lock privileges. The adapter does not prove production
encryption-at-rest, backup deletion, physical erasure, operator access controls,
or named-environment wiring. Those remain `NOT_VERIFIED` pending operator
evidence.
