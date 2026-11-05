# Canonical tombstone v3 rollout and rollback

Issue #736 adds a governed, tombstone-only `CanonicalAcceptance:v3`. It does not
perform provider deletion, physical cleanup, or any production tombstone.

## Reader-first rollout

1. Apply migration `20270112_111`. It keeps v1/v2 readable, adds the v3 columns
   and exact authorization foreign key, and installs immutable selector history
   plus a CAS-protected current selector head.
2. Deploy readers before publishing selectors. A missing, expired, ambiguous, or
   `DENY_NEW` selector rejects a new tombstone; there is no implicit legacy mode
   or downgrade.
3. Publish a source-object and exact-scope selector through an interactive
   `canonical_tombstone_selector.publish` context. `LEGACY_V1_ALLOWED` preserves
   an explicitly approved compatibility writer; `V3_REQUIRED` admits only the
   governed v3 path.
4. Enable v3 only after the provider-specific Google, strict Yandex and mounted
   filesystem contract suites pass. Observe authorization denials, optimistic
   conflicts, retry exhaustion and idempotency reconciliation before widening
   scope.

The v3 transaction re-resolves and locks the current selector, provider
connection and approval, qualified evidence, deletion policy, issuer and
authorization decision, lifecycle authority, retention pointer and source head.
It then commits the exact authorization link, acceptance, source-head CAS,
audit event, outbox intent and idempotency result together. A known aborted
transaction may be retried only within the configured bound and repeats all
authority checks. An unknown commit is reconciled with the original
idempotency key and digest; it is never retried under a new identity.

## Safe rollback

Downgrading migration `20270112_111` intentionally retains v3 columns, readers,
selector history/current heads and existing v3 records. The exact authorization
foreign key is detached so older migrations can remove and later recreate their
authorization table without deleting retained v3 rows; forward upgrade restores
the constraint. The downgrade installs
`trg_canonical_acceptance_v3_disabled`, which rejects new v3 inserts. Do not drop
or rewrite retained history during rollback.

After rollback, confirm that:

- v1/v2/v3 reads still resolve exact stored evidence;
- new v3 writes fail closed;
- existing v3 authorization links and audit/outbox lineage remain queryable;
- no operator substitutes a v1 write for a denied v3 transition.

Re-enabling the writer requires a forward deployment of the compatible
application and removal of the rollback write-disable trigger as an explicit
operator step. If commit outcome is uncertain, reconcile the original receipt
before any further action.
