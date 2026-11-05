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
