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
