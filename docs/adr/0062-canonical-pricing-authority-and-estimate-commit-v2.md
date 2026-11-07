---
status: proposed
---

# ADR-0062 revision 2: Canonical pricing authority and Estimate commit boundary

**Status:** PROPOSED / PENDING_ARCHITECTURE_ACCEPTANCE. The Architecture Owner accepted required changes to revision 1 on 2026-11-07 15:58:04 UTC (`ACCEPT_WITH_CHANGES`), but has not accepted this exact revision or digest. This ADR grants no implementation or production authority.

**Revision lineage:** revision 1 remains preserved at [the original ADR](0062-canonical-pricing-authority-and-estimate-commit.md), SHA-256 `498cd09b67f88c627f4b6d2307267e2ca5e4b686fc5623899e2aba6730c653f8`. This revision implements the six requested changes; it is a new decision payload.

## Context and decision boundary

R2 proposal generation needs exact, reviewable price bases and deterministic Estimates while keeping source evidence, Finance authority, Data validation, Security access and human approval distinct. This revision applies the accepted principles: one logical canonical `PricingAuthorityRegistry` in Spine's existing PostgreSQL persistence; exact `SourceRevision` references; one R2 `WorkItem`/`WorkflowRun`; immutable versioned artifacts; current `AccessPolicy`; and transactional audit, outbox and idempotency. It does not define migrations or claim that current runtime code implements these contracts.

No accepted contract currently provides pricing-specific canonical records or proves a shared row-lock protocol across source lifecycle, `AccessPolicy`, workflow-run and artifact owners. ADR-0018 and ADR-0060 require compatible serialization at owner boundaries, and ADR-0018 explicitly defines the invariant rather than a universal SQL lock order. Accordingly, this revision specifies transaction invariants and records concrete lock ordering as a pre-implementation acceptance gate; it does not invent tables, locks, generations or owner capabilities.

## Canonical ownership and source binding

1. Spine's existing canonical persistence owns one logical `PricingAuthorityRegistry`. This is a domain contract, not a second database or replacement for external price sources.
2. An immutable candidate binds workspace/environment, `SourceObject`, exact `SourceRevision` ID and digest, stable `PriceItem` identity, in-revision record locator and digest, UOM, currency, tax metadata, commercial scope, effective interval, Data mapping/validation version, and attributable Finance authority decision.
3. Source provenance, Finance approval, Data validation and current source-content authorization are separate facts. Activation requires all of them and current `AccessPolicy` authorization. Import, synchronization, Context Broker retrieval and administrative access do not activate pricing authority.
4. New source content or a material mapping change creates a new immutable candidate and requires Data revalidation and Finance approval. Supersession and revocation are explicit; revoked versions cannot be selected for new calculations or automatically restored.

## Candidate lifecycle

The complete state set is `DRAFT`, `VALIDATED`, `APPROVED`, `ACTIVE`, `SUPERSEDED`, `REVOKED`, `REJECTED`, and `CANCELLED`. The transition table below is exhaustive for this proposal. `SUPERSEDED`, `REVOKED`, `REJECTED`, and `CANCELLED` are terminal. A new attempt uses a new immutable candidate identity; no terminal candidate can transition to `ACTIVE`.

Every successful row below is one canonical PostgreSQL Unit of Work (UoW). It verifies tenant/environment, actor, current authority and expected candidate generation; appends the immutable transition and required Audit Event, transactional outbox intent, and idempotency result atomically; then commits. A failed precondition, CAS, audit, outbox, or persistence operation rolls back all protected writes. Provider/network I/O is outside the UoW. `expected generation` means the owner-issued version/CAS token, not a newly asserted database column; its concrete owner contract is an implementation gate below.

| Transition | Required authority | CAS and prerequisites | Atomic result and audit/outbox | Retry, recovery and invalid transition |
|---|---|---|---|---|
| `DRAFT → VALIDATED` | Data validator capability, with current Security authorization; validator is attributable independently from requester where the accepted segregation policy applies. | Expected candidate generation; exact source revision/digest, item mapping, schema, UOM/currency/tax metadata and scope pass the pinned Data Validation contract; source currentness is checked. | Append validation result and state transition; audit validation identity/digest and emit content-safe validation event in the same UoW. | Same idempotency key/digest returns the original receipt. After known abort, revalidate current source and policy. Failed checks leave candidate `DRAFT` and return typed validation failure. Other transitions from `DRAFT` are invalid except `REJECTED` and `CANCELLED` below. |
| `DRAFT → REJECTED` | Authorized Data decision authority for an unvalidatable candidate, or authorized Finance authority for an ineligible financial candidate; exact capability mapping must be configured under approved role policy. | Expected candidate generation; rejection reason is required; no prior terminal transition. | Append immutable rejection decision, audit actor/reason code and emit content-safe rejected event atomically. | Exact replay returns receipt; changed request under same key conflicts. Any stale generation returns conflict. A rejected candidate cannot be edited/reactivated. |
| `DRAFT → CANCELLED` | Original requester or separately authorized candidate controller; current Security authorization. | Expected candidate generation; reason required; no active authority can be affected. | Append cancellation and audit/outbox atomically. | Exact replay returns receipt. Stale generation conflicts. Cancelled candidate cannot be reopened; any other transition is invalid. |
| `VALIDATED → APPROVED` | Finance Owner or separately delegated Finance approver; no self-approval by the source requester where applicable. | Expected candidate generation; current exact SourceRevision, Data validation, Finance policy/source authority, scope and effective interval verified; no missing/ambiguous financial metadata. | Append immutable Finance decision and transition; audit exact payload/version/digest, actor and reason; emit content-safe approval event atomically. | Exact replay returns receipt. Known abort retries only after complete revalidation. Revoked/superseded source or stale Data validation fails closed. |
| `VALIDATED → REJECTED` | Finance authority rejecting financial eligibility, or Data authority withdrawing a failed/stale validation; exact actor capability required. | Expected generation; reason and decision reference required. | Append terminal rejection and safe audit/outbox in one UoW. | Exact replay returns receipt; stale generation conflicts. No reactivation. |
| `VALIDATED → CANCELLED` | Original requester or authorized candidate controller; current Security authorization. | Expected generation; reason required; cancellation does not mutate SourceRevision or another candidate. | Append terminal cancellation and safe audit/outbox atomically. | Exact replay returns receipt; stale generation conflicts. No reactivation. |
| `APPROVED → ACTIVE` | Separately authorized activation principal; current Security authorization. Finance approval is not itself activation permission. | Expected candidate generation and expected scope/priority current-head generation. Recheck exact Finance approval, current Data validation, exact SourceRevision/currentness, current `AccessPolicy`, workspace/environment and effective interval. Check all active versions for the same governed scope/priority. A conflicting overlapping window fails unless a separately approved conflict/supersession policy authorizes the exact transition. | In one UoW, publish active head, transition any explicitly superseded predecessor, append audit and one transactional outbox intent, and persist idempotency result. No external fetch inside transaction. | Exact same command/digest replays stored result. A stale authorization/head or conflict is not auto-retried as current. Unknown commit is reconciled by durable idempotency identity. Failed audit/outbox rolls back activation. |
| `APPROVED → REJECTED` | Finance authority withdrawing approval before activation, with reason and current authorization. | Expected generation; exact prior approval reference; candidate is not active. | Append withdrawal/rejection, audit and safe outbox atomically. | Exact replay returns receipt; stale/currentness mismatch conflicts. No reactivation. |
| `APPROVED → CANCELLED` | Original requester or authorized controller, subject to Finance approval withdrawal being recorded; current Security authorization. | Expected generation; reason; candidate is not active. | Append cancellation and approval-withdrawal reference with audit/outbox atomically. | Exact replay returns receipt. If activation serialized first, cancellation is invalid and requires separate revocation. |
| `ACTIVE → SUPERSEDED` | Separately authorized activation command for a successor; Finance, Data and AccessPolicy checks for the successor all pass. | Expected active-head generation; successor is approved; intervals and scope are validated. Conflicting active versions in the same governed scope/priority cannot overlap unless an explicit approved supersession/conflict rule applies. | Activation and predecessor supersession are one UoW with exact CAS, audit, one outbox intent and idempotency result. | Same identity/digest returns receipt. If another activation/revocation commits first, CAS fails; caller reloads and re-resolves, never applies stale command. |
| `ACTIVE → REVOKED` | Separately authorized revocation authority; actor and reason attributable; current Security authorization. | Expected active-head and candidate generations; exact target version/scope; revocation effective point recorded. | Atomically append revocation, move current head as policy permits without restoring an older version, audit and emit a content-safe disposition event, and persist idempotency result. Affected undelivered Estimates are marked for review, not rewritten. | Exact replay returns receipt. A stale head conflicts and must be re-evaluated. Unknown commit reconciles through the same durable identity. Revoked versions cannot reactivate. |

All transitions not listed—including any transition out of `SUPERSEDED`, `REVOKED`, `REJECTED`, or `CANCELLED`; `ACTIVE → ACTIVE`; and terminal-to-`ACTIVE`—return a typed invalid-state/conflict outcome and write no transition, approval or outbox intent. Safe denial audit may follow the existing audit policy but cannot disclose restricted source data.

The authority assignment for candidate cancellation/rejection and activation/revocation must be bound to concrete approved capabilities before implementation. This ADR proposes role classes and fails closed while the corresponding capability is absent; it does not create grants.

## Activation overlap and currentness

Effective periods are half-open `[from,to)`. Before activation, the canonical commit must serialize the candidate against the current registry head and detect any active authority with the same exact workspace/environment, governed scope and precedence whose interval intersects the candidate interval. It must reject overlap when the sources conflict. It may apply a different result only if an explicit, versioned Finance-approved conflict/equivalence/supersession policy covers both candidates. Missing scope fields are not wildcards. Activation cannot rely only on a pre-transaction read.

Approval and activation are distinct transitions. `APPROVED` never implies `ACTIVE`; each activation command rechecks Finance approval, Data validation and current `AccessPolicy`. Source fetches are done before the UoW and pinned to immutable source revisions.

## PricingBasisResolver and Estimate artifacts

`PricingBasisResolver` resolves only from a trusted workspace/environment, `pricing_as_of`, service/project/client/contract scope, approved rule-set version and current eligible authority bindings. It applies the accepted precedence: exact contract/client/service/period; exact service+project class; exact service-only; exact project-only; workspace/environment base. Missing scope fields do not wildcard; equal-rank conflicts fail `PRICE_CONFLICT` unless an approved deterministic rule applies. It returns one exact `PricingBasis` or typed failure. It does not use an LLM, historical Estimate, Context Broker authority or unapproved external lookup.

Each Estimate and successor is an immutable versioned artifact in the existing artifact persistence boundary and is linked to the existing R2 `WorkItem` and single `WorkflowRun`. Human approval binds the exact current artifact version and calculation digest. Material change creates a successor and requires fresh approval. Price overrides never mutate a source book. Context Broker retrieval provides evidence only; it conveys no pricing authority.

## Estimate approval, revocation race and idempotency

Estimate approval is a canonical command against the expected current artifact version/digest and current workflow state. At commit it rechecks authorization, PricingBasis source currentness and authority, current `AccessPolicy`, approved CalculationProfile and current Estimate version. Requester and approver identities are compared by canonical actor identity, not role labels. An approval cannot bind a stale successor or revoked basis.

Revocation and Estimate approval must share an owner-level serialization fence for the exact PricingBasis/authority. If revocation commits first, the Estimate approval fails closed. If Estimate approval commits first, its artifact and approval history remain immutable; a later revocation prevents an affected undelivered Estimate from proceeding until a successor is recalculated and reapproved. Delivery disposition is recorded in the same R2 workflow/artifact lifecycle, not a second workflow engine.

The approval command's canonical Estimate-version decision, required audit, outbox intent and durable idempotency result commit atomically. The idempotency identity binds workspace/environment, canonical actor/service principal, operation and request digest. Same identity/digest returns the original receipt; a different digest conflicts. On unknown commit outcome, the caller reads the original persisted receipt. It must not issue a new approval or outbox intent. Known serialization/deadlock aborts may retry only after rollback and complete authority revalidation, with the same identity and digest.

Required audit/outbox failure rolls back the protected transition. Provider/network calls, workflow engine calls and object-store I/O are not inside PostgreSQL's transaction. Durable outbox processing is at-least-once and idempotent under existing contracts.

## PostgreSQL serialization, CAS and lock ordering

This ADR requires atomicity and compatible owner serialization, not a guessed global row-lock order. Existing ADR-0018 and ADR-0060 support local current-pointer/source-lifecycle/AccessPolicy fences and explicitly require implementations to inventory mutation paths and prove compatible serialization. Available repository documents do not establish pricing tables, exact row keys, owner APIs, cross-owner lock acquisition, or one lock order spanning `SourceRevision`, `AccessPolicy`, `WorkflowRun` and artifacts. Therefore:

1. Every participating owner must expose its existing canonical read/version/CAS or serialization seam; pricing code cannot lock another owner's private rows directly.
2. The eventual implementation must inventory all competing mutation paths and define a single acyclic lock/fence acquisition order by owner, including source revision/revocation, AccessPolicy replacement, pricing-head activation/revocation, WorkflowRun transition and artifact-head/approval commit.
3. The transaction must compare each expected owner generation at commit, or hold that owner's supported fence through commit. A current read without a compatible fence is insufficient.
4. Before implementation, a repository/schema review must verify that every owner participates in one PostgreSQL transaction and that audit, outbox and idempotency receipt share that UoW. If not, ADR revision or a separately accepted contract is required; no distributed atomicity is implied.
5. PostgreSQL serialization/deadlock failures roll back. A bounded retry may reuse the same idempotency identity/digest only after re-resolving all current authority. Unknown outcomes use durable receipt reconciliation.

**Unresolved technical acceptance requirement:** before runtime work, document the concrete existing tables/owner APIs, their generation/CAS fields, actual lock/fence order, deadlock-cycle proof and test harness. This ADR intentionally does not assert a lock order unsupported by the current contracts. Deterministic PostgreSQL tests must cover activation overlap races, AccessPolicy/source revocation, Estimate approval/revocation ordering, stale CAS, rollback after audit/outbox failure, concurrent duplicate idempotency, lost response and workspace/environment isolation.

## Tenant isolation and failure handling

Every registry, source binding, PriceItem, command, Estimate and audit reference carries trusted `workspace_id`; environment-specific authority and execution carries `environment_id`. Authorization is rechecked at commit and scoped background work uses an explicit service principal. Cross-tenant and denied lookups use non-disclosing outcomes. Missing authority, stale versions, conflicts, invalid state, stale CAS, mismatched idempotency digest or unavailable owner fence fail closed. No partial activation, Estimate approval, audit intent or outbox intent survives a failed UoW.

## Compatibility and consequences

- Reuses `SourceObject`/`SourceRevision`, current `AccessPolicy`, existing R2 `WorkItem`/`WorkflowRun`, artifact versioning, audit, outbox and idempotency ownership; no second workflow or independent pricing database.
- External price sources remain authoritative for their source content; Spine separately governs which immutable source observations have financial authority.
- The new state machine is a proposed contract only. No migration, pricing code, runtime permission or production source activation is authorized by this ADR.
- Exact capability bindings for activation, revocation, cancellation and rejection; concrete owner transaction seams/lock order; and PostgreSQL concurrency qualification are prerequisites to implementation.

## Deterministic acceptance specification

The following are required future tests, not tests claimed to have run:

1. Each listed transition commits once for the exact expected generation; every unlisted transition is rejected with no protected partial effect.
2. Two conflicting same-scope/priority candidates with overlapping half-open intervals race to activate: at most one conflicting active authority commits; an endpoint-touching interval does not overlap.
3. Finance approval, Data validation or AccessPolicy changed/revoked before activation commit causes rejection; approval alone never activates.
4. Concurrent Estimate approval against one artifact version: one CAS wins; the loser cannot write an approval, audit success event or outbox intent.
5. Revocation-first ordering blocks Estimate approval; Estimate-approval-first preserves immutable history and blocks later delivery pending successor/reapproval.
6. Same idempotency identity/digest replay returns the same receipt and one outbox intent; changed digest conflicts; lost response reconciles the committed result without a duplicate.
7. Injected audit/outbox failure rolls back state, CAS/head change, idempotency result and approval together.
8. Forced deadlock/serialization abort performs rollback, then same-identity retry rechecks all owner state; a newly revoked basis is not reused.
9. Cross-workspace/environment source, policy, registry or Estimate references fail without disclosing protected source details.
10. A Data/Finance/Security actor mismatch or same canonical actor attempting prohibited self-approval is denied regardless of role label.

Offline synthetic contract tests may establish pure transition behavior. PostgreSQL races, current-rights checks, transaction rollback and recovery require integration tests against the actual canonical schema and are not proven by a documentation review.

## Architecture Owner decision request

The Architecture Owner accepted `ACCEPT_WITH_CHANGES` for revision 1 at 2026-11-07 15:58:04 UTC, bound to its SHA-256 above. That decision approved the required amendments, not this successor text. Review this exact revision and record `ACCEPT`, `ACCEPT_WITH_CHANGES`, or `REJECT` with role attribution, UTC time, and this revision's SHA-256. An ACCEPT applies only to the exact digest. Any material edit requires a new revision/digest and new exact-text acceptance before implementation approval.
