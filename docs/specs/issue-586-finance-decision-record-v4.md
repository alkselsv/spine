# Issue #586 — Finance/Tax Decision Record v4

**Decision status:** F1–F8 ACCEPT; F9 ACCEPT_WITH_CHANGES; F9-T1 and subsequent F10–F16 ACCEPT / ACCEPT_WITH_CHANGES as recorded below. The v3 CalculationProfile candidate is **not approved or activated**.
**F1–F9 decision request:** package v1.3, SHA-256 `ae8916b72b6f8c2ffb9811262ac484415efb44f074586a49d52563e868828041`.
**Product Owner decision basis:** package v1.1, SHA-256 `73c04fc138dcb36c34fff57169085ddbd87ada19b15d8be31dc4eae54b4e8325`.
**Original profile v1:** [`r2-commercial-v1`](issue-586-calculation-profile-candidate-v1.json), SHA-256 `8452636abbf6f7a653b593583b9cb835e5ebb2636be09d3a59f9bcddd2312724`; unchanged.
**Profile v2:** [`r2-commercial-v2`](issue-586-calculation-profile-candidate-v2.json), SHA-256 `506a314621aeda3b02f731a8befd8b0813844ef45c2eaf00a623da86b35eda08`; historical candidate, unchanged and superseded as the successor proposal.
**Profile v3 candidate:** [`r2-commercial-v3`](issue-586-calculation-profile-candidate-v3.json), SHA-256 `208016dc497570cb0406ae6f92c8a4a402c24b18ff2ca1223f894804ef88a900`; proposed payload, not approved or activated.

## 1. Attribution and evidence scope

The human owner explicitly confirmed they act separately as Finance/Tax Owner; the separate role-confirmation timestamp was not supplied. Each Finance/Tax decision time is recorded below. The confirmations below are attributed only to the Finance/Tax capacity. Evidence is the explicit conversational approval reported for each individually presented decision, with the UTC time supplied below. The exact original prompt wording is not included in the evidence supplied for this record and is not reconstructed. These records are not a GitHub-authored human approval, external review, or electronic signature. No policy decision here names or grants a runtime user permission.

## 2. Original F1–F9 decisions

The F1–F9 decisions remain independently bound to package v1.3 and retain their original scope and times:

| ID | Decision | UTC on 2026-11-07 | Scope |
|---|---|---:|---|
| F1 | ACCEPT | 13:31:26 | Finance Owner is accountable financial authority. Eligible immutable, versioned sources are approved contract-specific rates, service/project-class books and workspace/environment base books. Historical Estimates/LLM output do not establish price authority. Manual price overrides require separate approval. |
| F2 | ACCEPT | 13:31:55 | Exact precedence: contract/client/service/period; service + project class; service-only class; project-only class; workspace/environment base. Missing fields are not wildcards. Same-rank conflicts fail closed absent an independent approved equivalence rule. Override is never fallback. |
| F3 | ACCEPT | 13:32:16 | Half-open `[from,to)`; `pricing_as_of` is business quote date with explicit timezone normalization and no universal TTL. Exact approved source version required. Revoked/superseded sources unavailable to new calculations; Estimates immutable; affected approved-undelivered Estimates require recalculation/reapproval. |
| F4 | ACCEPT | 13:32:38 | RUB/USD/EUR/CNY; official Bank of Russia FX authority; nominal normalization; deterministic direct/inverse/RUB-mediated conversion; discount → tax → FX → final line quantization; same-currency identity; missing authorized observation `FX_RATE_UNAVAILABLE`; no online or LLM rate. |
| F5 | ACCEPT | 13:33:27 | Decimal-only; 34 significant digits; `ROUND_HALF_UP`; `Emin=-999999`, `Emax=999999`, `clamp=0`; decimal-string inputs; accepted v1 traps/intermediate semantics; `.01` currency quantum; one final line quantization; exact sum of line totals; explicit bounded audit reconciliation. |
| F6 | ACCEPT | 13:34:14 | Russian Federation first supported jurisdiction; other jurisdictions need approved TaxPolicy; explicit inclusion mode; extract pre-tax base; discount before tax and recompute; ordered tax components/compound references; evidenced exemptions; unknown applicability blocks; mixed mode needs a complete separately approved formula. No actual rates/TaxBasis revisions/exemptions approved. |
| F7 | ACCEPT | 13:35:31 | 0% `NO_DISCOUNT`; above 0 through 10% authorized manager; above 10 through 20% Finance Owner; above 20% prohibited. One discount per line/no stacking; percentage/fixed amount; fixed amount measured against pre-tax base for threshold; independent human override approval; no self-approval or source-book mutation; successor Estimate/fresh approval after material change. |
| F8 | ACCEPT | 13:37:24 | Accepted typed fail-closed outcomes: `PRICE_NOT_FOUND`, `PRICE_SOURCE_STALE`, `PRICE_CONFLICT`, `PRICE_BASIS_REVOKED`, `PRICE_ACCESS_DENIED`, `FX_RATE_UNAVAILABLE`, `CURRENCY_POLICY_UNRESOLVED`, `TAX_JURISDICTION_UNSUPPORTED`, `TAX_RULE_UNRESOLVED`, `DISCOUNT_POLICY_UNRESOLVED`, `OVERRIDE_APPROVAL_REQUIRED`, `ESTIMATE_INPUT_INVALID`, `CALCULATION_RANGE_EXCEEDED`. Blocking Estimates cannot be approved/issued; no implicit fallback; audit blocker without unauthorized source disclosure. |
| F9 | ACCEPT_WITH_CHANGES | 13:38:14 | Preserve profile v1; make a versioned successor incorporating the specified F1–F8 policies, immutable Estimate/reapproval, inclusive-tax correction, recurring fractions and revised vectors. This did not accept any unseen successor payload or activate a profile. |

## 3. Subsequent Finance/Tax decisions

The human Finance/Tax Owner separately accepted the following additional decisions. Decision labels F10–F16 are record identifiers for these confirmations; they do not alter the original F1–F9 package binding. The supplied supplemental confirmations were not bound to a separate package digest; they do not approve the later profile v3 JSON or ADR text.

| ID | Result | UTC on 2026-11-07 | Confirmed policy |
|---|---|---:|---|
| F9-T1 | ACCEPT_WITH_CHANGES | 14:04:04 | Inclusive tax extraction residual is diagnostic/audit only; never added to `net_source`, tax or taxable base, never distributed; use directed-rounding intervals; block when tax equation cannot be validated or final currency-quantized result is not unique; display reconciliation remains separate. |
| F10 Quote date / FX effective date | ACCEPT | 14:05:18 | `pricing_as_of` is `YYYY-MM-DD` business quote date. Timestamp normalization requires an explicitly approved workspace IANA timezone; no default. Select by official FX effective date; non-business dates use latest officially effective rate, not latest downloaded. Pin exact observation/source version/effective date; missing/ambiguous authority is `FX_RATE_UNAVAILABLE`. |
| F11 Cross-currency | ACCEPT | 14:06:12 | Same-currency identity. RUB pairs use approved Bank of Russia quotations. Non-RUB pairs use `FX(A→B) = (RUB per unit A)/(RUB per unit B)`. Normalize nominal; no intermediate monetary quantization; all required observations apply to the same quote date; missing/ambiguous path blocks. |
| F12 Zero-base fixed discount | ACCEPT | 14:10:12 | Base 0 + discount 0 is `NO_DISCOUNT`; base 0 + positive discount or discount above base is `DISCOUNT_POLICY_UNRESOLVED`; negative discount is `ESTIMATE_INPUT_INVALID`; positive-base fixed discount uses `discount/base` for limits; no division by zero. Zero-priced item only from approved source. |
| F13 Numeric input | ACCEPT | 14:11:38 | Decimal string only, maximum 34 significant input digits; quantity > 0; unit price ≥ 0; source-specific scale/domain policies are explicitly versioned; never silently round input. Invalid scale is `ESTIMATE_INPUT_INVALID`; arithmetic range is `CALCULATION_RANGE_EXCEEDED`; missing numeric source schema blocks. |
| F14 Source publication | ACCEPT | 14:13:04 | Every financially active source version has attributable Finance approval, is immutable and explicitly scoped, and requires PriceItem identity, UOM, currency, effective date, exact SourceRevision/digest. Import/sync is not approval. New content needs new approval; supersession/revocation explicit. Data validates independently, Security authorizes independently, Architecture governs atomic activation. |
| F15 Russian TaxBasis | ACCEPT | 14:15:58 | Russian Federation first supported. Approved immutable TaxBasis revisions must derive from applicable official legal/tax authorities, with explicit service category/regime and effective periods. No LLM or name-based tax inference; exemptions require approved evidence; unknown applicability blocks; historical Estimates are not rewritten. No actual Russian tax rate/rule revision was approved. |
| F16 Approval authority | ACCEPT | 14:16:56 | No-discount needs no additional discount approval; >0–10% manager; >10–20% Finance Owner; >20% prohibited. Manual price override requires independent authorized approver. Finished Estimate requires Estimate Approver. No requester self-approval. Bind approval to exact Estimate version and calculation digest; material change creates successor/fresh approval. Security separately governs concrete assignments/enforcement. |

## 4. Successor CalculationProfile v3 proposal

The exact candidate is `r2-commercial-v3`, version 3. Its digest will be calculated over the final exact UTF-8 JSON bytes and included here and in the consolidated status record. This payload is a proposed technical translation. No confirmation in this record approves its unseen exact digest.

### Calculation operations

1. Validate exact approved PricingBasis, PriceItem, SourceObject/SourceRevision and digest; explicit scope; UOM; currency; date; authorization; source schema; and every pinned policy version. Inputs are finite decimal strings only, at most 34 significant digits; do not coerce or round input. Require quantity > 0 and unit price ≥ 0. Zero price requires an approved source.
2. Compute `gross_source = quantity × unit_price` in the accepted Decimal context.
3. Resolve the explicitly approved tax jurisdiction, category, TaxBasis version, effective period, inclusion mode, components and compound references. No tax inference from description/model. Unsupported jurisdiction → `TAX_JURISDICTION_UNSUPPORTED`; unknown/missing supported rule → `TAX_RULE_UNRESOLVED`.
4. For exclusive lines, `base_before_discount = gross_source`. For inclusive lines, solve the ordered approved tax recurrence at precision 34 / `ROUND_HALF_UP`; compute `tax_extraction_residual_diagnostic = gross_source - (base_before_discount + Σ included_tax_before_discount)` solely for audit/validation. It is never added to `net_source`, taxable base, tax, FX input, Estimate total or display reconciliation.
5. Validate the tax equation by outward interval propagation. Inputs are exact point intervals. Addition/subtraction use endpoint bounds with `ROUND_FLOOR` for lower and `ROUND_CEILING` for upper values. Multiplication uses endpoint extrema. Division fails if the denominator interval contains zero and uses outward endpoint quotients. The exact ordered tax formula must enclose `gross_source`; otherwise block `TAX_RULE_UNRESOLVED`.
6. Apply one approved discount to the pre-tax base. No stacking. For base=0 and discount=0 return `NO_DISCOUNT`; positive fixed discount at base 0 or discount above base → `DISCOUNT_POLICY_UNRESOLVED`; negative discount → `ESTIMATE_INPUT_INVALID`. For positive base compute fixed discount percentage as `discount/base` and enforce approved thresholds.
7. Recalculate post-discount tax components in approved order. `tax_base_i = taxable_base + Σ explicitly referenced prior compound components`; `tax_i = rate_i × tax_base_i`. `net_source = taxable_base + Σ post_discount_tax_i`. Never add the extraction diagnostic or pre-discount included tax again.
8. Convert after tax. Normalize the official Bank of Russia nominal. Let `rub_per_unit(C)` be the exact authorized normalized observation. Same currency is identity; for any pair `A→B`, `FX = rub_per_unit(A)/rub_per_unit(B)`. The selected observations must use the same official effective date selected for the business quote date. No intermediate currency quantization.
9. Propagate an interval for the final amount through the exact approved discount/tax/FX operation DAG. Quantize both interval endpoints to the Estimate currency quantum using `ROUND_HALF_UP`. If the results differ, block `TAX_RULE_UNRESOLVED`; if equal, quantize the ordinary pinned Decimal calculation once to that unique result. Arithmetic range failure → `CALCULATION_RANGE_EXCEEDED`.
10. Derive separate display-component quantization and bounded display reconciliation after successful equation/uniqueness validation. The reconciliation cannot contain the tax extraction diagnostic or repair a formula. Sum the final quantized line totals exactly, with no Estimate-level re-quantization.

### FX effective date and `pricing_as_of`

`pricing_as_of` is a calendar date in `YYYY-MM-DD`. A timestamp supplied at a boundary is converted only using the exact approved workspace IANA timezone binding; no host timezone/UTC/local default is allowed. Select the latest official Bank of Russia effective date not later than the quote date. A cross-currency calculation requires every component observation to be authorized for the same selected effective date. `downloaded_at` never selects a rate. Missing, conflicting, stale or ambiguous authorized observations return `FX_RATE_UNAVAILABLE`. The actual workspace timezone and observations are not supplied by these policy decisions.

### Source publication and authorization

A financially active source version must be immutable and explicitly scoped; bind stable PriceItem identity, unit, currency, effective interval, exact source revision/digest and Data mapping/validation version. Require attributable Finance approval, independent Data validation and current Security authorization before Architecture’s atomic activation. Import/sync does not approve; new content requires revalidation and Finance approval; supersession/revocation is explicit; no automatic reactivation. These roles define policy responsibilities, not concrete user grants.

### Estimate and approver bindings

Price/discount/source changes create immutable successor artifacts. Discount, override and finished Estimate decisions bind the exact Estimate version and calculation digest. Requester cannot approve their own request/Estimate. Current authorization and source currentness are checked at commit. No source price changes through Estimate review. Reuse the existing R2 WorkItem/WorkflowRun and canonical artifact persistence.

## 5. Approved policy vs bindings and data

### A. Approved with concrete policy values

- Source authority classes and F1–F2 precedence, conflict and no-wildcard rules.
- Half-open pricing intervals and `pricing_as_of` meaning; no universal TTL.
- Currency set, FX authority, nominal normalization, cross-rate formula, same-quote effective date and conversion order.
- Decimal context, input digit maximum, accepted Decimal traps/intermediate semantics, quantization and output currency quantum.
- Supported-jurisdiction direction and tax calculation principles; no name/LLM inference.
- Discount forms, thresholds, no-stacking, zero-base behavior and approver role categories.
- Source publication approval responsibilities, immutable versions, revocation and Estimate reapproval principles.

### B. Approved principle; exact implementation contract formalized in v3/ADR

- Directed interval arithmetic mechanics and unique currency-quantized result requirement.
- Exact canonical identity/digest fields for each source record, approved TaxBasis and calculation snapshot.
- Canonical registry lifecycle, activation transition and current-authority/CAS/outbox/idempotency transaction details.
- Security capabilities, actor independence and audit disclosure boundaries.

These records do not mean the v3 JSON or ADR-0062 proposal has been accepted as exact contract text.

### C. Configuration/source data to supply later under approved policy

- Workspace IANA timezone value.
- Source-specific input scale/domain policies and schema versions.
- Actual price books, contract rates, PriceItem mappings, SourceRevision digests and Data Validation reports.
- Actual official FX observations/source revisions by effective date.
- Actual TaxBasis revisions, tax rates, service-category mappings and exemption evidence.
- Concrete security principals, role assignments, audit field allowlists and retention bindings.

### D. Material decisions still required

- Finance/Tax acceptance of the exact profile v3 payload and digest after this proposal is published.
- Architecture Owner acceptance of the exact ADR-0062 text (the prior confirmation approved principles, not this unseen ADR).
- Any future change to the approved Finance policies or a material conflict found during contract/spec review.

## 6. Qualification reference

The deterministic synthetic vectors and acceptance matrix are in [`issue-586-pricing-policy-qualification-v1.md`](issue-586-pricing-policy-qualification-v1.md). They do not use production source data or call an external provider. PostgreSQL authorization/CAS/idempotency and recovery qualification is specified but not claimed executed.

## 7. Separate role records

- Product: [Product Owner decision](issue-586-product-owner-decision-v1.md), unchanged.
- Data: [Data approval record v2](issue-586-data-approval-record-v2.md).
- Security: [Security approval record v2](issue-586-security-approval-record-v2.md).
- Architecture: [Architecture acceptance record v2](issue-586-architecture-acceptance-record-v2.md) and [proposed ADR-0062](../adr/0062-canonical-pricing-authority-and-estimate-commit.md).
