---
status: accepted
---

# Qualification authority binding hard expiry

This decision extends ADR-0021 without changing its text.  A temporary
qualification authority is represented by a versioned, qualification-scoped
binding between one Workspace/Environment and exactly one canonical human or
service principal.  The binding has an immutable UTC deadline.  It is not a
general identity-management record and cannot create an identity, membership,
role, content grant, credential, database role, or target.

The decision records the hard-expiry extension approved for issue #531 and
implemented by issue #541.  The approval was conveyed by a project participant
and is not represented as a formal GitHub review by the Security Owner.  It
authorizes this bounded contract and its implementation, not provisioning or
operation of a concrete SOURCE or RESTORE target.

## Decision

PostgreSQL database time is authoritative.  Every applicable resolution checks
`transaction_timestamp() < expires_at` in the same transaction that resolves
current authority.  Equality is expired.  Cleanup, a reaper, and external
credential expiry are supplementary controls and their absence never extends
canonical authority.

Each binding version preserves the original qualification reference,
principal, Workspace, Environment, service purpose/operation scope, and
deadline.  A successor may change lifecycle status only.  Appending an
effective version advances the Workspace/Environment authorization generation
in the same transaction.  Consequently activation, revocation, and other
effective binding changes cannot commit without the generation change.  A
revoked binding is denied even if credentials or cleanup state remain.

Human bindings are evaluated in both request authorization and later protected
consumption.  Service bindings are evaluated from the server-selected service
principal, purpose, operation, Workspace, and Environment; request input cannot
select or broaden them.  A principal with qualification-binding history is
denied unless its single current binding version is active, scope-matching, and
strictly before its deadline.  Principals with no qualification binding retain
the existing ADR-0021 behavior.

## Resolution and consumption inventory

The repository has the following applicable canonical paths:

| Path | Principal | Current-authority boundary |
| --- | --- | --- |
| Protected HTTP request composition | Human | `resolve_current_authorization_snapshot` in the request transaction |
| Initial upload policy authority refresh | Human | the same human snapshot resolver |
| Tenant PostgreSQL Unit of Work entry | Human and/or service | qualification authority is rechecked after trusted context binding and before protected repository work |
| Durable Ask activity admission | Service plus admitted human generation | the service resolver runs for every activity/action boundary and the current generation must equal the admitted generation |
| Q&A content/evidence/citation decisions | Human plus service | identity-authority revalidation resolves both principals before each protected decision |

The API, worker, ingestion, Q&A, document lifecycle, audit, and outbox
compositions consume protected tenant repositories through the Unit of Work
boundary.  Direct worker reads that establish durable activity authority use
the service resolver explicitly.  Migration-owner helpers, schema migration,
database backup/restore roles, readiness probes, and authority-free
cancellation/cleanup do not consume a qualification principal's protected
authority and are outside this binding.  They remain governed by their existing
operator and database-role contracts.

Durable orchestration may retain non-authority state after expiry, but every
later protected activity or external action obtains fresh authority.  An
expired or revoked binding, or a stale generation, denies that operation.  Only
safe cancellation or cleanup that does not read protected content or perform a
protected external action may continue without the expired authority.

## Consequences

- Qualification expiry remains effective when cleanup and secret revocation
  are unavailable.
- The runtime role receives execute-only access to bounded resolver functions,
  not table CRUD or enumeration access.
- AccessPolicy `valid_until` remains a separate content-grant constraint; this
  binding does not manufacture `read_content` or `process_content` grants.
- Deadline changes require a different approved qualification binding.  A later
  version cannot renew or extend an existing deadline.
- Rollback restores the preceding schema and resolver definitions; it cannot be
  used to claim that an already expired operational qualification is valid.
