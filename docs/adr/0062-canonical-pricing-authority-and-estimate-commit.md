---
status: proposed
---

# ADR-0062: Canonical pricing authority and Estimate commit boundary

**Status:** PROPOSED; the Architecture Owner accepted the governing principles on 2026-11-07, but has not accepted this exact ADR text or digest. This ADR grants no implementation or production authority.

## Context

R2 proposal generation needs an exact, reviewable price basis and deterministic Estimate while keeping external source evidence, Finance authority, source access, Data validation and human approval separate. Existing Spine contracts already provide PostgreSQL canonical state, immutable `SourceObject`/`SourceRevision`, current `AccessPolicy`, versioned artifacts, one R2 `WorkItem`/`WorkflowRun`, transactional Audit Events and outbox/idempotency boundaries. No accepted ADR currently owns canonical pricing authority activation and its race with Estimate approval/revocation.

Alternatives considered:

1. Treat an external price book or its latest revision as the current authority. Rejected: synchronization is not financial approval and external state cannot share a PostgreSQL transaction.
2. Create a separate pricing database/workflow. Rejected: this duplicates canonical state and breaks existing R2 artifact/workflow ownership.
3. Store versioned pricing authority decisions in the existing canonical PostgreSQL boundary, reference exact source revisions, and serialize activation/Estimate approval at commit. Proposed: it reuses Spine ownership and makes immutable lineage and CAS possible.

## Proposed decision

### Canonical ownership and source binding

1. PostgreSQL owns one logical `PricingAuthorityRegistry` for Spine-governed source bindings, Finance approvals, Data validation references, activation state and current version heads. This is a logical domain contract within existing canonical persistence, not a second database and not a copy that silently replaces an external price book.
2. Each immutable source binding identifies exact workspace/environment, `SourceObject`, accepted `SourceRevision` ID/digest, stable PriceItem identity, in-revision record/field locator and digest, UOM, currency, tax metadata, scope, effective interval, Data mapping/validation version and Finance authority decision. The raw external source remains authoritative for its own content; a SourceRevision or AccessPolicy does not create pricing authority.
3. Finance authority and current source-content access are distinct. Activation requires attributable Finance approval, Data validation and current Security authorization. Current `AccessPolicy` is checked independently. Import, sync, association, Context Broker retrieval or administrator access cannot activate a price.
4. New source content or material mapping change creates an immutable candidate successor and requires Data revalidation and Finance approval. Supersession/revocation is explicit. Revoked versions cannot be used for new calculations or automatically restored.

### Proposed authority lifecycle

Candidate state graph:

```text
DRAFT → VALIDATED → APPROVED → ACTIVE → SUPERSEDED
                                  └────→ REVOKED
```

- `DRAFT → VALIDATED` records a versioned Data Validation result for the exact source revision, item mapping and scope.
- `VALIDATED → APPROVED` records Finance approval bound to exact source revision/digest, PriceItem/version, currency/UOM, tax metadata, scope and effective interval.
- `APPROVED → ACTIVE` is a separate atomic activation command. It rechecks Finance approval, Data validation, current AccessPolicy and the expected registry head, then CAS-publishes the immutable successor.
- `ACTIVE → SUPERSEDED` occurs only as part of activating an explicitly approved successor for the same governed scope. The prior version remains historical and cannot be used for a new calculation after its effective/currentness fence says it is superseded.
- `ACTIVE → REVOKED` is an explicit authorized revocation decision with reason/effective event. Revocation prevents new selection and emits a content-free review disposition for affected undelivered Estimates. It never rewrites an Estimate or automatically restores a previous version.
- No transition returns a version to `ACTIVE`; reactivation requires a new immutable version and new validation/approval.

The exact transition graph, rejected/abandoned candidate representation, overlap resolution and authorized revocation actor are proposals requiring Architecture and role policy acceptance; this ADR does not silently treat them as previously approved facts.

### Deterministic resolver and PricingBasis

`PricingBasisResolver` is a pure domain/application boundary over a trusted workspace/environment, `pricing_as_of`, service/project/client/contract scope, approved rule-set version and current eligible source bindings. It applies the accepted exact precedence: exact contract/client/service/period; exact service+project class; exact service-only; exact project-only; workspace/environment base. Missing scope fields do not wildcard. Same-rank conflict is `PRICE_CONFLICT` absent a separately approved equivalence rule. The resolver returns one exact `PricingBasis` or a typed failure; it does not call an LLM, infer price, select a Context Profile or fetch an external provider.

A `PricingBasis` pins source and record revision/digests, PriceItem/version, stable locator, unit, amount/currency, effective period, Finance approval, Data validation, rule-set/profile references and current authorization evidence. A `PricingBasis` is a decision snapshot; it does not mutate the external source.

### Estimate artifacts and existing workflow

An Estimate and each successor are immutable versioned artifacts stored through the existing artifact persistence contract and linked to one R2 `WorkItem` and one canonical `WorkflowRun`. A line retains its exact PricingBasis, requirement/work reference, quantity/UOM, source currency, discount/tax/FX inputs, CalculationProfile version/digest, exact result and provenance. The final approval binds the exact current Estimate artifact version and calculation digest. Any material change creates a successor and invalidates prior approval for the successor. Human review/override never updates a price source.

The Context Broker may retrieve authorized evidence through a versioned Context Profile; neither retrieval nor profile membership grants pricing authority. The pricing calculator is a deterministic code step; all monetary arithmetic is Decimal-only, and LLMs cannot calculate or supply authoritative amounts.

### Atomic decision and concurrency boundary

All mutable authority used by an activation or approval is canonical PostgreSQL state. Provider requests are made before the transaction. The exact returned source observation/revision is persisted and approved before it can enter a calculation snapshot; no network/provider call is made inside the transaction.

Proposed lock/check order within one PostgreSQL Unit of Work:

1. Resolve trusted actor, workspace and environment; check the exact command's idempotency identity.
2. Lock/read the current source lifecycle/current-revision owner and exact immutable revision.
3. Lock/read current `AccessPolicy` version/generation for each source and check the exact operation permission.
4. Lock/read the `PricingAuthorityRegistry` scope head and exact approved Finance/Data/authority versions.
5. Lock/read the R2 `WorkflowRun` and Estimate artifact head; compare expected run version, artifact version and digest (CAS).
6. Recompute/validate the command digest and current authorization/currentness; reject stale, revoked, cross-workspace or mismatched bindings.
7. Append the immutable Estimate successor/approval or authority activation, required Audit Event and transactional outbox intent in the same transaction. Commit only if every CAS and authority fence remains valid.

This order is a proposal to prevent a revocation or policy replacement from committing between the final check and the protected transition. If revocation commits first, calculation/approval observes it and fails closed. If an Estimate commits first, a later revocation does not rewrite that historical Estimate; an affected undelivered Estimate receives a review disposition and cannot be sent/approved as current without successor calculation and reapproval. Exact row locks/keys must reuse existing owner seams and be specified against the eventual schema; this ADR does not invent migrations or a new cross-tenant lock service.

The transaction writes one canonical decision and its required audit/outbox records atomically. A stable idempotency identity binds tenant, actor/service principal, purpose, operation and exact request digest. Retry with the same identity/digest returns the persisted receipt; reuse with a different digest conflicts. If the commit response is lost, the caller reads/reconciles the persisted idempotency receipt and never creates a second logical Estimate/approval. Protected readback still rechecks current authorization.

### Tenant scope and failure/recovery

Every registry, source binding, PriceItem, command, artifact and audit reference carries trusted `workspace_id`; execution and authorization also carry `environment_id`. Composite ownership constraints and current `AccessPolicy` prevent cross-workspace lookup or disclosure. Missing/cross-tenant identities return the same non-disclosing not-found/denied shape. Background work uses an explicitly scoped service principal and rechecks current rights; it cannot inherit the requester's authority indefinitely.

Stale source/policy heads, revoked bases, conflicting authority, ambiguous identity, denied access, stale run/artifact CAS, mismatched idempotency payload, failed required audit or outbox write cause transaction rollback and a typed safe outcome. No partial authority activation or Estimate approval remains. Recovery retries the same idempotency identity after re-resolving current authority; if the old basis is now revoked, it must not retry as though it were current. Estimates remain immutable and supersession/reapproval is explicit.

## Consequences

- R2 introduces no second workflow engine, independent pricing database, Context Broker authority, LLM monetary calculation or source-price mutation path.
- Source observations, Finance approvals, Data validation and Security permissions remain independently attributable canonical facts.
- A PostgreSQL transaction can serialize Spine-owned source/AccessPolicy/registry/artifact heads only when they share the canonical persistence boundary. It cannot make a Bank of Russia/provider fetch transactional or prevent an upstream provider from changing after the source observation is captured.
- Activation and Estimate approval require exact version/digest, current authorization, CAS and audit/outbox/idempotency integration. A migration/adapter cannot be implemented until record schemas and current owner locks are accepted and specified.
- Deterministic offline domain tests, PostgreSQL concurrency/race tests and workflow replay tests are mandatory before implementation readiness; this ADR and synthetic vectors do not claim those tests have run.

## Open acceptance questions

The Architecture Owner approved the general principles (Architecture-1..3) on 2026-11-07 but did not accept this exact ADR text. Accept or change:

1. Is PostgreSQL-canonical ownership of one logical PricingAuthorityRegistry and exact SourceRevision binding accepted?
2. Is the candidate lifecycle graph and prohibition on reactivation accepted? What exact rejected/abandoned candidate and overlap transitions are required?
3. Is the stated commit lock/check order implementable using current source lifecycle, AccessPolicy and artifact owner seams? If not, specify the exact owner serialization contract.
4. Is the commit rule for revocation races and undelivered Estimate disposition accepted?
5. Are idempotency, lost-response reconciliation, atomic Audit Event/outbox and tenant-isolation behavior complete for the R2 scope?
6. Does any new ADR or schema choice materially change previously accepted Product/Finance/Data/Security policy and require a separate role decision?

**Exact ADR decision request:** the Architecture Owner must record `ACCEPT`, `ACCEPT_WITH_CHANGES` or `REJECT` against this file's published SHA-256, with role attribution and UTC decision time. If changed, publish a new ADR revision/digest and obtain a new exact-text decision before implementation approval. This draft itself does not represent that approval.
