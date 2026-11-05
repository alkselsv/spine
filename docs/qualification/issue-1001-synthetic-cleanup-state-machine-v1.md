# Gmail synthetic cleanup state machine v1

**Contract:** `synthetic-cleanup-job:v1` in
`spine.domain.synthetic_cleanup`. **Qualification status:** deterministic domain
contract only. The contract uses fake opaque identifiers, performs no I/O, and
has no production composition or deletion authority. `VERIFIED_SYNTHETIC` means
only that every declared fake store supplied a synthetic verified receipt.
It must never be consumed as permission to delete production data.

The contract intentionally has no source selector, provider identifier, Gmail
payload field, artifact field allowlist, retention duration, credentials, store
adapter or production authorization field. Every model forbids extra fields.
Opaque identifiers use a closed vocabulary of synthetic fixture constants;
arbitrary strings, including arbitrary values with a `fake-` prefix, are rejected.

## States and transition outcomes

| Current state | Event/input | Next state | Fence / safe result |
|---|---|---|---|
| PROPOSED | authority missing | BLOCKED | No cleanup; `AUTHORITY_MISSING` |
| PROPOSED | authority stale | BLOCKED | No cleanup; `AUTHORITY_STALE` |
| PROPOSED | authority revoked | BLOCKED | No cleanup; `AUTHORITY_REVOKED` |
| PROPOSED | synthetic test authority current | FENCED_TEST_ONLY | Test-only fence on; no deletion permission |
| FENCED_TEST_ONLY | START with freshly checked current synthetic test authority | CLEANUP_PENDING | Test-only fence remains on |
| FENCED_TEST_ONLY | START with missing, stale or revoked authority | BLOCKED | No dispatch; authority safe code |
| PENDING / INCOMPLETE | one fake store fails | CLEANUP_INCOMPLETE | Fence remains; `STORE_FAILED` |
| PENDING / INCOMPLETE | fake store result unknown | UNKNOWN | Fence remains; unresolved |
| PENDING / INCOMPLETE | all declared stores verified | VERIFIED_SYNTHETIC | Fence remains; synthetic evidence only |
| PENDING / INCOMPLETE | commit acknowledgement unknown | UNKNOWN | Fence remains; `COMMIT_UNKNOWN` |
| UNKNOWN | receipt missing, incomplete, duplicate or still unknown | UNKNOWN | Fence remains; reconciliation required |
| UNKNOWN | explicit complete receipt, all stores verified | VERIFIED_SYNTHETIC | Fence remains; fake receipts only |
| UNKNOWN | explicit complete receipt with failure | CLEANUP_INCOMPLETE | Fence remains; retry same operation key only after current fake authority |
| INCOMPLETE | RETRY with current synthetic test authority rechecked on this attempt | CLEANUP_PENDING | Same immutable operation key; retry count increments |
| INCOMPLETE | RETRY with missing, stale or revoked authority | BLOCKED | Fence remains; safe authority code; no retry |
| Any state | CANCEL | CANCELLED | Fence on; no restart transition |
| Any state | restore requested | State unchanged | Restore quarantine on; replay marked pending and any prior replay result cleared |
| VERIFIED_SYNTHETIC with replay pending | successful synthetic restore replay | VERIFIED_SYNTHETIC | Restore quarantine may release in this model only after replay evidence |

Invalid transitions preserve the state and return `INVALID_TRANSITION`.
`BLOCKED` has no path back to cleanup on that operation. An unknown commit can
only be resolved with an explicit receipt listing every required fake store;
unknown/missing receipts remain `UNKNOWN`. Partial or uncovered stores cannot
produce synthetic verification. Duplicate verified store results are stable;
replayed `START` after work has started is an unchanged no-op, including after
verification. Each retry event carries a fresh authority result; missing,
stale or revoked authority blocks the job before another attempt can start.
Initial dispatch also requires a fresh current-authority result on `START`;
missing, stale or revoked results block dispatch even after an earlier positive
check.

## Production boundary

This is a pure domain model. No API, worker, persistence, cleanup coordinator,
provider or storage adapter imports it. Its state and receipt types are scoped
under a synthetic namespace, and the only positive authority value is
`CURRENT_TEST_ONLY`. Any future production cleanup requires separately approved
authority and implementation work after HD1/HD3/HD4/HD6; this contract itself
does not authorize or claim physical deletion.

Restore remains quarantined by default. The only fence release represented is
after `VERIFIED_SYNTHETIC` plus an explicit successful synthetic replay event.
This demonstrates state semantics only; it does not qualify any real restore
topology.
