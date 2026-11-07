# Issue #586 — Architecture Owner Acceptance Record v2

**Decision:** Architecture-1 through Architecture-3 `ACCEPT` as architecture principles.
**Decision date:** 2026-11-07.
**Role attribution:** the human owner explicitly confirmed acting separately as Architecture Owner. Conversational evidence is role-scoped and not an independent signature. Exact prompt wording was not supplied and is not reconstructed.
**Exact ADR acceptance:** PENDING for ADR-0062 SHA-256 `498cd09b67f88c627f4b6d2307267e2ca5e4b686fc5623899e2aba6730c653f8`. The decision approved principles, not the later unseen ADR-0062 proposal.


The supplied conversational confirmations describe the Architecture decisions below but do not bind them to a separate package or document digest. This record transcribes only that supplied scope; it does not imply approval of later profile, ADR, implementation or configuration text.

## Decision record

| ID | Result | UTC confirmation | Accepted scope |
|---|---|---:|---|
| Architecture-1 | ACCEPT | 14:28:09 | One logical canonical PostgreSQL `PricingAuthorityRegistry`, using existing Spine canonical persistence; source provenance separate from Finance approval; immutable pricing/Estimate versions; resolver uses approved precedence; Finance + Data + current Security authorization required for activation; no independent pricing database/engine; Context Broker is not authority; external provider fetch is outside PostgreSQL transaction. |
| Architecture-2 | ACCEPT | 14:28:58 | At commit recheck current authorization/authority, bind exact Estimate version with CAS, prevent approval against revoked basis; atomic canonical decision + Audit Event + transactional outbox intent; idempotent commands; reconcile unknown commit from persisted idempotency identity; qualify with deterministic PostgreSQL concurrency tests. |
| Architecture-3 | ACCEPT | 14:30:12 | Conceptual lifecycle `DRAFT → VALIDATED → APPROVED → ACTIVE → SUPERSEDED / REVOKED`; approval does not activate; activation separately authorized; transitions versioned/audited/CAS-protected; revoked sources cannot auto-reactivate; sync cannot activate; historical Estimates immutable; recovery respects current authorization/retention/revocation; one R2 workflow architecture. |

## Accepted compatibility boundaries

- Reuse existing canonical `SourceObject` and immutable `SourceRevision` contracts for source provenance. Source currentness, Data validation and Finance approval are distinct facts.
- `AccessPolicy` remains current source-content authorization. Pricing authority is a separate business decision. A Context Profile, Context Broker retrieval result, provider connection, administrator role or source association grants no pricing authority.
- Use one canonical PostgreSQL persistence boundary for Spine-owned pricing authority and Estimate state. The registry is a logical governance contract, not an independent database or source-of-truth copy of external price books.
- Use the existing R2 `WorkItem`, single `WorkflowRun`, typed commands/events and artifact lifecycle. Do not add a second workflow/run engine.
- Estimates are immutable versioned artifacts. Approval binds the exact current artifact version and calculation digest. Material change creates a successor with fresh review/approval.
- A commit decision must serialize current source revision/authority, current AccessPolicy, registry activation head and Estimate head/version. Its canonical decision, required Audit Event and transactional outbox intent commit in one PostgreSQL unit of work. Duplicate commands are idempotent; unknown commit is reconciled by the persisted command identity.
- Fetches from Bank of Russia or other external providers occur outside the database transaction. Persist exact source observations/revisions first; the commit transaction validates their canonical currentness and approvals. PostgreSQL cannot make an upstream provider call transactional.
- Source-price records are never changed by Estimate review or override. Revocation prevents future use; historical Estimates remain immutable and affected undelivered Estimates require a successor/reapproval path.

## Exact ADR status

The proposed [ADR-0062](../adr/0062-canonical-pricing-authority-and-estimate-commit.md) formalizes these principles and supplies a candidate lifecycle and transaction ordering. Its exact transition graph, locking details, recovery semantics and typed failure transitions were not included in the prior human approval; they remain proposals until the Architecture Owner accepts the exact ADR text and digest or supplies changes.

## Architecture acceptance record

- **Role-scoped principles:** ACCEPT, the three confirmations/time points above.
- **Exact ADR-0062 text/version/digest:** PENDING_ARCHITECTURE_ACCEPTANCE; proposal digest `498cd09b67f88c627f4b6d2307267e2ca5e4b686fc5623899e2aba6730c653f8`.
- **Concrete source and Estimate contract/version:** pending implementation specification after ADR acceptance.
- **Concurrency, revocation, idempotency and lost-response tests:** specified as required qualification; not run and not claimed passed.
- **Production authorization / implementation:** NOT GRANTED.

## Exact Architecture Owner decision request

For the exact ADR-0062 text and SHA-256 recorded in the consolidated decision overlay, submit one role-attributed outcome: `ACCEPT`, `ACCEPT_WITH_CHANGES`, or `REJECT`. An ACCEPT applies only to that exact ADR digest. List each requested change with the changed section and rationale. The earlier Architecture-1 through Architecture-3 confirmations remain accepted and are not retroactively expanded to the ADR text.
