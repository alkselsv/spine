# Issue #586 — Finance pricing policy approval checklist v1

**Status:** PROPOSED / PENDING_FINANCE_APPROVAL
**Decision package:** [Pricing authority D1–D10 v1](issue-586-r2-pricing-decision-package-v1.md)
**Product direction:** [Project Owner decision record](issue-586-product-owner-decision-v1.md)

This record requests Finance (and where applicable Tax) decisions on policy shape. It does not approve a rate, currency, tax regime, FX provider, discount limit or authority. Blank values are deliberate blockers. Actual approved tariffs can be loaded later, but the authority, formulas, and interpretation rules below must be settled before #586 is accepted as implementation-ready.

## 1. Authority and source precedence

**Proposed policy:** only explicitly Finance-approved exact source revisions and rule-set versions may provide current pricing facts. Select in this order:

1. Exact approved customer/contract + service/item + effective-period rate.
2. Approved service/project-class price book; specificity: exact service+project, exact service-only, exact project-only.
3. Approved workspace/environment base price book.
4. Manager override only through a separate request and approval; it is not an automatic fallback.

Historical Estimates and model conclusions are not pricing authority. Equal rank without an approved equivalence rule returns `PRICE_CONFLICT`. Source availability or Finance read access alone does not approve the price.

| Finance decision | Proposed value / response |
|---|---|
| Authority owner and approver identity/reference for each source class | PENDING |
| Required approval evidence and exact version/digest fields | PENDING |
| Accept/change the four classes and precedence above | PENDING |
| Service/project class vocabulary and within-tier specificity | PENDING |
| Tie/equivalence and source supersession procedure | PENDING |
| Scope of contract-specific rates and authorized customer key | PENDING |

## 2. Currency and FX

| Finance decision | Proposed rule / required value |
|---|---|
| Allowed source currencies per workspace/environment | PENDING — explicit ISO currency allowlist |
| Allowed Estimate/output currencies | PENDING — explicit ISO currency allowlist |
| Authoritative FX source/provider and approval authority | PENDING — exactly one governed authority per policy version; do not fill by agent |
| Exact FX record | Must pin base/quote pair and direction, Decimal rate, as-of instant/date, source identity/revision/version/digest and approval. |
| Missing/expired/unapproved pair | `FX_RATE_UNAVAILABLE`; block complete calculation and approval; no online lookup or cached implicit fallback. |
| **Proposed FX stage** | Calculate gross, discount and all tax components in PricingBasis/source currency; convert the completed line amount to Estimate currency after tax calculation and before output quantization. Pin one exact rate for that line. Finance must accept or replace this stage. |
| FX age/as-of semantics and any tolerance | PENDING — set in source policy, no universal TTL. |

## 3. Recommended CalculationProfile to accept or replace

The following one-profile proposal resolves operation order while leaving the business policy values to Finance. A calculation is not permitted until the accepted profile has a version and digest.

### Inputs and numeric context

1. Accept only finite Decimal quantity, unit price, rates and discount values. Parse source numerics by a governed representation rule; locale-formatted strings are not passed directly to the calculator.
2. Candidate arithmetic context: **34 significant digits**, explicit `ROUND_HALF_UP` context rounding, traps for invalid operation, division by zero and overflow. Finance approves or replaces precision, context rounding, trap set and supported input scales. No float conversion.
3. Resolve the versioned currency exponent/quantum table. Missing currency precision blocks; do not assume two decimal places.
4. Sort lines by stable `line_id` for canonical output and digest.

### Per-line operations

1. Validate quantity/UOM against the selected PriceItem and Estimate UOM. Validate source and output currency and exact PricingBasis/FX/Tax/Discount references.
2. `gross_source = quantity × unit_price` in PricingBasis currency.
3. Apply exactly one selected approved discount rule before tax. Persist discount type, basis, exact amount and authority. Stacking is **disabled in this proposal** unless an approved policy defines an ordered composition; missing or competing discount rules block.
4. Resolve tax mode from exact approved TaxBasis. For tax-exclusive prices, initial taxable base is `gross_source − discount_source`. For tax-inclusive prices, define `inclusive_amount = gross_source − discount_source` (the post-discount amount still including tax); the rule must provide an exact versioned extraction formula. For one non-compound tax component the candidate is `included_tax = inclusive_amount × rate / (1 + rate)`, `taxable_base = inclusive_amount − included_tax`. For multiple or compound inclusive tax components, the approved TaxBasis must pin one complete deterministic extraction formula for the ordered component set; if it is absent or does not match the components, block with `TAX_RULE_UNRESOLVED`. No guessed generic formula.
5. Calculate tax components in ascending explicit `tax_order`. Each component declares taxable base formula and whether prior tax is included (compound). Duplicate order, missing base/rate, incompatible category or unresolved exemption blocks as `TAX_RULE_UNRESOLVED`. Rates and rule applicability come only from the approved authority version.
6. `net_source = taxable_base + sum(applicable tax components)` for exclusive mode; inclusive mode retains the supplied inclusive amount as net after extracting the included tax component values. Store gross, discount, taxable base and each tax component as exact source-currency Decimal values.
7. If currencies differ, multiply `net_source` by the exact pinned FX rate in the declared direction **after tax and before rounding**. Convert report components with the same pinned rate. No intermediate currency quantization.
8. Quantize the final Estimate-currency line total once to its approved quantum using the approved output rounding mode. Candidate: `ROUND_HALF_UP`. This is the line's authoritative total.

### Estimate aggregation and rounding

9. Compute `Estimate.total` as the exact sum of quantized line totals in stable line order. Do not round it a second time.
10. Compute Estimate-level subtotal, discount and per-tax-component reporting totals from their exact converted components; aggregate first, then quantize each displayed aggregate once to the same output currency quantum/mode.
11. Persist `rounding_adjustment = Estimate.total − (displayed_subtotal − displayed_discount + displayed_tax_components)` so displayed components reconcile to the sum-of-lines total. The adjustment is explicit, visible and auditable; it is not a hidden price change.
12. Retain exact intermediate Decimals and profile/rule digests for replay. Any change in profile, FX/tax/discount source, input or engine version creates a successor Estimate and requires review.

This proposal determines a coherent sequence but **does not declare Finance approval**. The CalculationProfile must version at least: precision, context rounding/traps, accepted input scale, currency quantum table, source/estimate currency behavior, tax formulas/order/compounding, inclusive extraction, discount composition, FX stage, line quantization, total aggregation, aggregate-report quantization, output rounding and reconciliation display.

## 4. Tax policy decisions

| Finance/Tax decision | Proposed behavior / required value |
|---|---|
| Jurisdictions and their authority/source | PENDING — no country presumed |
| Service tax categories and mapping owner | PENDING |
| Tax-inclusive vs tax-exclusive source interpretation | Explicit per approved PriceItem/TaxBasis; missing value blocks |
| Discount interaction | Discount before tax in the proposed profile; accept/change explicitly |
| Multiple taxes | Explicit ordered components; each has rate, base formula, compound flag and effective period. Ambiguous/duplicate order blocks. |
| Inclusive extraction when multiple taxes apply | Exact deterministic formula per versioned policy; absent formula blocks. |
| Exemptions/special regimes | Exact approved evidence and applicability rule; no model inference |
| Tax effective period/as-of and rounding | PENDING; align with pricing_as_of and profile quantum/order |
| Unknown applicability | `TAX_RULE_UNRESOLVED`; do not issue a complete/approved Estimate |

## 5. Discount and override authority

| Finance decision | Required response |
|---|---|
| Allowed discount forms (percent/fixed/contract formula) | PENDING |
| Numeric discount limits and scope | PENDING — no limit invented |
| Stacking and ordering | Proposed single discount per line; additional discounts disabled absent explicit composition policy |
| Roles allowed to request an override | PENDING; coordinate with Security record |
| Roles allowed to approve an override | PENDING; explicit independent approver proposed |
| Whether requester/source maintainer may approve own request | Proposed **no**; separate actor is required. Finance must accept/change. |
| Required reason/evidence and prior/proposed values | Required: reason code + explanation, exact prior basis/version/value, proposed value/version, actor, evidence, approver and decision ref. |
| Override effect | New Estimate successor only; no source price-book mutation and no inherited approval. |
| Missing limit/approver/policy | `OVERRIDE_APPROVAL_REQUIRED`; line remains unresolved and Estimate cannot be approved. |

## 6. Policy decisions versus later price data

**Required for #586 acceptance:** approved authority classes/order/scope; currencies and FX authority/stage; Decimal/rounding profile; tax sourcing, inclusion, order and multiple-tax behavior; discount and override limits/approvers; missing-price/FX/tax behavior; effective-date/revocation rules; exact line evidence and reapproval rules.

**Can be populated later through governed source onboarding:** actual service tariffs, item descriptions and amounts; active customer contract prices; actual approved FX observations; applicable tax rates/exemptions; actual price book rows. Each data publication must still have exact approved provenance and effective scope. No value from historical Estimate or model output may be silently promoted.

## 7. Finance approval record

- **Decision:** PENDING — select `ACCEPT`, `REJECT`, or `ACCEPT_WITH_CHANGES` for each section and resolve every required value above.
- **Finance/Tax approving identity and role authority reference:** PENDING.
- **Exact package/checklist version and digest:** PENDING.
- **Decision time:** PENDING.
- **Accepted CalculationProfile version/digest:** PENDING.
- **Rationale, changes and remaining policy parameters:** PENDING.

## Finance acceptance criteria

- [ ] Approves exact price-authority classes, approval evidence, source precedence/specificity, conflict and supersession handling.
- [ ] Records allowed source and Estimate currency sets and exactly one approved FX authority/policy; explicitly approves the after-tax conversion stage or replaces it.
- [ ] Approves internal Decimal precision, traps/input rules, rounding mode, output currency quantum source, line and estimate quantization points, aggregation and reconciliation behavior.
- [ ] Approves tax-inclusive/exclusive semantics, discount-before-tax order or a replacement, multiple-tax ordering/compounding, inclusive extraction formulas, jurisdictions/categories/source and exemption evidence.
- [ ] Approves discount forms/limits, override roles, independent approver rule and missing-rate outcome.
- [ ] Uses only attributable Finance/Tax decisions; actual values/rates are not invented or bundled into this policy approval.
