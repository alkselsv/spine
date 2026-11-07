# Issue #586 — Consolidated Pricing Decision Status v1.5

**Status:** ROLE POLICY DECISIONS RECORDED; exact CalculationProfile v3 and ADR-0062 acceptance pending; NOT IMPLEMENTATION-APPROVED.
**Issue:** [#586](https://github.com/alkselsv/spine/issues/586), remains OPEN.
**Existing PR:** [#1062](https://github.com/alkselsv/spine/pull/1062), documentation-only, remains Draft.
**Current main verified:** `2092f2b132efc647ce3112da63ba26556f73fd5f`.

## Historical decision identities

- Product Owner acceptance: package v1.1, SHA-256 `73c04fc138dcb36c34fff57169085ddbd87ada19b15d8be31dc4eae54b4e8325`; 2026-11-07 11:11:07 UTC. Scope remains Option 2 and product-scoped D1–D10 only.
- Finance/Tax F1–F9 request: package v1.3, SHA-256 `ae8916b72b6f8c2ffb9811262ac484415efb44f074586a49d52563e868828041`.
- Original CalculationProfile v1: SHA-256 `8452636abbf6f7a653b593583b9cb835e5ebb2636be09d3a59f9bcddd2312724`, preserved.
- Finance successor v2: SHA-256 `506a314621aeda3b02f731a8befd8b0813844ef45c2eaf00a623da86b35eda08`, retained historical candidate; its additive residual proposal is superseded by F9-T1.
- Proposed CalculationProfile v3: SHA-256 `208016dc497570cb0406ae6f92c8a4a402c24b18ff2ca1223f894804ef88a900`; **not accepted or activated**.
- Proposed ADR-0062: SHA-256 `498cd09b67f88c627f4b6d2307267e2ca5e4b686fc5623899e2aba6730c653f8`; **not accepted**.

## Five independently attributed governance capacities

The Project Owner stated that the same human acts in Product, Finance/Tax, Data, Security and Architecture capacities. Each decision below remains separately role-scoped. This does not assert independent external review, waive application-user segregation, create named grants, or prove runtime enforcement. Where no external delegation instrument was supplied, evidence is the conversational role self-attribution and the decision times below; no credential, signature or authority document is invented.

| Capacity | Decision | Confirmation time(s), UTC 2026-11-07 | Record and boundary |
|---|---|---|---|
| Product Owner | ACCEPT Option 2 and product-scoped D1–D10 | 11:11:07 | [PO v1 record](issue-586-product-owner-decision-v1.md), bound to package v1.1. Unchanged. |
| Finance/Tax Owner | F1–F8 ACCEPT; F9 ACCEPT_WITH_CHANGES | 13:31:26, 13:31:55, 13:32:16, 13:32:38, 13:33:27, 13:34:14, 13:35:31, 13:37:24, 13:38:14 | [Finance v4](issue-586-finance-decision-record-v4.md), bound to package v1.3. F9 accepts changes, not an unseen profile. |
| Finance/Tax Owner follow-up | F9-T1 ACCEPT_WITH_CHANGES; F10–F16 ACCEPT | 14:04:04; 14:05:18; 14:06:12; 14:10:12; 14:11:38; 14:13:04; 14:15:58; 14:16:56 | Finance v4. Profile v3 exact digest remains pending. |
| Data Owner | Data-1..4 ACCEPT | role confirmation 14:17:56; decisions 14:18:58, 14:19:29, 14:20:43, 14:21:47 | [Data v2](issue-586-data-approval-record-v2.md). Policy accepted; no actual source mapping was inspected. |
| Security Owner | Security-1..4 ACCEPT | role confirmation 14:22:49; decisions 14:24:26, 14:25:06, 14:25:43, 14:26:33 | [Security v2](issue-586-security-approval-record-v2.md). Policy accepted; no runtime permission grants/enforcement are proven. |
| Architecture Owner | Architecture-1..3 ACCEPT | role confirmation 14:27:05; decisions 14:28:09, 14:28:58, 14:30:12 | [Architecture v2](issue-586-architecture-acceptance-record-v2.md). Principles accepted; exact ADR-0062 text remains pending. |

All confirmations are reported conversational evidence. Exact original prompt wording for individual decisions was not supplied for transcription and is not reconstructed. The GitHub status comment is a record of this evidence, not a human-authored electronic signature or an additional approval.

## Consolidated policy state

**Finance/Tax policy accepted:** eligible source classes and precedence; effective-date semantics; currencies RUB/USD/EUR/CNY; official Bank of Russia FX authority, nominal normalization and RUB-mediated formula; accepted Decimal context/input maximum/output quantum; RU as first supported jurisdiction and official TaxBasis policy; discounts/approver tiers; no self-approval; source publication approvals; diagnostic-only tax extraction residual with directed interval validation and unique final quantization; listed fail-closed outcomes.

**Data policy accepted:** workspace/environment-stable PriceItem identity, exact SourceRevision/digest and in-revision mapping, typed UOM/currency/tax metadata, deterministic provenance and validation, explicit revocation/supersession, no automatic identity merge or source re-publication.

**Security policy accepted:** separate least-privilege capabilities, AccessPolicy separate from financial authority, canonical-actor segregation, exact-version approval, commit-time reauthorization, scoped/non-disclosing audit and workspace/environment isolation.

**Architecture principles accepted:** one PostgreSQL-canonical PricingAuthorityRegistry within Spine canonical persistence, immutable source/Estimate versions, current authority and AccessPolicy at commit, exact-version CAS, atomic Audit Event/outbox/idempotency, provider fetch outside transaction, reuse of one R2 WorkItem/WorkflowRun and artifact persistence, no source-price mutation, no separate workflow/store.

## Remaining bindings and decisions

**Configuration/source instances to provide later under approved policy:** workspace IANA timezone; source-specific scale/domain profiles; actual PriceItem mappings and exact SourceRevision digests; Data Validation reports; actual official FX observations; actual TaxBasis revisions/rates/service-category mappings/exemption evidence; named runtime principal grants; audit field allowlist/retention terms. No prices, real exchange observations or tax rates are supplied here.

**Exact owner decisions still required:**

1. Finance/Tax ACCEPT / ACCEPT_WITH_CHANGES / REJECT against the exact CalculationProfile v3 digest above. F9-T1 approved the diagnostic-only residual policy, but did not approve this unseen JSON payload.
2. Architecture Owner ACCEPT / ACCEPT_WITH_CHANGES / REJECT against the exact ADR-0062 digest above. Architecture-1..3 accepted principles, not this exact text or proposed detailed transition/locking/recovery graph.
3. Any material change requested in either review must be issued as a new version/digest and approved in its role-scoped capacity before implementation authorization.

## Issue acceptance-criteria assessment

| #586 criterion | Current state |
|---|---|
| Product/Finance select authority, precedence and freshness | Policy decisions recorded; workspace timezone and source-specific bindings remain configuration. |
| Security/Data define source/override authorization and data quality | Role policies accepted; concrete principals and source mappings remain to be provisioned/validated. |
| Currency, discount, tax and rounding are deterministic | Policies and v3 candidate specified; exact v3 payload approval pending; actual TaxBasis data later. |
| Missing/stale/conflicting outcomes and override audit | Typed behavior and audit principles accepted; detailed runtime fields/retention and enforcement unqualified. |
| Approved facts remain distinct from model conclusions/historical analogues | Accepted across all role records; no LLM or historical Estimate authority. |

**Closure decision:** Issue #586 is **not ready for closure** because Finance has not approved the exact successor profile digest and Architecture has not accepted the exact ADR-0062 digest. Configuration/source bindings and runtime enforcement also remain before implementation/production readiness. Keep the Issue OPEN and PR Draft; this is not production pricing authorization.
