# Gmail synthetic failure and recovery vectors v1

**Version:** `gmail-option-b-recovery-vectors:v1`

**Scope:** deterministic synthetic vector catalog and a store-effect fake. The
fake records only invented opaque IDs and safe result codes; it does not execute
cleanup, a state machine, or physical deletion. The catalog's per-vector
`NOT_RUN` fields preserve the status of this standalone fixture; they do not
represent the separate #1020 integration qualification.

## State/transition mapping

This document describes the original standalone fixture contribution. Its tests
validate the vector catalog and fake-store behavior; the #1020 follow-up imports
the canonical #1001 state machine and executes the applicable recovery vectors.
See [the #1020 integration qualification](issue-1020-synthetic-cleanup-integration.md)
for exact event mappings, outcomes, safe-code differences and limitations.

| Vector | #1001 starting state → event mapping | Expected #1001 state | Required deny/fence | Qualification |
|---|---|---|---|---|
| partial_delete | `CLEANUP_PENDING` → store A verified, store B failed | `CLEANUP_INCOMPLETE` | Reads, publication, checkpoints denied; operation fenced | NOT_RUN |
| store_outage | `CLEANUP_PENDING` → store unavailable | `CLEANUP_INCOMPLETE` | Same | NOT_RUN |
| missing authority | `PROPOSED` → `CHECK_AUTHORITY` missing | `BLOCKED` | No dispatch; access remains denied | NOT_RUN |
| stale authority | `PROPOSED` → `CHECK_AUTHORITY` stale | `BLOCKED` | No dispatch; access remains denied | NOT_RUN |
| revoked authority | `PROPOSED` → `CHECK_AUTHORITY` revoked | `BLOCKED` | No dispatch; access remains denied | NOT_RUN |
| cross_workspace | `FENCED_TEST_ONLY` → mismatched workspace command | `BLOCKED` | Generic deny; no foreign effects/evidence | NOT_RUN |
| duplicate_retry | `CLEANUP_INCOMPLETE` → retry same operation key with fresh current test authority; replay same command | `VERIFIED_SYNTHETIC` after every required receipt verifies | No duplicate logical fake effect; access remains fenced | NOT_RUN |
| cancellation | `CLEANUP_PENDING` → `CANCEL`, then late result | `CANCELLED` | Fence retained; late result cannot resume work | NOT_RUN |
| unknown_commit | `CLEANUP_PENDING` → `COMMIT_UNKNOWN`, no receipt | `UNKNOWN` | No retry under a new key; all reads/publication/checkpoints denied | NOT_RUN |
| restore quarantine/replay | `VERIFIED_SYNTHETIC` → `RESTORE_REQUESTED`, restrictions pending, replay succeeds | `VERIFIED_SYNTHETIC` | Restore stays quarantined until replay evidence; the synthetic deleted record remains unreadable after replay; this vector does not qualify real restore | NOT_RUN |
| audit/log redaction | `CLEANUP_PENDING` → forbidden field emitted | `CLEANUP_INCOMPLETE` | Reject event; safe code only | NOT_RUN |
| missing/uncovered store | `CLEANUP_PENDING` → uncovered store detected | `CLEANUP_INCOMPLETE` | No completion while coverage unknown | NOT_RUN |

The catalog additionally pins each expected safe code, access-path denial, and
qualification status in `tests/qualification/test_issue1002_synthetic_recovery_vectors.py`.

The integration qualification in #1020 resolves applicable mappings and records
which behaviors have no corresponding #1001 state-machine event. In particular,
the contract has no separate outage or uncovered-store event; cross-workspace,
audit-redaction and restore-read checks stay at their fake boundary. The
integration report records those exact safe-code/state differences without
weakening the #1001 fence invariants.

## Fake-store boundary

`FakeStoreHarness` has a fixed synthetic workspace, a declared set of fake
stores and fake artifacts, optional store outages, a stable operation identity,
and recorded effects. Runtime checks accept only a closed vocabulary of fixture
IDs; tenant and current-test-authority checks run before duplicate-result
replay. It can demonstrate duplicate replay, tenant mismatch, outage and
uncovered-store behavior. It has no content bytes, real source
selectors, provider interface, credentials, retention terms, or production
composition. `SyntheticAuditEntry` rejects extra fields including body, address,
provider ID, attachment name and digest.

The original pytest checks prove that the vectors are complete as data, the fake
store is deterministic/idempotent/tenant-scoped, and the synthetic audit schema
rejects forbidden fields. The #1020 report separately records state-machine and
fake-boundary results. Neither qualification executes a real storage adapter,
PostgreSQL/RLS, production backup restore or production telemetry; those results
remain `NOT_VERIFIED` and require their named integration boundaries plus HD5/HD6
approval.

The generic commit-fence recovery contract introduced by #1018 is a separate
application boundary and does not execute these synthetic cleanup vectors or
qualify Gmail cleanup.
