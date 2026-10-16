# R1 read-only quarantine inspection

**Contract:** O06 administrator metadata inspection.

**Production qualification: not approved.** This runbook never replays,
deletes, repairs, or acknowledges a delivery.

## Prerequisites

- An authenticated administrator session with explicit workspace and
  environment scope, an incident/change reference, and a reason to inspect.
- The deployment has composed the tenant-scoped inspection reader and approved
  authorization/audit path. If the response is `unavailable`, do not query the
  delivery table or logs as a fallback.

## Permissions

Use the authorized runtime administrator identity for the exact workspace and
environment. Requests are audited. The endpoint is read-only, page-bounded, and
keyset-paginated; database RLS and the application authorize scope. Do not use a
dispatcher, owner, superuser, or RLS-bypass identity for inspection.

## Procedure

1. Call `GET /api/v1/admin/outbox/deliveries/quarantined?limit=50` using the
   authenticated Control Plane client. The maximum page size is 100.
2. Interpret `empty`, `complete`, `partial`, and `unavailable` distinctly. For
   `partial`, continue only with the returned cursor, same scope, and same page
   size. A cursor from another scope/filter/page size is invalid.
3. Review only event ID, scoped IDs, attempt count, timestamps, bounded safe
   failure code, and correlation ID. Follow a trusted correlation only through
   the separate O07 view and its authorization boundary.
4. Record whether inspection was empty/complete/partial/unavailable and the
   safe event references. Route any needed retry, replay, or repair decision to
   its separately approved incident procedure; none is provided here.

## Evidence

Record schema version, scoped request/audit IDs, UTC inspection time, bounded
page state and count, opaque delivery/correlation IDs, safe error codes, and
incident reference. Do not copy raw envelopes, payloads, credentials, exception
messages, provider data, SQL, protected text, or another workspace's IDs.

## Stop and escalate

Stop on 401/403, scope mismatch, invalid cursor, `unavailable`, unexpected
content fields, unbounded results, or any indication that inspection mutated a
delivery. A denied response must not be used to infer whether a foreign event
exists. Preserve the safe status/request ID and escalate to the authorization
or persistence owner. Never bypass authorization or audit.

## Recovery boundary

This operation reads bounded metadata only. It does not retry or replay work,
change attempts, delete or repair quarantine rows, expose event payloads, or
disable audit. Diagnostic sink failure cannot be repaired by opening server
logs from this runbook.

## Exercise

Run the synthetic API contract and runtime-role PostgreSQL inspection exercises:

```bash
uv run pytest -q \
  tests/api/test_outbox_inspection.py \
  tests/application/outbox/test_dispatch.py
```

When the owned disposable PostgreSQL container runtime is available, prove
runtime-role RLS-scoped, safe read-only behavior:

```bash
uv run pytest -q tests/integration/postgresql/test_postgresql_outbox_delivery.py::test_runtime_inspection_is_rls_scoped_safe_and_read_only
```

This does not grant production access or authorize mutation. If the container
runtime is unavailable, record this integration node as not run.
