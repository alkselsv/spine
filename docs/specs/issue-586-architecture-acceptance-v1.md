# Issue #586 — Architecture acceptance record v1

**Status:** PROPOSED / PENDING_ARCHITECTURE_ACCEPTANCE
**Decision package:** [Issue #586 D1–D10](issue-586-r2-pricing-decision-package-v1.md)
**Implementation handoff:** [Pricing contracts and handoff](issue-586-pricing-implementation-handoff-v1.md)

This record checks compatibility with current Spine boundaries. It is not Product, Finance, Security or Data approval.

## Compatibility review

| Concern | Existing contract / assessment | Acceptance state |
|---|---|---|
| Source identity/provenance | Reuse tenant-scoped `SourceObject`, immutable `SourceRevision`, provenance, canonical acceptance and exact locators. Price approval is a separate binding; current source head alone is not price authority. | PROPOSED — confirm |
| Access vs pricing authority | Reuse current `AccessPolicy` and trusted context for source content. Add a separately approved pricing business permission/authority decision; source ACL, connector grant, admin status and Context Profile do not grant it. | PROPOSED — confirm |
| Workflow | One R2 `WorkItem` and canonical `WorkflowRun`; typed commands/events and Temporal for durable waits/retries. No pricing-specific workflow engine/run. | PROPOSED — confirm |
| Estimate immutability | Typed Estimate is an immutable versioned artifact with predecessor, exact input/policy digests and lineage. Approval binds exact current version. | PROPOSED — confirm persistence/CAS boundary |
| CAS and reapproval | Human command validates expected run and artifact version/digest. Amount/basis/policy changes create successor and invalidate predecessor approval for new version. | PROPOSED — confirm atomic admission and state transition |
| Audit/outbox/idempotency | Reuse canonical AuditEvent, diagnostics separation, existing command digest/replay and transactional outbox. No independent pricing audit store. | PROPOSED — confirm atomic effects and event registry changes |
| Context Broker | Reuse authorized Broker and versioned Context Profile only for bounded retrieval after authorization. Profile cannot assign pricing authority or make selection/approval. | PROPOSED — confirm |
| Calculation | Pure versioned Decimal code step receives exact typed basis/profile; no LLM money arithmetic, provider lookup or hidden persistent loop. | PROPOSED — confirm |
| Source mutation | Estimate review/override creates successor decision/artifact only; never updates original price source. | PROPOSED — confirm |

## ADR decision

**No existing accepted ADR directly conflicts with Option 2.** ADR-0011 (canonical AccessPolicy), ADR-0018 (source revision/publication lifecycle), ADR-0020 (audit/diagnostic separation), ADR-0029 (immutable evidence handoff), ADR-0047 and ADR-0058 (immutable requirements artifacts and exact human decisions), and ADR-0048 (current evidence authorization) provide compatible boundaries. ADR-0044/0050/0053 reinforce that provider/source access and associations confer no PricingBasis authority.

**A new ADR is recommended before implementation approval** because the repository has no accepted decision for the canonical ownership and atomic lifecycle of approved pricing authority. The exact architecture question is:

> Which PostgreSQL-canonical versioned records bind an exact accepted SourceRevision to Finance-approved price authority/rule-set scope, and what atomic command/CAS boundary activates a successor policy and binds a deterministic Estimate artifact version to current source authorization, approval and idempotent audit/outbox effects?

The ADR must also settle whether the approved basis/rule-set belongs in the existing Artifact domain or a dedicated canonical pricing domain module/repository, and how source revocation is serialized against Estimate calculation/approval. It must preserve one R2 WorkflowRun, existing AccessPolicy as source-content authority, immutable artifacts and no source-price mutation. This is an ADR requirement recommendation, **not** a fabricated accepted ADR. Do not edit or supersede accepted ADRs silently.

## Architecture acceptance record

- **Decision:** PENDING — select `ACCEPT`, `REJECT`, or `ACCEPT_WITH_CHANGES` per concern above.
- **Architecture Owner identity and authority reference:** PENDING.
- **Exact package/record version and digest:** PENDING.
- **Decision time:** PENDING.
- **ADR:** Required question accepted for separate ADR before implementation approval; exact ADR approval/number remains pending.
- **Rationale and boundary changes:** PENDING.

## Architecture acceptance criteria

- [ ] Confirms reuse of SourceObject/SourceRevision, current AccessPolicy, Context Broker, WorkItem/WorkflowRun, artifact, AuditEvent and outbox/idempotency contracts.
- [ ] Confirms no second WorkflowRun/engine, independent pricing store, pricing authority via Context Profile, or source-price mutation.
- [ ] Defines immutable Estimate version and current-version approval/CAS boundary.
- [ ] Records the canonical pricing authority ownership/activation question in an ADR before implementation is marked ready.
- [ ] Keeps Product, Finance, Security and Data approvals separately required and attributable.
