# R1 isolated restore and reconciliation

**Profile:** O04 / `IsolatedRestoreVerifier`

**Production qualification: not approved.** Only an empty, dedicated,
non-serving restore target is eligible.

## Prerequisites

- A coherent, currently trusted v1 recovery manifest, verified PostgreSQL and
  filesystem artifacts, matching fence/cutoff/inclusion evidence, and complete
  workspace/environment scope inventory.
- An owner-approved disposable PostgreSQL database named with the required
  `spine_restore_` prefix, an absent isolated filesystem target, compatible
  `pg_restore`, and a fresh isolated exercise/change record.
- Separate restore and verifier identities; the complete serving-identity set;
  no superuser, RLS bypass, inherited restore-owner privilege, or serving-role
  access to the target.
- Deployment-approved credentials, encrypted transfer, and an independent
  evidence reviewer. Keep values in the secret manager only.

If any deployment binding is missing, do not restore. The repository has no
public restore API or operator CLI; invoke only an owner-reviewed composition
entry point. Never point the verifier at a live database or use a serving
identity for restore.

## Permissions

The restore identity may create objects only in the dedicated empty database
and isolated filesystem target. The verifier has read-only access to those
targets and the protected artifact reference. Serving identities are denied
database and filesystem access. The operator is separately authorized to
request and review the exercise; no credential is copied into evidence.

## Procedure

1. Verify manifest trust and read back both immutable component receipts. Reject
   absent, corrupt, incomplete, indeterminate, or mixed recovery sets before
   invoking restore.
2. Confirm target identity and emptiness through the approved isolated-target
   owner. Recheck that no live service configuration points to either target.
3. Invoke the existing verifier against the chosen recovery manifest. It runs
   `pg_restore` with errors enabled and one transaction, applies isolated
   database grants, materializes regular files in private staging, verifies
   finalized receipts/digests, and reconciles restored canonical references.
4. Accept only a `verified` immutable `IsolatedRestoreEvidence` record read back
   from the promoted target. Verify references to finalized originals/private
   manifests, tenant/RLS isolation, staged-object exclusion/restart policy, and
   revision, tombstone, and watermark semantics.
5. Repeat the same evidence read. It must replay the stored result without
   restoring over or rewriting the target. Keep the target isolated after the
   exercise; do not register it with a serving service.

## Evidence

Record the manifest/evidence IDs and versions, isolated target aliases from the
approved deployment inventory, UTC time, verifier/tool profile version,
component outcomes, reconciliation result, tenant-isolation result, idempotent
read-back result, safe failure code if any, and audit/change reference. Do not
record database names, paths, object bytes, source text, credentials, raw SQL,
command output, or provider errors. Keep raw protected receipts in the approved
evidence store and cite opaque IDs only.

## Stop and escalate

Stop before promotion for a non-empty or wrongly named target, target identity
mismatch, incompatible tools, missing permission, archive or digest failure,
mixed fences, missing finalized bytes/private manifest, foreign tenant
reference, RLS failure, staged-object promotion, or reconciliation conflict.
Preserve the typed `rejected`, `unavailable`, or `indeterminate` outcome. Do not
retry over an existing target; provision a newly approved empty target after
owner review. Escalate to storage/security owners.

## Recovery boundary

The verifier changes only isolated targets and its immutable isolated evidence.
It does not repair the live store, alter active projections, or authorize
serving restored data. No physical production restore, PITR, RPO, or RTO claim
follows from synthetic tests.

## Exercise

Run offline verifier acceptance/rejection tests and the real disposable
PostgreSQL/filesystem exercise:

```bash
uv run pytest -q \
  tests/infrastructure/test_isolated_restore.py \
  tests/application/storage/test_recovery_integrity.py
```

When the owned disposable PostgreSQL container runtime is available, exercise
the actual isolated database and filesystem restore:

```bash
uv run pytest -q tests/integration/postgresql/test_isolated_restore.py
```

If unavailable, record that exercise as not run; deployment qualification still
requires the actual approved isolated targets and evidence reviewer.
