# Canonical processing admission serialization

This inventory is the implementation contract for `SourceProcessingAdmission:v1`
and content-only `CanonicalAcceptance:v2`. It supplements ADR-0018. Adding a
writer for any authority below requires updating this inventory and a concurrency
test before enabling that writer.

## Canonical acceptance lock order

The PostgreSQL canonical UoW acquires serialization points in this order and
holds them until commit or rollback:

1. source lifecycle advisory locks, sorted by Source Object ID (shared);
2. workspace/environment canonical generation counter row;
3. exact source/revision admission advisory lock;
4. `CurrentSourceHead` row;
5. Provider Connection advisory lock keyed by logical connection ID (shared);
6. Bitrix24 root-policy current row, then content-policy current row;
7. trusted canonical selector current-head row;
8. exact current Provider Connection/approval rows;
9. Bitrix24 grant-authority row;
10. current lifecycle-authority row;
11. current exact-source retention-applicability row;
12. current AccessPolicy row;
13. active projection pointer rows.

Immutable revision, provenance, receipt, verifier, selector-version, policy-version,
admission-decision and evidence rows are resolved under those current-authority
fences. A row reread without its listed serialization point is not a substitute.
Known PostgreSQL deadlock/serialization aborts reopen the whole UoW and re-resolve
all authority with the same idempotency key and command digest. An unknown commit
outcome uses the same path so the idempotency receipt reconciles the original
result instead of creating another acceptance.

## Mutable writer inventory

| Authority | Writer | Shared serialization point |
|---|---|---|
| Provider Connection/version and approval | `PostgreSQLProviderConnectionManagementRepository.register` and provider revocation/configuration paths | exclusive logical-connection advisory lock; acceptance and selector publication take the shared form |
| Bitrix24 S1 grant fence/token epoch | `PostgreSQLBitrix24OAuthRepository` refresh/revoke/recovery paths | logical-connection advisory lock followed by `bitrix24_grant_authorities` row |
| Root/content policy | `PostgreSQLBitrix24DiskPolicyRepository._publish` | current root/content policy rows, root before content when both are consumed |
| Acceptance selector | `PostgreSQLCanonicalAcceptanceSelectorRepository.publish` | connection advisory lock, root/content current rows, then selector current-head row |
| Lifecycle eligibility | `PostgreSQLLifecycleAuthorityRepository.append_decision` | exclusive source lifecycle advisory lock followed by current lifecycle row; acceptance takes shared advisory form first |
| Retention applicability | `PostgreSQLRetentionApplicabilityRepository.append` | current target applicability row |
| AccessPolicy | canonical AccessPolicy activation repository | current source policy row and its existing CAS |
| Canonical source currentness | canonical acceptance repository; legacy tombstone/content compatibility writers use the same repository | generation counter, exact revision advisory lock and `CurrentSourceHead` CAS |
| Deletion-evidence qualification | `PostgreSQLTombstoneAuthorizationRepository.record_evidence` | separately authorized qualifier service principal; it cannot be the tombstone issuer |
| Deletion-policy approval | `PostgreSQLTombstoneAuthorizationRepository.record_policy_approval` | exact source-contract/connection/scope current approval row |
| Trusted tombstone issuer | `PostgreSQLTombstoneAuthorizationRepository.record_issuer` | workspace/environment current issuer row |
| Tombstone authorization issuance | `PostgreSQLTombstoneAuthorizationRepository.issue` | exact `CurrentSourceHead`, current deletion-policy approval, then current issuer; the injected commit verifier must acquire the existing connection/scope, lifecycle, retention and legal-authority fences before returning eligible |
| Tombstone authorization revocation/supersession | `PostgreSQLTombstoneAuthorizationRepository.record_decision` | exact current authorization-decision row after resolving the current issuer |

There is no second canonical source store or processing-current pointer. Provider
reads, byte download/verification and parser execution stay outside this lock
order and outside the canonical transaction.

## Expand and rollback

- `20261227_95` is the reader-first expansion. It adds immutable processing
  admissions, explicit acceptance version/link columns, composite FKs, checks,
  indexes, RLS and immutability, while a database trigger keeps v2 writes off.
- A later ticket may remove only that writer fence after #727 and #729 are
  complete. Application composition also fails closed unless a current
  mode-specific processing authority is supplied.
- Contracting `20261227_95` is refused when v2 history exists. Historical v1 rows
  keep schema version 1 and receive no fabricated processing link, selector or
  approval.

Google v1 and explicitly selected Yandex/filesystem v1 compatibility continue
through their persisted v1 semantics. Bitrix24 S2 provenance cannot fall back to
v1 when its processing admission is missing.

## Qualified deletion authority rollout and rollback

- `20270111_110` is an additive S3-5 migration. It persists content-free
  `QualifiedDeletionEvidence:v1`, versioned deletion-policy approvals, trusted
  issuer versions, exact `TombstoneAuthorization:v1` records and append-only
  authorization decisions. Tenant RLS, composite foreign keys, immutable-row
  triggers, command/head uniqueness and generation-guarded current pointers are
  installed before any writer is composed.
- Each authorization references the immutable canonical acceptance for its
  expected source revision. `cas_version` remains the exact issuance snapshot
  checked against `CurrentSourceHead`; no foreign key points back to that mutable
  pointer, so the authorized transition can advance it.
- This migration does not enable a canonical tombstone writer, provider mutation,
  physical cleanup, live qualification or Bitrix24 deletion. The S3-6 writer must
  revalidate the authorization and every current authority in its own canonical
  UoW; an issued authorization alone has no effect on `CurrentSourceHead`.
- Operational rollback disables the issuance composition first and leaves the
  additive readers and tables in place. Accepted authorizations and decisions are
  never rewritten or backfilled. Alembic downgrade is safe only before these
  tables contain authority history; after first use, schema contraction requires a
  separately reviewed retention-preserving migration.
- Missing, stale, expired, revoked, superseded, mismatched or unsupported evidence,
  policy, issuer, head, retention or legal authority returns the typed
  `TOMBSTONE_NOT_AUTHORIZED` outcome. Exact command replay remains stable after a
  later revocation, while a changed digest conflicts and a second authorization
  for the same expected head is denied.
