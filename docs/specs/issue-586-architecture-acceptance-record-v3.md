# Issue #586 — Architecture Owner Acceptance Record v3

**Decision:** Architecture-1 through Architecture-3 `ACCEPT` as architecture principles.
**Decision date:** 2026-11-07.
**Role attribution:** the human owner explicitly confirmed acting separately as Architecture Owner. Conversational evidence is role-scoped and not an independent signature. Exact prompt wording was not supplied and is not reconstructed.
**Revision 1 decision:** `ACCEPT_WITH_CHANGES` for ADR-0062 at 2026-11-07 15:58:04 UTC, bound to SHA-256 `498cd09b67f88c627f4b6d2307267e2ca5e4b686fc5623899e2aba6730c653f8`.
**Revision 2 exact-text decision:** PENDING_ARCHITECTURE_ACCEPTANCE; [ADR-0062 revision 2](../adr/0062-canonical-pricing-authority-and-estimate-commit-v2.md), SHA-256 `160b6f9de631e85849a0d962139fc1a8af59caf8782c793a16185342c6b50e3a`.


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

The original proposed [ADR-0062 revision 1](../adr/0062-canonical-pricing-authority-and-estimate-commit.md) formalized the principles but lacked terminal candidate states, overlap handling and sufficiently grounded transaction requirements. On 2026-11-07 15:58:04 UTC, the Architecture Owner accepted required changes to that exact revision (`ACCEPT_WITH_CHANGES`). This is not acceptance of revision 2's exact text.

## ADR-0062 revision 1 change decision

| Field | Recorded decision |
|---|---|
| Outcome | `ACCEPT_WITH_CHANGES` |
| Role | Architecture Owner, separately role-scoped |
| Decision time | 2026-11-07 15:58:04 UTC |
| Exact reviewed object | ADR-0062 revision 1, SHA-256 `498cd09b67f88c627f4b6d2307267e2ca5e4b686fc5623899e2aba6730c653f8` |
| Evidence | Explicit conversational confirmation reported by the user for this task. It is not a GitHub-authored approval or independent electronic signature; no exact question wording beyond the supplied decision scope is reconstructed. |

Accepted required changes:

1. Add explicit terminal `REJECTED` and `CANCELLED` candidate states; terminal versions cannot transition to `ACTIVE`.
2. Prevent overlapping conflicting active authority versions within the same governed scope/priority unless an explicitly approved supersession/conflict policy applies.
3. Keep `APPROVED → ACTIVE` as a separate atomic command that rechecks Finance approval, Data validation and current `AccessPolicy`.
4. Define PostgreSQL serialization/CAS and lock ordering consistently with existing `SourceRevision`, `AccessPolicy`, `WorkflowRun` and Artifact ownership contracts; prevent deadlock cycles and stale approvals.
5. Order revocation versus Estimate approval deterministically: earlier committed revocation blocks approval; later revocation preserves immutable history but prevents an affected undelivered Estimate from proceeding without recalculation/reapproval.
6. Reconcile unknown commit outcomes through durable idempotency identity without duplicate approval or outbox intent.

The six changes are incorporated as proposals in revision 2. Their exact implementation contract, including a concrete lock/fence order, remains subject to revision 2 acceptance and repository/schema qualification.

## Architecture acceptance record

- **Role-scoped principles:** ACCEPT, the three confirmations/time points above.
- **ADR-0062 revision 1:** `ACCEPT_WITH_CHANGES`, 2026-11-07 15:58:04 UTC, original digest above.
- **Exact ADR-0062 revision 2 text/version/digest:** PENDING_ARCHITECTURE_ACCEPTANCE; SHA-256 `160b6f9de631e85849a0d962139fc1a8af59caf8782c793a16185342c6b50e3a`.
- **Concrete source and Estimate contract/version:** pending implementation specification after ADR acceptance.
- **Concurrency, revocation, idempotency and lost-response tests:** specified as required qualification; not run and not claimed passed.
- **Production authorization / implementation:** NOT GRANTED.

## Exact Architecture Owner decision request

For the exact ADR-0062 revision 2 text and SHA-256 recorded in the consolidated decision overlay, submit one role-attributed outcome: `ACCEPT`, `ACCEPT_WITH_CHANGES`, or `REJECT`. An ACCEPT applies only to that exact ADR digest. List each requested change with the changed section and rationale. Earlier Architecture-1 through Architecture-3 and revision 1 change decisions remain within their recorded scopes and do not approve revision 2's exact text.
