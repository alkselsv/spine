# Gmail synthetic failure and recovery vectors v1

**Version:** `gmail-option-b-recovery-vectors:v1`

**Scope:** deterministic synthetic vector catalog and a store-effect fake. The
fake records only invented opaque IDs and safe result codes; it does not execute
cleanup, a state machine, or physical deletion. All vector qualification
statuses are `NOT_RUN` against any state-machine or production boundary.

## State/transition mapping

Vectors use the state names and event vocabulary proposed for #1001. This branch
is standalone and imports no #1001 code, so it has no hidden branch dependency.
Although #1001 is now merged, this PR remains standalone: its state mapping is
specification-only and its unit tests validate the vector catalog and fake-store
behavior, not the canonical #1001 transition semantics. A separate integration
task must execute these vectors against #1001.

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

The #1001 mapping is intentionally unqualified. Its current contract uses
`FAILED`/`UNKNOWN` store outcomes rather than a distinct outage result and does
not define an uncovered-store event; the cross-workspace, audit-redaction and
restore-read checks also need application/adapter boundaries. The harness uses
its own invented fake-reference vocabulary, while #1001 accepts only its closed
fixture enum. Keep those mappings `NOT_RUN` until the follow-up integration task
selects exact transitions, identifiers and safe codes without weakening the
#1001 fence invariants.

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

The pytest checks prove only that the vectors are complete as data, the fake
store is deterministic/idempotent/tenant-scoped, and the synthetic audit schema
rejects forbidden fields. They do not execute a cleanup state machine, real
storage adapter, PostgreSQL/RLS, backup restore or production telemetry. Those
qualification results remain `NOT_RUN` and require their named integration
boundaries plus HD5/HD6 approval.

The generic commit-fence recovery contract introduced by #1018 is a separate
application boundary and does not execute these synthetic cleanup vectors or
qualify Gmail cleanup.
