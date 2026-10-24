# Issue #562 composed projection publication and recovery

Status: **NOT_VERIFIED**.

The exact Q16-01 and R4 target is retained and readable. Both prerequisite
reports pin commit `1fa3e9ee5047c081c1113c8eabd699f995d140b9`, deployment
fingerprint `3a08998510a6e34a464de43df1887dac04d59156ec779b91257b621a4a9a0d66`,
authorization generation 2 and provider profile digest
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

The operator authorized bounded run `issue562-r6-003` in GitHub comment
`6385289811`, with authorization-generation amendment `6385357326`. Q16-01
passed on the exact target, including a fresh authenticated same-execution
observer capture, and R4 passed all five formats, lifecycle, retry, isolation
and tombstone scenarios.

The supported successor command was then exercised twice. Command
`174b4041-489a-4995-a8c9-d067707c74ac` was accepted while the dispatcher was
stopped and bound to the approved pre-validation fault profile. It failed before
a projection run, candidate or receipt existed, so the injected validation
boundary was not reached. To distinguish the fault seam from canonical
resolution, the profile was removed and clean command
`5107f424-355d-4d69-a65d-90ae324ce65c` captured a new boundary and was delivered
normally. It produced the same bounded `failed` status before a run, candidate
or receipt. In both cases active snapshot
`130886bf-c04d-5e9e-9222-d6d145859c68` and activation generation 1 remained
unchanged. No paid provider call was reached.

The current blocker is therefore the public successor resolver against the
exact post-R4 canonical boundary, not provider availability or the qualification
fault controller. Direct database inspection or repair is forbidden. The
machine-readable report remains `not_verified` with
`provider_compatibility_incompatible`; it records passed observations only for
changed-head fencing, failed-rebuild preservation and safe diagnostics.

Deterministic projection request, receipt, validation, publication, recovery,
profile-binding and fail-closed decision suites pass. The focused disposable
PostgreSQL outbox, recovery resolver/result and publication CAS contracts also
pass. These checks are supporting evidence only; they do not replace the
required composed live observations.

## Remaining live gate

Repair the successor resolver for the exact canonical state produced by R4 and
add a regression that runs the public successor path after parser,
original-storage and pre-acceptance retry histories. Then rerun Q16-01 and R4
against the repaired commit under a renewed exact-target authorization. The run
must produce all thirteen observations listed in the issue-specific runbook,
including both approved fault boundaries, stale CAS, tombstone behavior and
successor rollback. Any unexercised boundary or target mismatch remains
`NOT_VERIFIED`; direct database edits, unbounded calls and combining separate
deterministic/provider successes are forbidden.
