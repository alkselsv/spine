# Issue #86 follow-up hardening boundary

This note records the deliberately limited follow-up to Issue #86 and PR #139.
It does not implement document upload, Issue #6 persistence, Issue #95, or a
replacement receipt resolver.

## Verified-original boundary

- Issue #6 is authoritative for original-object storage integrity and its
  finalized verified receipt.
- A completed Issue #8 idempotency receipt is only an operation/result
  reference. It is not evidence that physical bytes were finalized or verified.
- A future ingestion binding may succeed only after an approved Issue #6
  integration confirms finalized/verified status and matching workspace,
  environment, object reference, digest, and byte length.
- Until that integration exists, content bindings fail closed in both
  persistence adapters and in the PostgreSQL trigger. They cannot create a
  binding, observation, or `observed` transition from a generic completed
  `original.upload` receipt.
- Tombstone bindings remain contentless and do not require an original object.

The rejected path is intentionally not represented as `VERIFIED`; absence of
authoritative evidence is a typed persistence conflict, not a successful
validation result.

## Scope and acceptance status

This follow-up retains the Issue #86 hardening for strict safe metrics,
PostgreSQL validation, idempotency, tenant isolation, concurrency, rollback,
replay mapping, and negative/fault-injection tests. AC-043--049, AC-084, and
the applicable AC-090 catalog checks are covered by the follow-up test suite.

AC-050 is intentionally partial: the fail-closed database-success invariant is
enforced, but the positive verified-original path and receipt replay cannot be
completed until Issue #6 exposes an approved persistence/resolver integration.
That integration is deferred to Issue #95 and must be implemented separately.
