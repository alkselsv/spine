# Issue #562 composed projection publication and recovery

Status: **NOT_VERIFIED**.

The exact Q16-01 and R4 target is retained and readable. Both prerequisite
reports pin commit `9d75b1e0e8531ed3534806ab8fc5071901157ece`, deployment
fingerprint `47be5f0ac4e031de25a6b864ed9874a6a5a5acbef9653dd54fc7892cbf0ea823`,
authorization generation 16 and provider profile digest
`8935297e3def5e786f7e3d34db87d13c2106fbb7f82f8d695c747cee4fe1c84f`.

A content-free read through the supported projection-status and document-query
APIs found the retained active publication healthy but stale at activation
generation 1 with 13 canonical drifts. Its only member is an earlier content
revision whose current canonical head is now a tombstone. The current R4 state
contains nine content heads and two tombstones; none of the nine content heads
is present in the active snapshot.

The older physical run `issue507-composed-002` passed all ten #212 gates, but
its v2 report does not retain a source commit, deployment fingerprint, provider
profile digest or run-authorization reference. It therefore cannot establish
compatibility with the exact #562 target.

The operator authorized bounded run `issue562-r6-001` in GitHub comment
`6382335412`. A canonical recovery request was accepted as command
`d185f024-bbf9-41cb-9aee-d068fcb77be0`, then failed closed as
`projection.recovery.canonical_rejected` before a recovery run or physical
receipt was created. No paid provider call was reached. The active snapshot,
activation generation 1 and CAS version 1 remained unchanged.

The failure is expected for recovery from stale inputs: the resolver requires
every active member to remain the current content revision and correctly fences
the tombstoned predecessor. The retained runtime has no supported redrive or
normal-successor control path that can create a fresh candidate from the current
canonical boundary after the original projection deliveries are quarantined.
Direct database edits are forbidden. The machine-readable report therefore
remains `not_verified` with `provider_compatibility_incompatible`; it records
passed observations only for changed-head fencing, failed-rebuild preservation
and safe diagnostics.

Deterministic projection request, receipt, validation, publication, recovery,
profile-binding and fail-closed decision suites pass. The focused disposable
PostgreSQL outbox, recovery resolver/result and publication CAS contracts also
pass. These checks are supporting evidence only; they do not replace the
required composed live observations.

## Remaining live gate

Add a supported, idempotent normal-successor/redrive command that captures the
current canonical boundary and emits a new projection request without reviving
quarantined delivery rows. Then rerun Q16-01 and R4 against that new commit and
repeat `issue562-r6-001` under a renewed authorization. The run must produce all
thirteen observations listed in the issue-specific runbook, including failure
fencing, stale CAS, tombstone behavior and successor rollback. Any missing safe
injection mechanism or target mismatch remains `NOT_VERIFIED`; direct database
edits, unbounded calls and combining separate deterministic/provider successes
are forbidden.
