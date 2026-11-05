# ADR-060: Exact external commit-fence contract

Status: accepted for the provider-neutral contract only. No provider is qualified
and no external authority is enabled by this decision.

Date: 2026-11-05

## Context

An approval or revocation epoch read before a PostgreSQL transaction does not
serialize that transaction with an external authority's revocation. The
provider-neutral contract is a prerequisite for the later F1/F2 adapters and
consumers; it does not implement a fence protocol or claim a revocation ordering
for any provider.

## Decision

`CommitFenceBinding:v1` is an immutable exact binding over issuer, approval
version and digest, workspace, environment, purpose, action, subject, artifact
state digest, complete source lineage digest, authority generation, revocation
epoch, operation, idempotency key and a UTC half-open validity interval. Its
canonical digest uses the versioned `spine.external-commit-fence-binding:v1`
domain separator. Composite source lineage is unique by exact source object,
revision and revision digest within the bound workspace/environment, sorted by
that identity before its canonical digest is calculated.

`CommitFenceOutcome:v1` records `acquired`, `denied`, `stale`, `expired`,
`ambiguous` or `released`. Only `acquired` may carry an opaque permit reference,
and that permit's validity is bounded by the requested binding. Unsupported or
unknown authority is denied. Expiry equality denies. Ambiguous acquisition is
not success. An acquired outcome must be reported by a supported authority
adapter; matching caller-controlled fields does not establish approval.

The permit reference is an opaque identifier, not a bearer secret. The contract
stores no permit contents, tokens or provider payloads. It is not a second
authority registry. Authority state remains with its existing canonical owner.

Existing v1 records remain unchanged: `PrivateArtifactDeletionAuthorization:v1`,
`PrivateArtifactDeletionRequest:v1`, `RetentionApplicability:v1`,
`ApprovedRetentionTerms` and `PrivateReportSourceBinding` keep their existing
schemas and canonical owners. There is no automatic v1-to-fence conversion;
callers must supply a complete v1 binding explicitly. This contract does not
authorize any existing operation, persistence, migration or permission change.

## Consequences

- Later adapters must compare every binding field and enforce a bounded permit
  through their transaction's commit point. An adapter that cannot prove support
  returns denial.
- Retries require a fresh authority decision and permit under the same exact
  operation/idempotency identity; unknown commit reconciliation belongs to the
  consuming transaction contract.
- The contract alone does not qualify an external authority, production
  composition, security policy or real-data use.

## F1 PostgreSQL adapter (#972)

The F1 adapter uses the caller's active PostgreSQL Unit of Work and adds no
tables, lock keys, permissions or authority registry. Its supported owner
states are deliberately limited:

| Owner state | Serialization held through commit | Exact bound state |
|---|---|---|
| Source lifecycle | Existing shared source-lifecycle transaction lock | Current eligible decision ID, generation and canonical decision-record digest |
| AccessPolicy | `FOR UPDATE` on the existing current-policy row, matching activation's lock | Current policy version ID, document digest, policy generation and CAS version |
| Retention applicability | `FOR UPDATE` on the existing current applicability pointer | Current applicability ID, canonical record digest and generation; exact-source targets only |

The binding issuer is a stable code identity for the canonical PostgreSQL owner.
It does not identify or qualify an upstream human, legal, retention or recording
approver. In particular, retention approval terms and other external or
unresolved authority portions remain denied unless separately qualified.
Source lineage is checked against immutable revisions and held under existing
shared lifecycle locks. The trusted manifest pointer remains owned by #811 and
is not implemented again here.

Every acquired F1 binding registers a synchronous validity check with the same
Unit of Work. The check runs after the transaction probe and immediately before
the PostgreSQL `COMMIT`; expiry equality fails the transaction and rolls it
back. The owner lock remains held until commit/rollback, so a canonical
replacement that obtains its owner lock first is observed as stale, while a
consumer that holds the lock commits before the replacement can proceed.
Synthetic PostgreSQL races qualify only these local lock orderings; they do not
qualify an external authority or production use.
