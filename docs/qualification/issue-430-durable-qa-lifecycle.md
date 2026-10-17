# Issue #430 durable Q&A lifecycle qualification

Contract: `spine.qa.ask-admission-cancellation:v1`, approved by #407 and bounded
by ADR 0004, ADR 0010, and the accepted Issue #11 execution boundary.

This qualification reuses the B01 slice contracts and tests. It exercises the
canonical admission contract against PostgreSQL 17 and starts the registered Ask
workflow against Temporal's local test server. API compatibility, disclosure,
outbox dispatch, and cancellation arbitration are also checked at their owning
public seams.

## Evidence matrix

The executable matrix in `tests/qualification/test_issue430_qualification.py`
maps each approved acceptance criterion to named tests. The machine-readable
report records evidence status and the integration boundaries exercised.

| Criterion | Evidence boundary |
| --- | --- |
| AC1 | PostgreSQL admission/replay records; rollback contract |
| AC2 | PostgreSQL replay, NFC-equivalent and concurrent retries; changed payload and tenant scope rejection |
| AC3 | Legacy synchronous route and cancellation compatibility; admission and conflict response contracts |
| AC4 | PostgreSQL child-process replay after persisted cancellation intent, including delivery of that exact persisted outbox event to Temporal after worker replacement; terminal-commit retry, expired outbox lease recovery, duplicate dispatch/reconciliation, and Temporal workflow recovery are also covered |
| AC5 | PostgreSQL CAS tests hold the winning transaction until the competing writer is observed waiting on the same run-row lock; cancel-before-start, in-progress, late, and repeated cancellation contracts are also covered |
| AC6 | PostgreSQL terminal retry rolls run, linked message, message receipt, and accepted disclosure back together, then persists one consistent winner; accepted-answer API disclosure boundary |
| AC7 | Durable admission denial for workspace, environment, and subject; identical not-found disclosure for foreign and unknown runs; API response, diagnostic, log, telemetry, and committed outbox payloads remain free of protected content |
| AC8 | OpenAPI export parity and generated admission/cancel request/response type examples compile and run in the frontend suite |
| AC9 | Exact PostgreSQL 17 disposable harness and Temporal local test server are named below; production-only boundaries are not claimed |

## Verification

The repository CI commands for this qualification are:

```text
uv run pytest -q
uv run python -m compileall -q src tests
pnpm web:api:check
pnpm web:check
```

PostgreSQL evidence uses the disposable PostgreSQL 17 test harness with the
migrated Spine schema and runtime role. The process-recreation test starts a
separate Python process, reconstructs its trusted context and persistence adapter
from configuration, and replays the command against the same database after
cancellation intent commits. It then reads the persisted cancellation event from
PostgreSQL, starts the matching Temporal run, replaces its worker, and delivers
that database-backed event through `AskCancellationDispatchConsumer`; the run
reaches a cancelled terminal result. The retry test starts a Temporal workflow, records
its activity task before replacing the worker, injects a PostgreSQL terminal
commit failure on the first activity attempt, then verifies Temporal retries
the activity and the canonical run reaches one terminal result. A related
PostgreSQL test proves a failed terminal transaction rolls back the run,
terminal message, accepted disclosure, and message receipt together before a
fresh adapter commits the replay. Outbox lease reclamation is exercised against
PostgreSQL. Temporal evidence uses
`WorkflowEnvironment.start_time_skipping()` and the local test server with the
registered workflow and activity adapters. One worker persists the execution
activity task and shuts down before an activity worker polls it; a fresh worker
consumes that task and completes the same workflow history. A separate case
routes a registered cancellation outbox event through the Temporal client and
persists the cancellation activity before worker replacement. PostgreSQL also
injects a commit failure at canonical terminal persistence, then recreates the
adapter and replays the same terminal result.
No production PostgreSQL failover, production Temporal service, production
authorization setup, or external model provider is claimed.

## Boundaries not exercised

- PostgreSQL server process restart/failover and production Temporal cluster
  behavior.
- Physical exactly-once execution. Delivery and workflow start are at-least-once;
  the qualification asserts stable logical identities only.
- External provider execution or production authorization configuration.

These are deployment or fault-injection prerequisites and remain explicitly
outside this qualification report's claims.
