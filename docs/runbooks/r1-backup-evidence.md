# R1 coordinated backup and evidence review

**Profile:** `r1-coordinated-backup:v1`

**Production qualification: not approved.** This procedure describes the approved
single-deployment policy; it does not bind a destination, credential, scheduler,
or operator command.

## Prerequisites

- A change record identifies the deployment, profile version, complete trusted
  workspace/environment inventory, and the owner of the backup operation.
- The approved configuration binds a separate protected destination, encrypted
  storage and transport, daily cadence, 30-day retention enforcement, compatible
  PostgreSQL tooling, and the POSIX original-object volume.
- A deployment-owned operator composition invokes `spine-recovery-operator:v1`
  with the existing coordinator, PostgreSQL adapter, filesystem adapter,
  artifact store, and independent verifiers. The repository CLI validates the
  profile; it does not discover deployment resources or credentials.
- Staged uploads have a documented restart/reconciliation policy. Finalized
  original bytes, private manifests, and canonical receipt/reference metadata
  are in scope.

If the deployment has no reviewed coordinator composition or any binding above
is missing, stop before creating a backup. There is no backup HTTP route and the
profile-validation CLI cannot perform recovery by itself. Do not substitute
direct `pg_dump`, `tar`, ad hoc Python, or manual manifest writes for the
coordinated operation.

## Permissions

Use the approved coordinator identity to start the operation. Component
identities are distinct, least-privileged, and do not bypass RLS. The PostgreSQL
inventory and dump use the trusted workspace/environment scope set. A separate
independent verifier may read artifacts but cannot rewrite immutable evidence.
Use only the approved audit/control-plane path; do not disable required audit.

## Procedure

1. Confirm the deployment/profile version, scope inventory, destination identity,
   retention/encryption policy, tooling compatibility, and change approval.
2. Start the existing ADR 0022 fence. Wait for admission to stop, in-flight
   operations to drain, and quiescence evidence to reach its durable cutoff.
3. Invoke the deployment-reviewed coordinator once. The coordinator obtains a
   custom-format PostgreSQL archive and a filesystem archive of finalized bytes
   plus private manifests; staged uploads follow the recorded restart policy.
4. Review each independent component receipt: durable artifact identity, digest
   verification, verifier result, tool profile/version, fence epoch, cutoff,
   inclusion set, and policy identity. `pg_restore --list` is a format check,
   not proof of a successful restore.
5. Accept a coherent recovery manifest only when both components, the inclusion
   set, fence epoch/cutoff, quiescence evidence, and terminal operation state
   agree. Read back the immutable manifest and audit reference.
6. Release the fence only through the coordinator's normal completion path. A
   partial or indeterminate operation remains incomplete; never edit its
   manifest into a success state.

## Evidence

Record immutable evidence IDs and schema/profile versions, UTC observation time,
scope reference, fence epoch/cutoff, quiescence watermark, independent
PostgreSQL/filesystem verifier outcomes, coherent/incomplete result, safe failure
code, change/audit reference, and operator role. Keep payloads, database names,
filesystem paths, artifact URIs, credentials, digests, raw command output, and
provider errors out of the runbook record. Store protected low-level receipts
only in their approved evidence store and refer to them by opaque ID.

## Stop and escalate

Stop on lost fence/quiescence, incomplete scope inventory, unavailable verifier,
missing/corrupt object, digest mismatch, mixed fence/cutoff/inclusion set,
unbound encryption/retention, or any component reported unavailable,
indeterminate, or failed. Preserve the incomplete evidence and safe error code;
escalate to the storage and deployment owners. Do not retry against the same
immutable artifact identity or claim that command success proves recoverability.

## Recovery boundary

This creates a logical PostgreSQL/filesystem recovery set; it is not
`pg_basebackup`, WAL/PITR, failover, or an RPO/RTO guarantee. It does not restore
data. Live stores and projections remain untouched. Isolated restore is the
separate procedure in [R1 isolated restore](r1-isolated-restore.md).

## Exercise

Run the repeatable synthetic component and failure-path exercises:

```bash
uv run pytest -q \
  tests/application/storage/test_backup_profile.py \
  tests/infrastructure/test_backup_artifacts.py \
  tests/integration/object_storage/test_filesystem.py
```

These tests do not use a production destination or qualify deployment retention
and encryption. When the owned disposable PostgreSQL container runtime is
available, separately exercise the actual custom-format archive path:

```bash
uv run pytest -q tests/integration/postgresql/test_postgresql_backup_tools.py
```

If that prerequisite is unavailable, record it as not run, never as a pass.
