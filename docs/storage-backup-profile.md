# Coordinated PostgreSQL and original-storage backup profile

`r1-coordinated-backup:v1` implements the approved single-deployment backup
profile for PostgreSQL canonical state and the filesystem original-object volume.
The coordinator uses the existing storage recovery fence and quiescence contract;
the PostgreSQL and object-volume artifacts are independently verified before a
coherent manifest can be persisted.

## Components and evidence

- PostgreSQL is exported with a compatible `pg_dump` custom archive. A separate
  `pg_restore --list` process checks the archive before and after it is stored.
  The dump includes finalized original-reference associations. Inventory for
  cross-component consistency comes from verified, finalized canonical
  associations in PostgreSQL. The inventory adapter requires the trusted
  workspace/environment scope set and applies the same transaction-local RLS
  context and configured backup role used by `pg_dump`; it does not assume a
  BYPASSRLS principal or enumerate references under a different login role.
- The filesystem adapter creates a deterministic archive of finalized payloads
  and their private manifests. It compares the physical set with the canonical
  PostgreSQL reference set, verifies each source receipt and payload, and excludes
  staged uploads under the explicit restart/reconciliation policy.
- Both component artifacts are stored immutably in the configured protected
  destination and reread for digest verification. Component evidence records
  artifact reference and digest, tool profile/version, fence epoch, cutoff,
  inclusion set and policy, and independent verifier result.
- The coordinated manifest is coherent only when both component records match
  the same fence and inclusion set, quiescence completed, and no indeterminate
  storage operations remain. Failed or partial runs retain an incomplete
  manifest with a closed safe failure category.

## Operational profile

The profile configuration records daily cadence, 30-day retention, destination
identity, encryption-at-rest and protected-transfer profile references, and
separate least-privilege backup and restore identities. These values are
deployment bindings supplied by an operator; recording them is not proof that a
production destination has been provisioned or independently qualified.

Staged uploads are excluded. A deployment must restart or reconcile them before
they can be used. Finalized objects and canonical receipt/reference metadata are
included together. A failure to find a referenced finalized object, verify a
digest, preserve a common fence, write the destination, or run a verifier fails
the component or the coordinated set closed.

The profile does not implement a scheduler, enforce physical destination
retention, bind production credentials or destinations, perform an isolated
restore, or claim PITR, RPO or RTO. Isolated restore qualification belongs to
O04. Production activation requires validated tool versions, destination and
encryption bindings, retention enforcement, identities and credentials, an
isolated restore target, and an independent verifier. The trusted workspace and
environment inventory scope source must also cover every active tenant in the
deployment; an incomplete scope set makes the physical/canonical set comparison
fail closed.

Tests use synthetic data and disposable test targets. The normal PostgreSQL
integration test exercises real `pg_dump` and `pg_restore` when client tools
matching the pinned test server are available; otherwise it skips with the
missing compatibility prerequisite. This does not qualify a production backup
destination or count as an isolated restore.
