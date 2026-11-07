# Issue #586 — Finance/Tax Decision Record v3

**Decision status:** F1–F8 `ACCEPT`; F9 `ACCEPT_WITH_CHANGES`; successor CalculationProfile is **not accepted, activated, or implementation-approved**.
**Evidence basis:** Finance decision request package v1.3, SHA-256 `ae8916b72b6f8c2ffb9811262ac484415efb44f074586a49d52563e868828041`.
**Product Owner basis:** package v1.1, SHA-256 `73c04fc138dcb36c34fff57169085ddbd87ada19b15d8be31dc4eae54b4e8325`; unchanged.
**Original candidate:** [`r2-commercial-v1` v1](issue-586-calculation-profile-candidate-v1.json), SHA-256 `8452636abbf6f7a653b593583b9cb835e5ebb2636be09d3a59f9bcddd2312724`; preserved unchanged.
**Successor candidate:** [`r2-commercial-v2` v2](issue-586-calculation-profile-candidate-v2.json), SHA-256 `506a314621aeda3b02f731a8befd8b0813844ef45c2eaf00a623da86b35eda08`; proposed technical translation only, not approved or activated.
**Decision date:** 2026-11-07. **Decision-maker attribution:** the human owner explicitly confirmed they also act as Finance/Tax Owner for #586. This record reports conversational confirmations; it is not a GitHub-authored owner comment or an independent electronic signature.

## 1. Attributable role-scoped decisions

The human owner individually accepted F1–F8 and accepted F9 with changes. The recorded confirmation for each individually presented item was «Да». The exact original question text for each individual presentation is not present in the decision evidence supplied for this record; the decision descriptions below reproduce only the scope expressly supplied with the confirmations and do not extend it. UTC confirmation times are preserved as reported.

| ID | Decision | UTC time | Accepted scope |
|---|---|---:|---|
| F1 | ACCEPT | 13:31:26 | Finance Owner is accountable financial authority. Eligible facts come from separately approved, immutable, versioned contract-specific rates, service/project-class books, or workspace/environment base books. Historical Estimates and LLM output cannot establish price authority. Manual overrides require separate approval. |
| F2 | ACCEPT | 13:31:55 | Deterministic precedence: exact contract/client/service/period; exact service + project class; exact service-only class; exact project-only class; workspace/environment base. Missing fields are not wildcards. Equal-priority conflict returns `PRICE_CONFLICT` unless an independent approved deterministic equivalence rule applies. Override is never fallback. |
| F3 | ACCEPT | 13:32:16 | Effective intervals are half-open `[from,to)`. `pricing_as_of` is the business quote date with explicit timezone normalization policy. No universal TTL. Exact source version and current approval are required. Revoked/superseded sources are unavailable to new calculations. Historical Estimates are immutable. An approved but undelivered Estimate whose basis is revoked requires recalculation and reapproval. Use typed stale/revoked outcomes. No global timezone or source-specific staleness duration was selected. |
| F4 | ACCEPT | 13:32:38 | Source and Estimate currency allowlist is RUB, USD, EUR, CNY. FX authority is official Bank of Russia exchange rates, with deterministic versioned effective-date policy and nominal normalization. Direct, inverse and RUB-mediated cross-currency conversion are deterministic. Calculation order is discount → tax → FX → final line quantization. Same-currency lines need no external observation. No unapproved lookup or LLM rate. Missing authorized rate returns `FX_RATE_UNAVAILABLE`. Actual observations remain governed source data to onboard later. |
| F5 | ACCEPT | 13:33:27 | Decimal-only; 34 significant digits; `ROUND_HALF_UP`; `Emin=-999999`, `Emax=999999`, `clamp=0`; decimal-string inputs, no binary floats; retain original candidate exceptional traps/range preflight and intermediate context semantics. Quantum is `0.01` for the four allowed currencies. Quantize each line once; Estimate total is exact sum of quantized line totals. Reconciliation is explicit, bounded and auditable. Pin exact profile/policy versions and deterministic serialization. |
| F6 | ACCEPT | 13:34:14 | Russian Federation is first supported jurisdiction; others require separately approved TaxPolicy versions. TaxBasis is versioned, authorized and based on applicable official rules. Explicit inclusion mode; inclusive base extraction; discount on pre-tax base; recalculate taxes after discount; explicit component order and compound references; exemptions need approved evidence. Missing/unsupported jurisdiction or rules block. Mixed inclusive/exclusive components require a separately approved complete formula. Actual rates, TaxBasis revisions and exemptions are not approved. |
| F7 | ACCEPT | 13:35:31 | 0% is explicit `NO_DISCOUNT` with no additional approval; above 0% through 10% inclusive requires authorized manager approval; above 10% through 20% inclusive requires Finance Owner approval; above 20% is prohibited by standard policy. One discount per line; stacking disabled. Percentage and fixed amount are candidate forms; fixed discount is converted to an equivalent percentage of pre-tax base for limit enforcement. Override needs reason, evidence, prior/proposed amounts and separate authorized human approval; requester cannot approve own request; source books remain immutable; material change makes successor Estimate with fresh approval. Security separately approves access and segregation. |
| F8 | ACCEPT | 13:37:24 | Fail-closed outcomes: `PRICE_NOT_FOUND`, `PRICE_SOURCE_STALE`, `PRICE_CONFLICT`, `PRICE_BASIS_REVOKED`, `PRICE_ACCESS_DENIED`, `FX_RATE_UNAVAILABLE`, `CURRENCY_POLICY_UNRESOLVED`, `TAX_JURISDICTION_UNSUPPORTED`, `TAX_RULE_UNRESOLVED`, `DISCOUNT_POLICY_UNRESOLVED`, `OVERRIDE_APPROVAL_REQUIRED`, `ESTIMATE_INPUT_INVALID`, `CALCULATION_RANGE_EXCEEDED`. Incomplete drafts may be saved; blocking errors prevent approval or issue as a completed proposal. No implicit price/FX/tax/discount fallback. Audit records exact blocker without unauthorized source disclosure. |
| F9 | ACCEPT_WITH_CHANGES | 13:38:14 | Original `r2-commercial-v1` v1 is not accepted unchanged. Preserve v1/digest. Prepare a versioned successor with F1–F8, immutable Estimate/reapproval, corrected inclusive formulas, deterministic recurring-fraction treatment and updated synthetic vectors. This accepts the required changes, **not** the unseen successor JSON, its digest, activation, or unresolved policy bindings. |

**Attribution boundary:** The owner separately confirmed Project Owner and Finance/Tax Owner roles. The supplied «Да» responses are role-scoped to the individually presented Finance/Tax decisions above. No identity beyond the role attribution supplied here, delegation document, GitHub-authored approval, or electronic signature is asserted. If governance requires repository-authored owner confirmation, obtain it separately before marking the Finance role approval repository-authenticated.

## 2. Finance authority and precedence accepted

Financial authority belongs to the Finance Owner, subject to separately attributable approval of each immutable source version. A source approval must bind the exact source object/revision/version/digest, price-item identity, workspace/environment, client/contract and service/project scope, currency, UOM, effective interval, authority decision and approving role. Approval is distinct from AccessPolicy and Data provenance acceptance.

Candidate rank, applied only after exact scope, access, source currentness and approval checks:

1. Exact contract/client/service/period rate.
2. Exact service + project-class price book.
3. Exact service-only class book.
4. Exact project-only class book.
5. Exact workspace/environment base book.

A missing scope key is a non-match, never a wildcard. Two eligible sources at the same rank return `PRICE_CONFLICT` unless a separately approved deterministic equivalence rule establishes equivalence. Arrival order, document recency, retrieval score, file order and LLM choice do not break ties. A manual override is a separate approval path, never a resolver fallback. Historical Estimates remain non-authoritative reference material.

## 3. CalculationProfile v2 successor proposal

The successor JSON preserves the original v1 artifact and records the F1–F8 decisions as profile fields. Its SHA-256 covers the exact UTF-8 file bytes. It is a proposed technical translation of accepted policy, not proof that the inputs or missing policy bindings exist.

### Exact operation sequence

1. Validate exact approved PricingBasis and source revision; current authority and access; quantity; UOM; source/Estimate currencies; `pricing_as_of`; and exact profile, tax, discount, FX and source policy references. Reject floats, non-finite values and invalid inputs.
2. `gross_source = quantity × unit_price`, in source currency, under the pinned Decimal context.
3. Resolve jurisdiction, explicit tax inclusion mode and full ordered TaxBasis. Unknown/unsupported/mixed modes without an approved complete formula block.
4. For exclusive prices, `base_before_discount = gross_source`. For inclusive prices, extract the pre-tax base using the ordered approved tax recurrence. Record any precision residual independently; never distribute it into a tax or base component. F9-T1 below is not yet approved.
5. Apply exactly one approved discount to the pre-tax base. Percentage discount is that percentage of base. Fixed amount is denominated in source currency; its equivalent percentage is `fixed_discount / base_before_discount` for limit checks. Positive fixed discount on a zero base blocks pending an explicit binding. `taxable_base = base_before_discount - discount_source`.
6. Calculate tax components in explicit order: `tax_base_i = taxable_base + sum(explicitly referenced prior compound components)`; `tax_i = rate_i × tax_base_i`. No implicit compounding. Exemptions require approved evidence.
7. `net_source = taxable_base + sum(post-discount tax_i)` plus the separately represented inclusive precision residual only if F9-T1 is later accepted. Do not add extracted pre-discount included tax a second time.
8. Apply the exact authorized FX observation after discount and all taxes. Normalize the official Bank of Russia nominal. For pair resolution: same-currency identity; exact direct observation; exact inverse observation; otherwise RUB-mediated factor `(RUB/source unit) / (RUB/target unit)`. Ambiguity, missing observation or invalid period returns `FX_RATE_UNAVAILABLE`. Non-business-day/effective-date behavior remains an explicit policy binding; no rate is inferred here.
9. Quantize each converted final line total once to `0.01` using `ROUND_HALF_UP`; no intermediate FX or tax currency quantization. Sum quantized line totals exactly; do not requantize Estimate total.
10. Quantize displayed components under the pinned profile and record only a bounded, explained display rounding adjustment. It cannot repair formula inconsistency. Inclusive extraction precision residual and final display rounding adjustment are separate fields.
11. Serialize canonical decimal strings and exact source/policy/profile revisions; digest the canonical inputs/results. Identical approved snapshots must replay to identical amounts and digests.

### F9-T1 — new Finance/Tax confirmation required

The approved v1 rule's unconditional exact equality of context-rounded inclusive components can reject a valid recurring fraction. The successor therefore proposes an explicit residual diagnostic, but F9 did not approve this precise residual policy.

**Recommended choice:** calculate `delta = gross_source - (base_before_discount + sum(included_tax_i_before_discount))` under the pinned Decimal context; retain `delta` as its own source-currency field; never assign it to tax/base; do not treat it as a price override. A deterministic interval/error propagation over the pinned arithmetic DAG derives an upper bound from each operation's Decimal rounding error, operand intervals and operation order. Accept extraction only if the residual lies within that derived bound; otherwise `TAX_RULE_UNRESOLVED`. Record the bound, operations, residual and profile digest. The bound is not a currency tolerance and is not applied after currency quantization. Candidate interval algorithm: propagate closed Decimal intervals through the inclusive extraction DAG. For each exact Decimal operation, use the same precision/exponent/trap configuration with `ROUND_FLOOR` for lower bounds and `ROUND_CEILING` for upper bounds; for add/subtract/multiply use endpoint extrema; for division evaluate endpoint quotients and fail if the denominator interval contains zero. The exact input values are point intervals. Recompute the declared tax recurrence as an interval and require the supplied gross point to lie inside the final recomposition interval. Persist each interval, operation, residual and profile digest. This interval pass only validates the precision residual; it does not replace the ROUND_HALF_UP commercial result. This is a proposed deterministic rule, still pending Finance/Tax confirmation. Whether the separately exposed delta participates additively in post-discount `net_source`, or instead causes blocking while remaining diagnostic-only, is also unresolved and requires explicit confirmation before finalization.

| Choice | Consequence |
|---|---|
| ACCEPT the bounded explicit residual proposal | A permitted precision residual remains visible and reproducible without allocation; requires pinning the interval propagation algorithm and display treatment. |
| REJECT; any nonzero residual blocks | Simpler invariant; some mathematically valid recurring cases cannot be calculated at the accepted precision. |
| CHANGE | Finance/Tax supplies another exact deterministic rule and test vectors. |

Status: `PENDING_FINANCE_TAX_CONFIRMATION`. No residual tolerance or allocation is currently approved.

### Display and reconciliation identities (v2 candidate; exact delta treatment pending F9-T1)

For a tax-exclusive source line, `gross_source = base_before_discount`. For a tax-inclusive source line, `gross_source = base_before_discount + sum(included_tax_before_discount_i) + tax_extraction_precision_delta`. The extracted pre-discount taxes explain the input price only; after discount, recomputed tax components replace them in the payable amount and must never be added a second time.

Candidate source-currency payable identity is `net_source = taxable_base + sum(post_discount_tax_i) + tax_extraction_precision_delta` only if the pending F9-T1 choice accepts additive delta treatment. Otherwise any nonzero delta blocks. After FX, quantize each final displayed component at the output currency quantum `Q`; define `display_reconciliation = quantized_line_total - (quantized_subtotal - quantized_discount + sum(quantized_post_discount_tax_components) + quantized_delta_if_accepted)`. Then `line_total = displayed_subtotal - displayed_discount + displayed_tax_components + displayed_delta + display_reconciliation`. `display_reconciliation` is solely a display rounding reconciliation, never a formula repair. Candidate absolute bound is `(m + 1) × Q/2`, where `m` is the number of signed displayed components (subtotal, discount, tax components and accepted delta); if exceeded, block with `TAX_RULE_UNRESOLVED` pending a dedicated typed output decision. The exact field exposure/error mapping remains part of the successor profile review.

## 4. Deterministic synthetic qualification vectors

All values below are synthetic mathematical fixtures, not tariffs, current FX observations, tax rates for an authorized production source, or jurisdiction-specific advice. Decimal context is precision 34 / `ROUND_HALF_UP`; line quantum `.01`.

| Vector | Inputs and operation | Expected result |
|---|---|---|
| V1 inclusive extraction | Gross `120`, single synthetic included rate `0.20`. | Pre-tax base `100`; included tax `20`; delta `0`; recomposed gross `120`. |
| V2 inclusive recurring | Gross `1`; ordered compound rates `0.07`, then `0.03` on base + first tax. | Base `0.9073586788857635423282823700208692`; tax1 `0.06351510752200344796297976590146084`; tax2 `0.02912621359223300970873786407766990`; residual `1E-34`. Must block unless F9-T1 accepts a deterministic bound; residual is not allocated. |
| V3 exclusive + discount-before-tax | Exclusive base `100`; percentage discount `0.10`; one synthetic rate `0.20`. | Discount `10`; taxable base `90`; tax `18`; source total `108`. |
| V4 multiple non-compound exclusive | Base `100`; rates `0.10` and `0.05`, both on same base. | Taxes `10`, `5`; total `115`. |
| V5 compound exclusive | Base `100`; first rate `0.10`; second `0.05` on base + first tax. | Taxes `10`, `5.5`; total `115.5`. |
| V6 FX nominal normalization and mixed currency | Synthetic approved observation says 900 RUB for nominal 10 USD; source amount `10 USD`; convert USD→RUB. | Normalized factor `90 RUB/USD`; converted amount `900 RUB`, then final line quantum. Fixture says nothing about a real observation or its authorization. |
| V7 inverse conversion | Same synthetic observation as V6, convert `10 RUB`→USD. | Decimal factor `1/90 = 0.01111111111111111111111111111111111` at precision 34; amount `0.1111111111111111111111111111111111 USD`; quantized line `0.11 USD`. |
| V8 final line rounding | Converted line amount `1.005` in any supported currency. | `1.01` under `ROUND_HALF_UP`. |
| V9 currency quantum | Values `1.005` in RUB, USD, EUR, CNY. | Each line result `1.01`; currency identity remains explicit for each output. |
| V10 total aggregation | Two lines quantize to `0.01` and `0.01`. | Estimate total `0.02`; no second quantization. |
| V11 discount thresholds | `0`, `0.10`, `0.1000001`, `0.20`, `0.2000001`. | `NO_DISCOUNT`; manager; Finance Owner; Finance Owner; prohibited respectively. |
| V12 missing FX | Source currency differs and there is no valid authorized direct/inverse/RUB path. | `FX_RATE_UNAVAILABLE`; no online lookup or fallback. |
| V13 conflicting price | Two currently authorized same-rank facts with different amounts and no equivalence approval. | `PRICE_CONFLICT`; no source selected. |
| V14 profile/source replay | Same exact canonical input snapshot, source revisions, policy/profile digests and approval refs replayed twice. | Byte-identical canonical output and digest. Any source/profile revision creates a different input identity and successor result. |
| V15 mixed tax modes | One line has one inclusive and one exclusive component without a separately approved joint formula. | `TAX_RULE_UNRESOLVED`; no estimate total. |
| V16 missing vs unsupported tax | Supported RU scope lacks applicable current TaxBasis; then, separately, a non-RU jurisdiction lacks an approved TaxPolicy. | First `TAX_RULE_UNRESOLVED`; second `TAX_JURISDICTION_UNSUPPORTED`. |
| V17 fixed discount limit | Pre-tax base `100`, fixed discount `10`. | Equivalent percentage `0.10`; manager approval required; discount `10`, taxable base `90`. |
| V18 override separation | Requester and proposed approver are the same actor. | `OVERRIDE_APPROVAL_REQUIRED`; no approved changed price; original basis unchanged. |
| V19 small quantization | Final line values `0.004`, `0.005`, `0.006`. | `0.00`, `0.01`, `0.01` respectively under HALF_UP. |
| V20 maximum in-range output coefficient | Final amount `99999999999999999999999999999999.99` in a supported currency. | Same value is representable with 34 coefficient digits; emit exactly that value if all other inputs are valid. |
| V21 output range exceeded | Final amount `100000000000000000000000000000000.00`. | `CALCULATION_RANGE_EXCEEDED` before emitting a quantized line. |
| V22 source/profile lifecycle | Calculate an Estimate, then revoke its basis or change its profile before delivery/approval. | Original Estimate version remains immutable; no new calculation uses revoked basis; successor requires fresh calculation/review/approval; exact approval cannot be reused. |

**Vector limitation:** V2 is intentionally pending-policy and is not a passing success case until F9-T1 is resolved; it currently demonstrates deterministic blocking. V7 pins the stated Decimal result for the specified precision-34 division. These are future implementation qualification cases, not tests executed against production code.

## 5. Remaining Finance/Tax bindings vs later governed data

### Policy bindings still needed before Finance can be considered fully implementation-ready

- Confirm F9-T1 residual policy and exact deterministic bound algorithm, including post-discount/display treatment.
- Quote-date timezone normalization and Bank of Russia effective-date/non-business-day selection policy; exact source-specific freshness policy and revocation decision effective semantics. No timezone, TTL or source delay invented here.
- Source policy/version references and approval evidence schema for each eligible price class; exact contract scope keys and service/project class taxonomy binding.
- FX observation ingestion/source revision and permitted observation effective/staleness interval bindings. Currency and authority are approved; operational source observations are not present.
- Russian Federation tax category mapping, official TaxBasis source/version, effective date selection, exempt evidence policy and invoice/report display requirements. Jurisdiction direction is approved; actual rates and tax rules are not approved by these confirmations.
- Discount policy reference and authority mapping for manager and Finance thresholds; fixed discount behavior for zero pre-tax base; exact request/approval evidence schema. The thresholds and no-stacking rule are accepted, but Security still must authorize permissions/segregation.
- Input scale and source-domain bounds; explicit end-to-end interval arithmetic specification for F9-T1.
- Exact revocation/currentness policy binding for approved-but-undelivered Estimates and estimate approval.

### Governed data that may be onboarded later, after policy approval

Actual contract rates, price-book rows, official FX observations, TaxBasis revisions/rates and exemption evidence are data instances, not policy decisions. They require authorized immutable source revisions and provenance when onboarded. No actual price, FX rate or tax rate is approved in this record.

### Other roles remain separate

Security access grants and segregation implementation, Data stable identity/row mapping/source quality and Architecture acceptance/ADR are not Finance decisions and remain pending in their separate records.

## 6. Required renewed Finance/Tax confirmation

Before treating the Finance policy as complete for implementation approval, obtain a separate attributable decision on F9-T1 and the unresolved policy bindings above. That decision must identify the exact Finance/Tax authority reference, UTC time, profile ID/version/digest and ACCEPT / ACCEPT_WITH_CHANGES / REJECT for each changed item. F1–F8 remain recorded against package v1.3 and are not reopened by this successor draft except where a material conflict is found.

## 7. Source references

- Finance request package v1.3: [`issue-586-r2-pricing-decision-package-v1.md`](issue-586-r2-pricing-decision-package-v1.md) (historical request digest above).
- Original proposed CalculationProfile v1: [`issue-586-calculation-profile-candidate-v1.json`](issue-586-calculation-profile-candidate-v1.json).
- Successor candidate v2: [`issue-586-calculation-profile-candidate-v2.json`](issue-586-calculation-profile-candidate-v2.json).
- Official Bank of Russia FX data service documentation: [Daily web service](https://www.cbr.ru/development/DWS/), [XML web service](https://www.cbr.ru/development/sxml/). These links describe source interfaces, not an approval of an observation or a rate.
- Separate approvals: [Product Owner](issue-586-product-owner-decision-v1.md), [Security](issue-586-security-approval-record-v1.md), [Data](issue-586-data-approval-record-v1.md), [Architecture](issue-586-architecture-acceptance-v1.md).
