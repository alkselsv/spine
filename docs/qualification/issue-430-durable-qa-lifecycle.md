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
| AC4 | Duplicate dispatch/reconciliation contracts and Temporal server workflow execution |
| AC5 | PostgreSQL run/cancellation contract and deterministic workflow/API race cases |
| AC6 | PostgreSQL canonical run/message state and accepted-answer disclosure boundary |
| AC7 | Workspace authorization and PostgreSQL delivery isolation |
| AC8 | Legacy/API OpenAPI contracts and generated client export parity |
| AC9 | PostgreSQL 17 disposable harness and Temporal local test server; limitations recorded below |

## Verification

The repository CI commands for this qualification are:

```text
uv run pytest -q
uv run python -m compileall -q src tests
pnpm web:api:check
```

PostgreSQL evidence uses the existing disposable PostgreSQL 17 test harness.
Temporal evidence uses `WorkflowEnvironment.start_time_skipping()` and a real
Temporal test server with the registered workflow and activity adapters. No
production Temporal service or external model provider is claimed.

## Boundaries not exercised

- PostgreSQL server process restart and crash injection between every commit,
  outbox delivery, Temporal start, and terminal persistence boundary.
- A production Temporal cluster or worker restart/recovery test.
- Both PostgreSQL CAS winners under an orchestrated simultaneous completion and
  cancellation race; deterministic API/workflow tests cover the response policy,
  and the PostgreSQL contract covers persisted cancellation transitions.
- Physical exactly-once execution. Delivery and workflow start are at-least-once;
  the qualification asserts stable logical identities only.
- External provider execution or production authorization configuration.

These are deployment or fault-injection prerequisites and remain explicitly
outside this qualification report's claims.
