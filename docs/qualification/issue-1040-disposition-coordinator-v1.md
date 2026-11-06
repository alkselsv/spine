# Disposition coordinator v1

This implementation is an application contract over repository, admission and
store ports. Tests use deterministic in-memory fakes; they prove state-machine
behavior, not durable persistence or physical deletion. PostgreSQL admission,
outbox and state persistence are assigned to #1042, while current authority and
read/processing fences are completed by #1041 and #1047.

The coordinator scopes identity by workspace, environment and random operation
ID. A request digest binds the bounded open retry operation and cannot change on
replay. Admission atomically checks current deletion authority and persists the
fence through one port. Before every store effect, the coordinator checks
authority again. A partial failure remains fenced; an unknown or unregistered
store is `UNKNOWN` / `NOT_VERIFIED`.

Each store attempt is persisted as `IN_PROGRESS` before calling its handler. A
lost response or process cancellation therefore resumes through reconciliation
under the same operation ID. State updates use a monotonically increasing
version for compare-and-swap. Cancellation keeps the fence and stops later
stores. Completion requires every currently registered store to verify and is
compare-and-swap protected against concurrent cancellation.

The verified result and minimal audit are persisted while the response replay
window remains open. The deadline is 24 hours after completion. An explicit
acknowledgement or the expiry port purges the operation ID, request digest and
per-store state; the audit remains limited to the approved allowlist. Expiry is
evaluated with an injected timezone-aware clock. T9 still needs to wire a
reliable scheduled caller; until then this contract does not prove automatic
purge during an idle period.

## Executed evidence

- `tests/application/test_disposition_coordinator.py`: deterministic coverage
  for workspace/environment isolation, retry, cancellation races, authority
  revocation, changed store registry, explicit unknown commit, lost-response
  recovery, process cancellation, completion commit recovery and expiry.
- `tests/application/test_retention_applicability.py` and
  `tests/domain/sources/test_retention.py`: #820 mapping/applicability
  regression coverage.
- Domain framework-import boundary test: passed.
- PostgreSQL adapter persistence, canonical lineage removal, real object storage,
  indexes/caches, backup/restore and production topology remain unqualified.
