# Gmail Option B synthetic threat and qualification matrix v1

**Status:** planning artifact. Every scenario is `NOT_RUN`; there are no PASS
claims, security acceptance, physical-erasure claims, or production qualification
results in this matrix. Data and provider inputs are synthetic. #876 Option C
NO-GO remains effective. HD1–HD6 and decision gate #998 remain open.

**Version:** `gmail-option-b-threat-matrix:v1`

**Scope:** proposed future Option B controls and test boundaries only. No Gmail
transport, credentials, real payload, production cleanup composition or
retention duration is authorized.

## Threat and qualification cases

| ID | Assets | Trust boundary | Threat / event sequence | Expected deny or fence outcome | Required evidence | Responsible owner | Planned test layer | Qualification |
|---|---|---|---|---|---|---|---|---|
| GOB-T01 | Synthetic authority snapshot, cleanup operation, affected source generation | Authority resolver → cleanup coordinator | Authority is revoked or generation advances after proposal but before dispatch/commit | Deny dispatch/commit; keep access and publication fence active; operation remains incomplete/blocked | Exact authority generation at resolve, dispatch and commit; stale worker negative result; audit with redacted refs | Security architecture + authority owner; Operations for run behavior | Deterministic fake race first; PostgreSQL serialization/RLS integration later | NOT_RUN |
| GOB-T02 | Parsed/body/attachment content and active reads | AccessPolicy / source lifecycle → API, worker, Context Broker | Read/process begins before fence; cleanup fence wins concurrently | No new read/process after current fence; in-flight lease handling follows approved semantics; no content leaks in denial | Concurrent read/process trace, current-generation decision, no-body response/log assertions | Security + application/access owner | Fake contract; PostgreSQL concurrency; API ASGI; projection/cache integration | NOT_RUN |
| GOB-T03 | Publication, projection inputs, outbox/checkpoint | Fence → outbox/projection/provider checkpoint | Stale worker publishes or advances checkpoint after fence | Reject commit/publication/checkpoint; hold retry for reconciliation under current authority | Commit-fence evidence, outbox/projection status, checkpoint unchanged, stale-worker denial | Security + workflow/projection owner + Operations | Fake; PostgreSQL transaction race; projection and worker integration | NOT_RUN |
| GOB-T04 | Originals and all registered derivatives | Cleanup coordinator → multiple stores | One store deletes, another fails or is unavailable | Keep fence; report incomplete with unresolved store; never mark verified | Per-store synthetic receipts and unresolved count; retry evidence; no completion while any failure/unknown remains | Storage owners + Operations | Fake store contract; object-store/PostgreSQL/projection integration separately | NOT_RUN |
| GOB-T05 | Idempotency key, store receipt, evidence ledger | Caller/retry worker → cleanup command boundary | Duplicate request or retry after partial/unknown response | Reuse the same operation identity; completed effects remain stable; no duplicate logical effect | Same-key replay observations, stable result, call/effect counts, receipt reconciliation | Application owner + Operations | Deterministic fake; persistence contract; crash/restart integration | NOT_RUN |
| GOB-T06 | Cleanup fence and work item | Cancellation caller → coordinator/store workers | Cancellation races dispatch, store commit or evidence write | Stop future effects at safe boundary; preserve fence; expose incomplete/cancelled state; reconcile any uncertain commit | Event ordering, cancelled work receipt, no unsafe resume or second key | Operations + application owner | Fake cancellation injection; durable workflow integration when approved | NOT_RUN |
| GOB-T07 | Store result and idempotency receipt | Store adapter → coordinator | Commit may have happened but acknowledgement is lost | Remain unresolved (`UNKNOWN`/incomplete); do not assume deletion or retry with a new identity; explicit receipt reconciliation required | Lost-ack vector, receipt lookup, no success before authoritative fake reconciliation | Storage owner + Operations | Fake receipt harness; adapter crash/restart integration | NOT_RUN |
| GOB-T08 | Restored database/object/projection data and deletion restrictions | Backup operator → isolated restore → serving systems | Older backup restores content whose cleanup restriction is current | Keep restore quarantined; deny reads, processing and publication until current restriction replay and synthetic verification succeed | Restore manifest, quarantine controls, replay watermark/currentness, negative reads/publication before release | Operations/backup owner + Security + Data | Fake replay vector; isolated PostgreSQL/object restore; projection/cache integration | NOT_RUN |
| GOB-T09 | Workspace-owned records, store receipts and errors | Workspace boundary → command/store/audit | Cross-workspace (cross-tenant) case: Workspace A requests or learns Workspace B artifact/evidence | Deny generically; no foreign effect and no protected text/identifier in output, audit or logs | Two-workspace isolation cases, RLS policy evidence, non-disclosure assertions | Security + PostgreSQL/storage owners | Fake contract; PostgreSQL RLS integration; API authorization | NOT_RUN |
| GOB-T10 | Audit events, logs, traces, metrics and error codes | Application → telemetry/audit sinks | Payload, address, provider ID, attachment name, secret or unapproved digest is emitted | Reject/omit forbidden fields; no payload-bearing exception propagation; safe code only | Forbidden-field scanner over synthetic sinks and negative schema tests | Security + audit/observability owner | Pure schema/unit; log capture; deployed telemetry scan later | NOT_RUN |
| GOB-T11 | Artifact/store registry and derivative graph | Inventory → cleanup completion evaluator | A store/class/copy is missing, unknown, newly introduced or lacks attestation | Fail closed; report uncovered scope; no `VERIFIED`; preserve fence | Versioned registry, class-to-store coverage, unknown-store negative case, change-detection evidence | Storage architecture + Data + Operations | Static inventory checklist; fake uncovered-store vector; integration coverage attestation | NOT_RUN |
| GOB-T12 | Provider-derived source state and local copies | Provider status → local store inventory | Provider outage/ambiguous absence is mistaken for successful deletion | Do not infer absence/deletion; local operation is `NOT_VERIFIED`; keep fence and unresolved count | Synthetic outage/ambiguous response, safe status, no unauthorized provider retry | Connector owner + Security + Operations | Fake only for preparation; no live provider test is authorized | NOT_RUN |
| GOB-T13 | Identity/provenance, hashes, tombstones, audit references | Cleanup evidence → future readers and joins | Internal UUID/hash/lineage/tombstone is presumed safe and retained without HD1 | Deny/omit every unapproved Gmail-derived field; no implicit allowlist | Schema/store join inventory and field-level decision evidence; absent decision remains denied | Legal/Data decide fields; Security reviews linkage; platform maps stores | Static schema inventory; synthetic forbidden-field tests | NOT_RUN |

## Evidence and acceptance rules

- All evidence must be synthetic, version-pinned, reproducible and bound to a
  named test boundary. A fake result qualifies only that fake boundary.
- Unit/fake results do not substitute for PostgreSQL/RLS, object storage,
  projection/cache, backup/restore or deployed telemetry evidence.
- A missing test, unsupported store, unknown commit, stale authority, incomplete
  artifact graph, or failed evidence check is `NOT_RUN`, `NOT_VERIFIED`, or
  failed as appropriate; none may be recorded as PASS.
- Security may accept only the threat/control and evidence actually exercised.
  Operations may accept only the run/recovery procedure exercised against the
  named topology. Legal/Data decisions and external Google/assessor outcomes are
  separate and cannot be inferred from tests.
- Completion criteria for any future cleanup must separately resolve every
  in-scope store and backup/restore obligation. Tombstones and logical fences do
  not prove physical erasure.

## Existing contracts to reuse

- [#812](https://github.com/alkselsv/spine/issues/812) contributes an
  authorization/current-authority/idempotency pattern and leaves backup/WAL/
  replica erasure an operator obligation unless proven.
- [#813](https://github.com/alkselsv/spine/issues/813) and
  [#976](https://github.com/alkselsv/spine/issues/976) own publication and
  revocation fencing; this matrix does not create a second fence service.
- #1016 adds current-source usage guards for general source read/process paths
  (`src/spine/application/source_usage_guard.py`,
  `src/spine/infrastructure/current_source_constraints.py`, and their focused
  tests). Those checks do not implement Gmail cleanup or qualify any row in
  this matrix; all 13 scenarios remain `NOT_RUN`.
- #857 owns email access/retention enforcement, not physical deletion.
- `RetentionApplicability` remains the single policy-applicability authority;
  it does not define a duration or authorize cleanup.

## Unresolved qualification gates

HD1 field allowlist, HD2 Google compatibility and external assessment, HD3
per-class policy, HD4 holds, HD5 backup disposition, and HD6 Security/Operations
acceptance remain pending at #998. The Project Owner-approved synthetic/offline
direction does not approve any of these specialist decisions or enable live
Gmail. This matrix is the active #1004 artifact; #1003 is a closed duplicate.
