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
fails closed, and their previous fake-reader tests were removed. In that former
shape, the adapter locked
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
| D2 Retention | Domain `RetentionApplicability` binds exact source or approved source class + artifact classes to exact policy ID/version/digest, approvals, validity window and generation. `PostgreSQLRetentionApplicabilityRepository` writes immutable binding, audit/outbox and current-pointer CAS in one UoW. `RetentionEligibilityReader` resolves lineage and checks current/effective/approved applicability. | No concrete `RetentionArtifactLineageResolver`, policy-approval reader, binding-approval reader, approved policy-terms source, or policy expiry calculator was found in `src/`. The ASR profile reader/model were removed because they accepted detached `expires_at`; `profile_ref` does not map to canonical IDs or participate in the storage transaction. `effective_until` is applicability validity, not artifact expiry. | Reuse the current pointer and approval ports. Add a transaction-aware canonical resolver over the same UoW/session that returns the exact applicability ID/generation and policy identity only after locking and verifying the current binding and full source lineage. Separately add a governed approved-terms contract that resolves artifact-class starting event, duration/rule, exceptions/holds and deletion obligations and computes expiry. Until that policy-terms source exists, deny ASR writes/reads that require an expiry. Do not add an ASR registry or infer a term. Additive `PRIVATE_ASR_*` artifact enum values are taxonomy only, not policy approval. | Restricted-role PostgreSQL tests for locked pointer + generation CAS, stale/revoked policy/binding approval, exact lineage/class, expiry derivation from approved terms, and concurrent update/commit. A race is only fenced when pointer writers use the same row lock; external approval changes remain outside the transaction until their authority is coordinated. | `NEEDS_INFO / DATA-LEGAL TERMS + TRANSACTIONAL ADAPTER` |
| D3 Aggregate / audit | Runner creates the detailed report audit receipt before report storage; aggregate export also wrote its canonical receipt in a separate UoW before aggregate insertion. | Callback refusal leaves the pre-created report receipt. There is no shared transaction or durable pending/publication protocol for either report/receipt or aggregate/receipt. | Preferred bounded correction: append canonical audit through the same PostgreSQL UoW/session as the report or aggregate row; make the receipt identity available to the payload before commit and publish only after that commit. Concrete aggregate export now fails closed. | Callback denial/exception/cancellation and audit insert failure; assert no report, successful receipt, or aggregate is visible. Also stale/revoked privacy approval and concurrent release revocation. | `BLOCKED / NOT_VERIFIED` |
| D4 Manifest binding | #771 runner validates manifest digest and record admission; #795 persists report digest and caller-provided record/revision pairs and checks outcome membership. | Storage has no authoritative manifest resolver and cannot establish that a supplied pair belongs to the approved dataset/split or exact source digest. | Pass the immutable manifest snapshot plus a trusted resolver result to the scoped report port; independently verify digest, dataset/split, unique record membership and exact revision/digest before staging. Resolver authority must come from the existing governed dataset admission path. | Forged/substituted manifest, duplicate record, wrong split, wrong revision/digest and cross-scope manifest cases. | `NEEDS_INFO` |
| D5 Runtime role/source lock | #795 PostgreSQL schema uses forced RLS; the current focused test connects as `spine_runtime` and asserts `rolbypassrls = false`, `relrowsecurity` and `relforcerowsecurity`. It fails before invoking `_locked_source`. | It does not demonstrate canonical source-head locking or source/policy metadata reads under the runtime role. | Create real canonical source/head/policy fixtures and use the actual shared admission lock path; runtime must remain non-superuser and `rolbypassrls = false`. | Actual current head, revision and policy fixture; `FOR UPDATE` lock path under runtime role; assert RLS and required grants. | `NEEDS_INFO` |
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
