# Issue #509 physical recovery attempt v2

Status: **FAIL; production qualification is not granted**.

An authorized synthetic SOURCE and a fresh isolated RESTORE were created for
`run:issue509-recovery-001`. The SOURCE accepted one synthetic document through
the public ingestion API and retained one verified canonical-to-original
association. SOURCE and RESTORE used distinct networks, databases, volumes and
identities.

Three coordinated recovery sets failed closed before quiescence produced a
coherent backup. Each attempt persisted an immutable incomplete manifest,
degraded its own fence and blocked further admission for that deployment-scope
reference. No coherent component set was promoted, so isolated restore,
projection recovery and the authorized canary were not started. RESTORE remains
empty, absent at its filesystem publication point and non-serving.

The exercise confirmed four concrete implementation/deployment gaps:

- tenant-scoped `pg_dump` needs row-security mode enabled;
- strict domain enums must be reconstructed explicitly from persisted strings;
- the backup identity needs an explicit, reviewed read inventory;
- the filesystem source currently needs profile-initialization access even for
  an export-only backup job.

The first two gaps are corrected in this change with regression tests. The
remaining blocker is that the coordinator collapses a pre-quiescence failure to
the generic `component_failure` category, preventing a trustworthy stage-level
diagnosis from the immutable evidence. Component probes passed independently,
but they are not promoted to a coordinated-backup result.

Temporary broad table reads were revoked after the exercise. No private data,
customer data, source text, credentials, raw provider errors, projection
publication or cutover claim is present in the metadata report. A new physical
attempt requires a new explicit authorization and an independent final evidence
review.
