# Issue #509 physical recovery attempt v3

Status: **backup and isolated restore verified; production qualification remains
NOT_VERIFIED**.

The authorized retry reused the immutable coherent recovery set
`recovery:issue509-005` and restored it into a fresh empty database and
filesystem target. The manifest has durable quiescence, verified PostgreSQL and
original-storage components, and a released fence. Physical restore,
canonical-to-original reconciliation and a tenant-scoped metadata canary all
passed. The target remained non-serving and was stopped after evidence capture.

Two earlier restore attempts failed closed. The first established that roles
referenced by restored RLS policies must exist as restricted `NOLOGIN` roles
before `pg_restore`. The second established that the isolated verifier must use
the restored tenant-policy role while setting the approved workspace and
environment scope. Neither failed attempt published its staged filesystem.

The follow-up run recorded in
`issue-509-projection-recovery-attempt-v1.json` rebuilt the physical Context
Projection and verified candidate retrieval plus an application-level
fail-closed `unpublished` status. It did not perform a permitted-source read
through an active application publication. The complete product recovery path
therefore remains NOT_VERIFIED, and independent review of the final evidence is
still required.

The JSON companion contains only opaque references, counts, states and digests.
It contains no source content, filenames, credentials or raw errors.
