# R1 projection rebuild, recovery, and successor rollback

**Contract:** O05 / #296 request → `projection_request` outbox → worker →
physical adapter → canonical result bridge.

**Production qualification: not approved.** A deterministic adapter and
disposable PostgreSQL evidence do not qualify a configured provider.

## Prerequisites

- A current authorized workspace/environment, projection kind, explicit
  incident/change reference, and a least-privilege administrator identity.
- The configured projection adapter and actual `projection_request` worker
  route are healthy. Confirm current active snapshot, canonical boundary,
  activation generation, config version, freshness/health and reconciliation
  before requesting work.
- A new idempotency key for one logical operator request. Retry the same
  accepted command with the same key; do not mint a new command to hide an
  unknown result.

If route composition is unavailable or reports `unavailable`, do not bypass the
outbox or call the memory adapter directly. Record the reason code and escalate.

## Permissions

Only an authorized administrator scoped to the same workspace and environment
may read status or request retry/rebuild/recovery. All requests retain the
trusted scope and audit context. The worker resolves canonical inputs and owns
physical execution; the operator never edits snapshot rows, CAS pointers, or
adapter storage.

## Procedure

1. Read `GET /api/v1/projections/{projection_kind}/status`; capture only the
   safe snapshot, boundary, freshness, health, reason code, and generation.
2. When approved by the incident owner, submit
   `POST /api/v1/projections/{projection_kind}/recovery` with one of the
   contract actions `retry`, `rebuild`, or `recover`, plus the required
   `Idempotency-Key`. The command returns `202 accepted`; that is only durable
   request acceptance, never completion.
3. Follow the same request through the `projection_request` outbox, worker
   delivery, physical result, canonical result bridge, validation/sealing, and
   publication status. Confirm one logical terminal result despite redelivery.
4. Publish only a physically validated, sealed successor through the existing
   canonical activation CAS. On failure, keep the active pointer unchanged and
   preserve the safe terminal code.
5. Rollback is a new validated successor restoring prior projection behavior;
   it is not historical pointer rewind. The public recovery action enum has no
   `rollback` action. Use only a separately approved successor-publication
   operation if one is composed for the deployment. If it is not available,
   stop and escalate; never write the active pointer directly.
6. Read status and lineage again. Confirm the expected active generation,
   boundary, snapshot/config identities, and reconciliation evidence before
   closing the incident.

Use the authenticated Control Plane client for these routes. It supplies the
trusted bearer identity plus `X-Workspace-ID`, `X-Environment-ID`, trace
context, and audit context; do not paste tokens or invent service credentials
into shell history.

## Evidence

Record command/intent ID, request and action versions, idempotency outcome,
worker delivery reference, safe result code, physical validation/seal IDs,
canonical successor/activation generation, boundary/config identity,
reconciliation result, UTC timestamps, scoped operator role, and audit/change
reference. Evidence is immutable, tenant-scoped, and content-free. Do not
record prompts, answers, source text, provider errors, credentials, stack traces,
or raw adapter payloads.

## Stop and escalate

Stop on authorization denial, scope/digest mismatch, stale/tombstoned/foreign
input, lost lease, unavailable worker/adapter, duplicate logical result,
missing canonical bridge, invalid physical evidence, failed validation,
or activation CAS conflict. Do not publish an unvalidated/mixed successor,
directly mutate canonical state, or turn missing evidence into success. Escalate
to the projection owner with the safe reason and evidence IDs.

## Recovery boundary

Recovery reuses the #296 request and existing lifecycle; it does not add a
second API, ledger, or recovery model. Rollback is successor publication. No
Cognee/provider production readiness is inferred from deterministic adapter or
PostgreSQL tests.

## Exercise

Run the O05 success, redelivery, terminal rejection, validation, publication,
and successor-rollback exercises against synthetic fixtures and disposable
PostgreSQL:

```bash
uv run pytest -q \
  tests/application/test_projection_recovery_execution.py \
  tests/application/test_projection_publication.py
```

See [O05 traceability](../qualification/issue-364-projection-recovery.md) for
the acceptance-criterion evidence map. The configured provider remains an
unverified deployment prerequisite. When the owned disposable PostgreSQL
container runtime is available, exercise the durable worker handoff and
publication CAS:

```bash
uv run pytest -q \
  tests/integration/postgresql/test_postgresql_outbox_delivery.py::test_postgresql_recovery_request_replay_dispatches_one_tenant_scoped_intent \
  tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_projection_publication_satisfies_cas_contract
```

If unavailable, record those integration nodes as not run.
