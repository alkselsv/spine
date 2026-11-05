# Issue #812: Final synthetic qualification

**Qualification date:** 2026-11-05

**Repository:** `alkselsv/spine`

**Current main at final reconciliation:** `c8d3cc2df8558d6da4f038ba93db67e27d9badf0`

**Decision:** Synthetic/offline technical acceptance is **VERIFIED**. Production deletion status is **DENIED**.

This additive record reconciles earlier evidence with the retained-v1 qualification in #955/#959 and negative authorization parity in #956/#961. The ASR qualification implementation commit is `56973fa3b544e9cc15e7620e3857a3388dae5362`; current main has since advanced to `c8d3cc2df8558d6da4f038ba93db67e27d9badf0`. The intervening #960 Google Sheets identity-classification change, #964 Yandex Mail end-to-end qualification, and #962 Gmail offline qualification do not touch ASR storage, its tests, or this qualification evidence. Earlier evidence records are unchanged. Only synthetic identities and payloads were used. This record is not authority to process real data, approve a retention duration, or delete production artifacts.

## Approved decisions and implementation

The Project Owner decisions recorded in #812 on 2026-11-04 approve HD-0 through HD-9 and C2. In brief: independent exact per-source bindings and one whole-artifact deletion decision; current applicability and approved terms are re-resolved per source; holds or loss of permission deny deletion/use as applicable; no partial deletion, inferred lineage, automatic v1-to-v2 conversion, new retention authority, or universal deadline; frozen v1 plus additive identity-keyed v2; historical data is retained; external approval/revocation fencing and production authority remain separate.

Merged implementation and qualification PRs:

| PR | Merge commit | Contribution |
|---|---|---|
| #937 | `bdf80c6a0f534faaf2f1c2d43fea0cb2287faad4` | Frozen deletion-contract v1; additive v2 and identity-keyed lineage |
| #938 | `a073c52b41fbc2155e2a50ccc103ec6aac8d1f89` | Composite retention and visibility semantics |
| #940 | `db03537bf41476a7bb4d0ec180f3f4100b25d927` | Per-source current report retention evidence |
| #942 | `ab8a4f993cc709c94af90fd05bd9cf5e666cad89` | Identity-keyed aggregate retention evidence |
| #948 | `75e8e4d76cb7532dcd61ecc0b27b53024d120ddf` | Current authority re-resolution and atomic whole-artifact deletion |
| #953 | `fff7dcdbedd746d88806cee2602da003a65686ca` | Multi-source PostgreSQL race, atomicity, lineage and RLS qualification |
| #954 | `06d34eae19a09d31718f8df14493d0354911b344` | MSR-A7 evidence record |
| #959 | `ee3d075e4648dc4372c5f79016fed7ed500c0faa` | Retained pre-v2 single-source PostgreSQL row qualification (#955) |
| #961 | `56973fa3b544e9cc15e7620e3857a3388dae5362` | Remaining PostgreSQL negative authorization parity (#956) |

## Qualification evidence and tested commits

- **#953 multi-source PostgreSQL batch:** CI run [41993850347](https://github.com/alkselsv/spine/actions/runs/41993850347), exact tested PR head `0561e45e79bc407112a5c50cd8dc0ea7b91b06e6`, success. Mandatory repository suite: 6,872 passed, 7 skipped, 2 deselected, 1 warning; exit 0. Targeted `tests/integration/postgresql/test_postgresql_private_asr_storage.py`: 119 passed, exit 0, `postgres:17.6-bookworm`, `spine_runtime`, `rolbypassrls=false`.
- **#955/#959 retained-v1 PostgreSQL batch:** exact PR head `aaf3051c39c547c01774a5a517f842380df6c07c`; CI run [42009077765](https://github.com/alkselsv/spine/actions/runs/42009077765), same head, all 7 jobs passed (Python 3.10–3.14, web, R1). The recorded storage module result is 128 passed, exit 0, PostgreSQL 17.6, `spine_runtime` with `NOBYPASSRLS`; focused legacy cases: 9 passed. The historical run record identifies local tested code as `50cd7fdb`; CI is verified against the exact PR head stated here.
- **#956/#961 negative-parity PostgreSQL batch:** exact PR head `2093a522563d23faa50d8e6d8336e3cef987d18e`; CI run [42018181596](https://github.com/alkselsv/spine/actions/runs/42018181596), same head, all 7 jobs passed (Python 3.10–3.14, web, R1). The storage module reported 141 passed, exit 0, PostgreSQL 17.6, `spine_runtime` with `NOBYPASSRLS`. Thirteen added negative cases cover five scope/identity variants, five invalid-grant variants and three changed-current-retention variants.
- The above tested heads are distinct from their merge commits. All three merge commits and the earlier implementation commits are ancestors of qualified main `56973fa3b544e9cc15e7620e3857a3388dae5362`. No new full PostgreSQL batch was run for this document-only reconciliation.

## Acceptance matrix

Each row names the proof boundary and the commit actually tested. “Synthetic VERIFIED” does not mean a production external authority has been fenced.

| #812 criterion | Evidence / exact test or record | Tested commit and run | Result |
|---|---|---|---|
| Exact workspace, environment, purpose, immutable artifact identity/kind/action/state digest, exact source/revision lineage and digest, current applicability/profile/generation, deadline, holds/exceptions, authority version and validity are bound | v1/v2 contract tests, including `test_v1_serialization_and_digest_remain_stable`, v2 canonical identity-keyed binding and rejection tests; PostgreSQL `test_postgresql_private_asr_composite_report_deletion_binds_every_current_source` | Contract: #937 merge `bdf80c6`; PostgreSQL: #953 tested head `0561e45e79bc407112a5c50cd8dc0ea7b91b06e6`, run 41993850347 | **VERIFIED — synthetic** |
| Expiration processing, deletion authorization, tombstoning, payload visibility removal and minimal audit are distinct typed/transactional steps | Whole-artifact PostgreSQL success and rollback tests; one tombstone, cleared primary payload visibility and one audit in the existing UoW | #953 tested head `0561e45e79bc407112a5c50cd8dc0ea7b91b06e6`, run 41993850347 | **VERIFIED — synthetic** |
| Spine-owned authority remains current through commit at the canonical PostgreSQL transaction/lock boundary | Current applicability, approved terms, AccessPolicy and source lifecycle are re-resolved under canonical PostgreSQL locks; validity is checked at deletion commit. See `test_postgresql_private_asr_composite_report_deletion_binds_every_current_source`, `test_postgresql_private_asr_changed_current_retention_denies_deletion_without_mutation`, and `test_postgresql_private_asr_expired_deletion_authority_rolls_back_audit_and_tombstone` | #953 tested head `0561e45e79bc407112a5c50cd8dc0ea7b91b06e6`; #961 tested head `2093a522563d23faa50d8e6d8336e3cef987d18e`, run 42018181596 | **VERIFIED — PostgreSQL-owned synthetic authorities only**; external approval/revocation leases remain #813 |
| Serialize report publication, authorized reads, cleanup/deletion and source revocation with canonical lock ordering | `test_postgresql_private_asr_authorized_read_serializes_with_expiration`; `test_postgresql_private_asr_publication_serializes_with_source_revocation`; `test_postgresql_private_asr_multi_source_lock_order_is_stable`; `test_postgresql_private_asr_opposite_source_order_serializes_without_deadlock` | #953 tested head `0561e45e79bc407112a5c50cd8dc0ea7b91b06e6`, run 41993850347 | **VERIFIED** for PostgreSQL-owned lifecycle races; **NOT VERIFIED** for external authority revocation races |
| Missing/stale/ambiguous authority, active hold, unresolved exception, wrong workspace/environment, action, artifact, forged references or lineage mismatch deny without mutation | `test_postgresql_private_asr_deletion_scope_and_identity_denials_preserve_artifact`; `test_postgresql_private_asr_invalid_deletion_grant_denies_without_payload_read_or_mutation`; `test_postgresql_private_asr_changed_current_retention_denies_deletion_without_mutation`; composite tampered-binding, stale-source, hold-any-source and lifecycle-withdrawal tests | #961 tested head `2093a522563d23faa50d8e6d8336e3cef987d18e`, run 42018181596; #953 tested head `0561e45e79bc407112a5c50cd8dc0ea7b91b06e6`, run 41993850347 | **VERIFIED — synthetic** |
| Invalid, expired, wrong-scope or request-mismatched deletion grants deny | `test_postgresql_private_asr_invalid_deletion_grant_denies_without_payload_read_or_mutation` (five variants) and `test_postgresql_private_asr_expired_deletion_authority_rolls_back_audit_and_tombstone` | #961 tested head `2093a522563d23faa50d8e6d8336e3cef987d18e`, run 42018181596; #953 tested head `0561e45e79bc407112a5c50cd8dc0ea7b91b06e6`, run 41993850347 | **VERIFIED** for represented grant variants. The authorization result carries a `revocation_epoch`, but the v2 request has no expected epoch and the adapter does not compare one. Mutable external epoch fencing is **NOT VERIFIED** and belongs to #813. |
| Whole-artifact authorization; partial authority or any contributor's hold/revocation cannot partially mutate a shared artifact | `test_postgresql_private_asr_composite_report_deletion_binds_every_current_source`; `test_postgresql_private_asr_composite_report_deletion_denies_active_hold_on_any_source`; `test_postgresql_private_asr_lifecycle_withdrawal_denies_composite_deletion_for_either_contributor`; partial/missing/extra/duplicate/permuted/forged binding cases | #953 tested head `0561e45e79bc407112a5c50cd8dc0ea7b91b06e6`, run 41993850347 | **VERIFIED — synthetic** |
| Legacy v1 request serialization/digest stays frozen; v2 is additive and identity-keyed | `tests/application/test_private_artifact_deletion_contract.py::test_v1_serialization_and_digest_remain_stable`; v2 canonicalization and lineage rejection tests | #937 merge `bdf80c6a0f534faaf2f1c2d43fea0cb2287faad4` (contract implementation); included in subsequent main CI | **VERIFIED** |
| Historical pre-v2 single-source JSONB row can be read/expired without rewrite; current authority is re-resolved | `test_postgresql_private_asr_legacy_single_source_retention_row_reads_and_expires_without_rewrite`; stale retention, stale AccessPolicy, lifecycle withdrawal, ambiguous/forged lineage, malformed/incomplete/substituted/mixed-version and cross-workspace cases | #959 exact tested head `aaf3051c39c547c01774a5a517f842380df6c07c`, run 42009077765 | **VERIFIED — synthetic**; original JSONB remains unchanged |
| No implicit migration, backfill or inferred v1-to-v2 conversion | Legacy JSONB equality before/after in `test_postgresql_private_asr_legacy_single_source_retention_row_reads_and_expires_without_rewrite`; v1/v2 contract tests; no migration in #955/#956 | #959 tested head `aaf3051c39c547c01774a5a517f842380df6c07c`; #937 merge `bdf80c6` | **VERIFIED** |
| Restricted `spine_runtime` does not bypass RLS | `test_postgresql_private_asr_authority_rls_grants_and_manifest_immutability`, plus runtime assertions in legacy and negative suites | #953 tested head `0561e45e79bc407112a5c50cd8dc0ea7b91b06e6`; #959 head `aaf3051c39c547c01774a5a517f842380df6c07c`; #961 head `2093a522563d23faa50d8e6d8336e3cef987d18e` | **VERIFIED — test DB only**, `NOBYPASSRLS` |
| Atomic tombstone, primary payload visibility removal and audit; audit failure rolls back; retry is idempotent | `test_postgresql_private_asr_composite_deletion_audit_failure_rolls_back_and_retries_once`; legacy read/expire without rewrite; negative tests assert unchanged artifact and no success audit | #953 tested head `0561e45e79bc407112a5c50cd8dc0ea7b91b06e6`; #959 head `aaf3051c39c547c01774a5a517f842380df6c07c`; #961 head `2093a522563d23faa50d8e6d8336e3cef987d18e` | **VERIFIED — synthetic** |
| No payload SELECT before authorization | `test_postgresql_private_asr_invalid_deletion_grant_denies_without_payload_read_or_mutation`; scope/identity and changed-current-retention denial tests also instrument SQL projection; earlier composite tampered-authority and audit-failure cases | #961 tested head `2093a522563d23faa50d8e6d8336e3cef987d18e`, run 42018181596; #953 tested head `0561e45e79bc407112a5c50cd8dc0ea7b91b06e6` | **VERIFIED** for qualified denial paths and SQL projection instrumentation |
| Concurrent authorized read versus deletion/expiration | `test_postgresql_private_asr_authorized_read_serializes_with_expiration` | #953 tested head `0561e45e79bc407112a5c50cd8dc0ea7b91b06e6`, run 41993850347 | **VERIFIED — synthetic** |
| Publication versus source revocation | `test_postgresql_private_asr_publication_serializes_with_source_revocation` | #953 tested head `0561e45e79bc407112a5c50cd8dc0ea7b91b06e6`, run 41993850347 | **VERIFIED** only for PostgreSQL-owned source lifecycle revocation; external production revocation is **NOT VERIFIED** |
| WAL, replica and backup erasure limitations | #930 evidence and PostgreSQL tombstone/visibility semantics; no independent physical erasure evidence | #954 tested head `ac4ddd43a2b0220f4559369544ad2488f6c1570d`, run 41998227255; #959/#961 preserve the same limitation | **NOT VERIFIED / operator obligation**; tombstone is not physical erasure |

## PR #923 supersession matrix

**Disposition: FULLY SUPERSEDED.** Its old single-source v1 implementation is incompatible with approved HD-8 and must not be merged or cherry-picked. The required contract and replacement security behavior are on main:

| PR #923 area | Current-main replacement and evidence |
|---|---|
| Deletion request and retention binding | #937 frozen v1 plus additive identity-keyed v2; golden v1 digest and v2 canonicalization/rejection tests |
| PostgreSQL current-authority and whole-artifact deletion mechanics | #948 re-resolution, lock boundary, atomic tombstone/payload visibility/audit and idempotency; #953 integration qualification |
| Multi-source lineage, holds, source lifecycle, races, audit rollback and RLS | #953 tests and run 41993850347, exact tested head `0561e45e79bc407112a5c50cd8dc0ea7b91b06e6` |
| Retained historical pre-v2 row behavior | #955/#959, exact tested head `aaf3051c39c547c01774a5a517f842380df6c07c`, run 42009077765 |
| Remaining scope/action/artifact/grant/current-terms/deadline/exception denials and no-payload-select parity | #956/#961, exact tested head `2093a522563d23faa50d8e6d8336e3cef987d18e`, run 42018181596 |

No unique necessary security guard from #923 remains uncovered in the synthetic PostgreSQL scope. External approval/revocation fencing was not supplied by #923 and remains #813; it is not implied by PostgreSQL source-lifecycle race tests.

## Authority boundary and remaining obligations

**Fenced synthetically in the PostgreSQL transaction:** Spine-owned source lifecycle and exact source links, current AccessPolicy, current RetentionApplicability/generation and terms/hold/exception resolution, artifact state and deletion transition, and transactional tombstone/payload-visibility/audit writes. Canonical lock ordering serializes the qualified PostgreSQL-owned read, publish, deletion and source-revocation paths through their database linearization points.

**External authorities left to #813:** any approval, terms, manifest-release or deletion authority whose current approval/revocation state is owned outside this PostgreSQL transaction. #813 must define and test a bounded lease/fence through commit, revoke-versus-commit outcomes, expiry, stale generation and retry-after-revocation. PostgreSQL-owned source lifecycle races do not qualify this external fence.

**Synthetic authority boundary:** the PostgreSQL tests use the fixed test-only authority, which returns a bounded grant with a fixed epoch; it is not a mutable external approval source. The database re-resolves and fences Spine-owned authority state through commit. No claim is made that the grant epoch is compared against an independently changing external epoch. The approved synthetic scope permits this fixture-only authority; #813 is required when such authority is externally governed.

**Technical decision:** all #812 synthetic/offline acceptance criteria are met. #813 remains a separate production blocker, as the approved #812 scope explicitly permits synthetic technical acceptance without it. This does not relax #812's commit-boundary requirement for the Spine-owned PostgreSQL authorities that were exercised.

- **VERIFIED:** synthetic multi-source and legacy single-source PostgreSQL behavior, exact v1/v2 compatibility, tested negative authorization parity, restricted test role, transactional rollback/retry and the stated PostgreSQL concurrency races.
- **NOT VERIFIED:** external approval/revocation fencing; physical WAL/replica/backup erasure; any production role, deployment, real-data workflow or deletion.
- **BLOCKED:** production composition and production deletion pending #813 and separate governance/environment decisions. #813 remains OPEN and unchanged.
- **OUT OF SCOPE:** real customer recordings, new retention durations, legal basis, Security/Legal/Data/Operations approvals, production deletion authorization and physical-erasure claims.

The only remaining follow-up is #813 and production governance/operational qualification. Those are not missing technical acceptance criteria for #812's approved synthetic scope.
