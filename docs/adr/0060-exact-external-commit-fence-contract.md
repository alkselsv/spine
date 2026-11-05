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

## Production composition (#980)

Production source selection is default-deny and is defined in
`docs/architecture/commit-fence-production-composition.md`. Only the three F1
owners above may be configured, and every consuming composition must name its
exact purpose/action uses and deployment scope. No F2 evidence currently exists,
so the v1 production composition exposes no external-provider registration
input; #983 must add a versioned, evidence-issued successor before any named F2
adapter can be selected. Synthetic fixtures are accepted only by an exact-binding
test composition and cannot be registered in production. This adds no authority
registry, grant, RLS change or `BYPASSRLS` capability.

## EF-5 retention consumer (#977)

The private-ASR retention consumer resolves approval and terms in the same
transaction that holds the exact-source current-applicability pointer
`FOR UPDATE` lock. Before that transaction commits, it also acquires the F1
retention owner fence against the same pointer and registers a synchronous
expiry guard on the PostgreSQL connection's `commit` event. F1 approval and
terms readers must lock each mutable current-owner pointer they rely on and
keep those locks until commit.
The fence binding's approval version/digest identify the current applicability
record; its state digest binds policy/version/digest, applicability ID/generation,
terms ID/version/digest, artifact class, resolved exceptions and holds, active
holds, and operation. Source lineage is checked against the immutable
PostgreSQL revision and remains protected by the lifecycle lock.

F1 is accepted only when both approval and terms sources are explicitly marked
as PostgreSQL-owned by trusted composition after their transaction-bound
locking behavior is qualified. Test-only synthetic sources require an explicit
test option and are not an application adapter. External or
unidentified sources remain denied (F4) until a provider-specific F2 adapter is
qualified; the deterministic F2 fixture and its revoke, expiry and outage tests
prove local port behavior only. They are not production provider evidence.

Owner/source mapping:

| Decision | Canonical owner | Consumer behavior |
|---|---|---|
| Current applicability ID and generation | PostgreSQL retention pointer | F1 row lock held through commit |
| Policy approval and terms | Explicitly PostgreSQL-owned transaction-bound readers | Included in the composite state digest; missing or unqualified readers deny |
| External approval or terms | Their named external provider | F2 is required; until qualified, deny |
| Exceptions and legal holds | Current retention binding and terms provider | Exact references are bound; unresolved or active holds deny |
