# Issue #586 — Finance/Tax pricing decision and CalculationProfile v2 (historical proposal)

> **Current decision record:** [Finance/Tax decision record v4](issue-586-finance-decision-record-v4.md). F1–F8 are ACCEPT and F9 ACCEPT_WITH_CHANGES; subsequent Finance/Tax decisions are recorded there. Profile v3 remains an unapproved candidate. This v2 checklist is retained as the original request/proposal and is not the current approval status.

**Historical proposal status:** SUPERSEDED AS THE ACTIVE FINANCE RECORD by v3; its candidate values were pending at publication time. See the current Finance/Tax decision record linked above.
**Decision package for this Finance request:** `issue-586-r2-pricing-decision-package-v1` v1.3, SHA-256 `ae8916b72b6f8c2ffb9811262ac484415efb44f074586a49d52563e868828041`.
**Accepted Product Owner basis:** package v1.1, SHA-256 `73c04fc138dcb36c34fff57169085ddbd87ada19b15d8be31dc4eae54b4e8325`; Product Owner approval remains limited to product scope.
**CalculationProfile candidate:** [`r2-commercial-v1` v1](issue-586-calculation-profile-candidate-v1.json), SHA-256 `8452636abbf6f7a653b593583b9cb835e5ebb2636be09d3a59f9bcddd2312724` (exact canonical UTF-8 JSON bytes).
**Decision record version:** v2. No Finance/Tax approval is asserted.

This document requests attributable Finance and, where a separate Tax authority exists, Tax decisions. Candidate values are not approved values. Security permissions, Data source mappings and Architecture acceptance remain separate decisions.

## 1. Finance authority and source precedence

### Proposed authority model

Only an explicit Finance authority decision over an immutable source revision/version may make a price fact eligible. The approval record binds: workspace/environment; authority owner and approver role reference; exact source object/revision/version/digest and price-item identity; permitted customer/contract, service/project class and currency/UOM scope; effective interval; policy/rule-set version; approval decision and time; supersession/revocation relationship. Source access, import success, a Google row association, an old Estimate or a model extraction is not approval.

Product Owner accepted the product ordering below. Finance must separately accept or change its use for financial facts:

1. **Contract-specific rate:** exact authorized client/contract + service/item + scope + currency/UOM + `pricing_as_of` inside the approved effective period.
2. **Service/project-class price book:** approved workspace/environment and class scope; specificity is exact service+project class, then exact service class, then exact project class. Missing scope fields never mean wildcard.
3. **Workspace base price book:** approved workspace/environment fallback for only the explicitly declared items/scope.
4. **Manual price:** never resolver fallback; a separate override request and approval under an approved policy, bound to an Estimate version.

Historical Estimates are analogue evidence only. They cannot be selected or promoted to a price basis because they are recent, similar or approved as Estimates.

### Source-specific Finance decisions

| Decision | Proposed rule | Required Finance response |
|---|---|---|
| Authority owner/mechanism | Finance names the accountable authority role and approval mechanism for each source class. Approval is immutable, attributable and binds exact revision/version/digest. | Identity/role authority reference; approving mechanism; required evidence; ACCEPT / CHANGE / REJECT. |
| Contract-specific rates | Require exact contract/client key, service/item, unit, currency, environment, effective dates and an explicit authorization to use its commercial terms for quotes. | Confirm scope and keying; state whether any contract term can override a lower-tier item or only the exact matched rate. No wildcard. |
| Class price books | Each immutable version names service/project class sets, items, scope, authority and effective interval. | Accept/change the Product Owner accepted specificity order; provide class taxonomy owner and allowed intersections. |
| Workspace base price book | Only explicit workspace/environment fallback scope; it cannot cross tenant or replace a more-specific applicable rate. | Confirm use, authority owner and allowed coverage. |
| Same-rank candidates | No arbitrary winner by file order, update time or retrieval score. Return `PRICE_CONFLICT` unless an approved, versioned equivalence/tie rule proves candidates equivalent or an authority decision supersedes one. | Decide equivalence dimensions and evidence, if any; otherwise ACCEPT fail-closed. No per-estimate silent tie-break. |
| Effective periods | Half-open `[effective_from, effective_to)` timestamps in an approved timezone/normalization policy; `pricing_as_of` is the business quote date from the typed command. Do not infer dates from file/sync metadata. | Confirm interval boundary/timezone and authoritative quote-date field; define source-specific validity policy, not a universal TTL. |
| Supersession/revocation | New approved version supersedes only as stated in its decision; revoked/superseded version is ineligible for new calculation. Preserve pinned history. | Define revocation authority/effective event and required disposition of already-approved but unissued Estimates. No in-place rewrite. |
| Manual override | Separate, reasoned, immutable override decision. It changes only a successor Estimate; never edits the price book. Finance defines acceptable business authority and financial bounds. | Define eligible requester/approver role classes and approval threshold policy; exact identities/permissions are not supplied here. Security separately grants permissions. |

**Required Finance outcome:** accept or change each row above, naming the responsible Finance authority and exact policy parameters. Finance read access alone does not authorize approval.

## 2. One proposed CalculationProfile

The single candidate is `r2-commercial-v1`, version 1, in the linked JSON file. The JSON is the profile payload; its digest is SHA-256 of the exact UTF-8 bytes. The Finance decision must ACCEPT it or publish a changed successor profile and digest. The profile cannot activate while any required policy reference below is null.

### 2.1 Inputs, precision and canonical identity

1. Inputs are exact finite `Decimal` values encoded as decimal strings: quantity, unit price, discount values, tax rates and FX rate. Reject binary floats, NaN, infinities, locale-parsed strings and implicit conversion.
2. Candidate accepts positive quantity and finite nonnegative unit price; zero/negative quantity or negative price is `ESTIMATE_INPUT_INVALID` unless an approved successor policy explicitly supports credit lines. Validate exact PriceItem identity, compatible UOM, source currency, Estimate currency and exact approved PricingBasis, discount/tax/FX authority references. Missing/ambiguous quantity, unit, currency, applicability or approved rule blocks; never assume a default.
3. Candidate Decimal context: **34 significant digits**, `ROUND_HALF_UP`, `Emin=-999999`, `Emax=999999`, `clamp=0`; traps for `InvalidOperation`, `DivisionByZero`, `Overflow`, `Underflow`, `Subnormal`, `Clamped` and `FloatOperation`. Candidate permits `Inexact`/`Rounded` signals and rounds such intermediate operations at the declared context; they are not range errors. Finance must ACCEPT/CHANGE precision, context rounding, exponent bounds/clamp, trap/signal set and permitted input scales. Currency quantization is distinct from context precision. Output quantization must preflight the rounded integer coefficient width before emitting a result; if the final coefficient exceeds 34 digits, return `CALCULATION_RANGE_EXCEEDED`. For quantum `.01`, a 32-digit integer plus two fractional places is the candidate maximum. Finance must approve/change this bound and preflight rule.
4. Candidate currency identity is explicit ISO currency code. Resolve the versioned approved quantum for each currency; never assume 2 fractional digits. Missing/unsupported currency or quantum is `CURRENCY_POLICY_UNRESOLVED`.
5. Canonical numeric serialization uses fixed-point base-10 strings, strips insignificant trailing zeros, represents negative and positive zero as `0`, and forbids exponent notation. Canonical input digest covers schema/version, trusted tenant/environment, sorted stable line IDs, exact decimal strings, all source/policy/profile digests and date fields; keys lexicographic, tax components ordered by `tax_order` then stable tax ID. SHA-256 over UTF-8 canonical bytes. Approval metadata is outside the profile payload digest and is separately pinned.

### 2.2 Exact operation order and equations

**Operation 1 — resolve line and source price.** Select one exact approved PricingBasis under the approved source precedence. Require `quantity` in Estimate UOM and `unit_price` in PricingBasis currency per unit. Validate current authorization and source approval at calculation time. Missing, stale, conflicting, revoked or unauthorized basis returns the existing typed outcome (`PRICE_NOT_FOUND`, `PRICE_SOURCE_STALE`, `PRICE_CONFLICT`, `PRICE_BASIS_REVOKED`, `PRICE_ACCESS_DENIED`) and leaves the line unresolved.

**Operation 2 — gross source amount.**

`gross_source = quantity × unit_price`

This is the unrounded line price in source currency, before discount and tax.

**Operation 3 — resolve tax mode and pre-discount base.** Tax mode and complete ordered component set are required before discount calculation. This order is deliberate: for tax-inclusive input, extracting the pre-tax base is necessary to apply the candidate discount-before-tax rule consistently.

- For a wholly tax-exclusive line: `base_before_discount = gross_source`.
- For a wholly tax-inclusive line, compute `base_before_discount` by solving the exact approved extraction formula for the declared inclusive tax components. For each ordered component `i`, the approved formula defines `tax_i = rate_i × (base + sum(named prior included tax components))`. Then `gross_source = base + sum(all included tax_i)`. Components must be linear in `base` under this profile; resolve with Decimal context and retain the exact context result. Re-evaluate the equation under the same context and require the result to equal `gross_source`; otherwise block `TAX_RULE_UNRESOLVED`. Do not allocate any source-currency residual to a tax or base component.
- If components mix inclusive and exclusive modes, categories disagree with PriceItem metadata, jurisdiction is unsupported, order is duplicated, or an exemption cannot be established, block. This v1 candidate does not define mixed-mode algebra.

For one non-compound inclusive tax at rate `r`: `base_before_discount = gross_source / (1 + r)` and `included_tax_before_discount = gross_source − base_before_discount` (equivalently `base × r`). For multiple non-compound inclusive components with rates `r_i`: `base = gross_source / (1 + Σr_i)`; each component is `base × r_i`. For ordered compound components, use the exact approved component recurrence above; example below.

**Operation 4 — apply one discount before tax.** No automatic stacking. Discount is measured against the pre-discount tax-exclusive base, even if the source unit price is tax-inclusive. A percentage is a finite fraction in `[0,1]` applied to that base; a fixed discount is a source-currency amount against that base. Finance may change allowed forms or policy. Require `0 ≤ discount_source ≤ base_before_discount`; otherwise `DISCOUNT_POLICY_UNRESOLVED`. Define:

`taxable_base = base_before_discount − discount_source`

An explicit approved “no-discount” decision means zero. Missing/competing discount policy is not silently interpreted as zero; return `DISCOUNT_POLICY_UNRESOLVED` (proposed additional typed D8 outcome).

**Operation 5 — compute tax components.** For each component in explicit ascending `tax_order`, compute:

`tax_base_i = taxable_base + Σ(tax_j named by component i's approved compound-base references)`
`tax_i = tax_base_i × rate_i`

Non-compound components have no included prior-tax references and share the taxable base. A compound component explicitly names which earlier components enter its base. Do not infer “all previous” unless the approved rule says so. Tax rates, jurisdiction/category, period, inclusion mode, component order, base references, exemption and evidence come only from the exact approved TaxBasis. Duplicate order, missing rate/authority, incompatible category or unresolved exemption blocks as `TAX_RULE_UNRESOLVED`.

For exclusive prices every applicable component is added on top. For inclusive prices all applicable components must be included in the input price and the approved extraction formula must match the same component recurrence; after discount, recompute them from the discounted base. For both supported modes:

`net_source = taxable_base + Σ(tax_i)`

The inclusive identity is `gross_source = base_before_discount + Σ(included_tax_before_discount_i)` before discount, and the candidate requires exact equality under the pinned Decimal context or blocks `TAX_RULE_UNRESOLVED`; there is no residual allocation. After discount, `net_source = taxable_base + Σ(post-discount tax_i)`. No tax is added twice.

**Operation 6 — source-currency result.** Preserve exact contextual Decimal fields: `gross_source`, `base_before_discount`, `discount_source`, `taxable_base`, each pre-discount included-tax extraction value when applicable, each post-discount `tax_i`, `net_source`, currency and exact rule references. No currency quantum is applied at this stage.

**Operation 7 — FX.** If source and Estimate currencies differ, require one exact approved FX observation with source/target pair, direction, finite positive Decimal rate, as-of date/time, valid period, provider/source revision/version/digest and authority approval. Candidate direction is `estimate_currency units per 1 source_currency unit`:

`amount_estimate = amount_source × fx_rate`

Convert `net_source` and each reported component using the same pinned rate after all discounts/taxes and before output quantization. No intermediate quantization or online lookup. Missing, stale, unapproved or ambiguous rate returns `FX_RATE_UNAVAILABLE`.

**Operation 8 — line total and displayed components.** Quantize `amount_estimate` exactly once to the approved Estimate-currency quantum `Q` using the approved output mode (candidate `ROUND_HALF_UP`):

`line_total = quantize(net_source × fx_rate, Q, rounding_mode)`

For a same-currency line, `fx_rate = 1` by identity and no FX record is needed. For review display, convert components with the same rate, then quantize **each line component once** to `Q`: `display_subtotal = Q(base_before_discount)`, `display_discount = Q(discount_source)`, and `display_tax_i = Q(tax_i)`. Here subtotal/discount are pre-tax equivalents, not tax-inclusive sticker totals. Do not display `gross_source` as subtotal for inclusive price; show it separately as “source price, tax-inclusive” if authorized.

**Operation 9 — rounding reconciliation.** For each line:

`line_rounding_adjustment = line_total − (display_subtotal − display_discount + Σ(display_tax_i))`

This is computed, never user-entered. It represents only the difference caused by independently quantizing the line total and its displayed components. With `m` displayed nonnegative components including subtotal, discount and each tax, its absolute magnitude is bounded by `(m + 1) × Q/2` under nearest rounding; enforce this bound. If it exceeds the bound, reject the calculation as an arithmetic/profile invariant failure, not as a commercial adjustment.

**Operation 10 — Estimate totals and display.** Sort lines by stable `line_id`. `Estimate.total = Σ(line_total)` exactly; do not re-quantize. Each displayed Estimate component is the exact sum of its already quantized per-line displayed component. `Estimate.rounding_adjustment = Σ(line_rounding_adjustment)`. Thus:

`Estimate.total = displayed_subtotal − displayed_discount + Σ(displayed_tax_components) + Estimate.rounding_adjustment`

No aggregate-only extra quantization or unexplained balancing correction is allowed. Changing any input, approved source revision, authority/rule/policy reference, FX/tax/discount basis, profile digest, engine version or relevant authorization snapshot creates a successor Estimate and requires review of that exact version.

### 2.3 Terms and tax reconciliation

| Term | Exact meaning |
|---|---|
| `gross_source` | Quantity × unit price as supplied by the exact PriceItem, before discount/tax calculation; it is tax-exclusive or tax-inclusive according to the approved PriceItem/TaxBasis. |
| `base_before_discount` | Tax-exclusive base represented by `gross_source`; for inclusive prices it is extracted by the exact approved component formula. |
| `discount_source` | Approved discount amount measured against `base_before_discount`, in source currency; a fixed discount is explicitly pre-tax. |
| `taxable_base` | `base_before_discount − discount_source`. |
| `included_tax_before_discount` | Tax component values that algebraically compose the inclusive source gross before discount; retained for traceability only. |
| `tax_i` / `exclusive_tax` | Each post-discount component computed by the approved tax-base recurrence. In exclusive mode these are added on top; in inclusive mode these are recomputed within the discounted inclusive commercial amount. |
| `net_source` | `taxable_base + Σ(post-discount tax_i)` in source currency. |
| `displayed_subtotal` | Sum of per-line quantized `base_before_discount` values after FX; it excludes tax. |
| `displayed_discount` | Sum of per-line quantized pre-tax `discount_source` values after FX. |
| `displayed_tax_components` | Component-wise sum of per-line quantized post-discount tax values after FX. |
| `Estimate.total` | Exact sum of quantized Estimate-currency `line_total` values. |
| `rounding_adjustment` | Sum of formula-derived per-line display reconciliation deltas, within the stated quantization bound; never a price/tax override. |

**The v1 checklist's generic “inclusive_amount = gross − discount, then included-tax extraction” did not define whether the discount was inclusive or pre-tax and could make the displayed subtotal/discount/tax identity count embedded taxes inconsistently.** This v2 profile resolves that ambiguity: extract the pre-discount tax-exclusive base first; apply the discount once to that base; recompute taxes from the discounted base; reconcile using base-equivalent subtotal and discount. Inclusive tax is not added on top a second time.

## 3. Synthetic qualification vectors (no real tariff or jurisdiction)

All rates, amounts and FX values below are invented solely as mathematical fixtures. They are not market data, tax advice, or policy approval. Unless stated, use a two-decimal synthetic output quantum and candidate `ROUND_HALF_UP`.

| ID | Inputs and rule | Expected exact intermediates and result |
|---|---|---|
| V1 inclusive extraction | Quantity `1`, inclusive gross `110.00`, one non-compound included component `10%`, no discount. | `base_before_discount=100`; `included_tax_before_discount=10`; `taxable_base=100`; `tax=10`; `net_source=110`. Display `100.00 + 10.00 = 110.00`; adjustment `0.00`. |
| V2 exclusive tax | Quantity `1`, exclusive gross `100.00`, one non-compound component `10%`, no discount. | `base_before_discount=100`; `taxable_base=100`; `tax=10`; `net_source=110`; line total `110.00`. |
| V3 discount before tax | Exclusive gross `100.00`; percentage discount `0.10`; tax `10%`. | Discount `10`; taxable base `90`; tax `9`; net/line total `99.00`; display `100.00 − 10.00 + 9.00 = 99.00`. |
| V4 inclusive discount before tax | Inclusive gross `110.00`; included tax `10%`; percentage discount `0.10` on extracted pre-tax base. | Base before discount `100`; included tax before discount `10`; discount `10`; taxable base `90`; recomputed included tax `9`; net/line total `99.00`; display `100.00 − 10.00 + 9.00 = 99.00`. |
| V5 multiple non-compound inclusive taxes | Inclusive gross `115.00`; two included components, `10%` and `5%`, both base = taxable base, neither compounds. | Base `100`; taxes `10` and `5`; net `115.00`; no double addition. |
| V6 compound exclusive taxes | Exclusive gross `100`; component A `10%` on base; component B `5%` on base plus A (explicit compound ref). | A `10`; B `5.5`; net/line total `115.50`. |
| V7 FX after tax | Source EUR amount `10.00`, exclusive tax `10%`, synthetic rate `1.25 USD/EUR`, USD quantum `.01`. | Tax `1.00 EUR`; source net `11.00 EUR`; converted net `13.750 USD`; line total `13.75 USD`. FX is a fabricated fixture only. |
| V8 line-level half-up and total | Two independent lines each have exact Estimate-currency net `0.005`, quantum `.01`. | Each line quantizes to `.01`; `Estimate.total=.02` (sum lines; no second rounding). |
| V9 currency quantum | Synthetic currencies with quanta `.01`, `.001`, and `1`; exact amounts `1.235`, `1.2345`, and `2.5`. | Candidate HALF_UP results respectively `1.24`, `1.235`, `3`; the exponent/quantum must come from the approved currency table, not a two-digit default. |
| V10 bounded display adjustment | Exclusive gross `.044`, tax `10%`, same output currency, quantum `.01`. | Exact net `.0484` → line `.05`; displayed subtotal `.04`, discount `.00`, tax `.00`; adjustment `.01`; identity `.05=.04−.00+.00+.01`. Bound for 3 components is `.02`; passes. |
| V11 missing FX | Source currency differs from Estimate currency; no approved current pair. | `FX_RATE_UNAVAILABLE`; no output total and no approval. |
| V12 conflict / stale source | Two approved same-rank candidates without an approved equivalence rule / otherwise eligible source outside effective interval. | `PRICE_CONFLICT` / `PRICE_SOURCE_STALE`; no arbitrary winner or historic fallback. |
| V13 unsupported / missing tax | Jurisdiction not listed in approved supported set / supported jurisdiction has no valid rate for date/category. | `TAX_JURISDICTION_UNSUPPORTED` / `TAX_RULE_UNRESOLVED`; neither becomes zero-tax. |
| V14 mixed tax modes | A line has one inclusive and one exclusive component without an approved joint formula. | `TAX_RULE_UNRESOLVED`; no complete calculation. |
| V15 output range boundary | With precision 34 and quantum `.01`, quantize `99999999999999999999999999999999` (32 integer digits), then one digit larger `999999999999999999999999999999999`. | 32-digit value plus `.00` fits 34 significant digits; 33-digit value would produce a 35-digit quantized coefficient and preflight returns `CALCULATION_RANGE_EXCEEDED` before emission. `0.0000000000000000000000000000000001` quantizes to `.00`. |
| V16 fixed discount | Exclusive gross `100.00`; fixed pre-tax discount `3.00`; tax `10%`. | Discount `3`; base `97`; tax `9.70`; net/line total `106.70`. |
| V17 approved manual override | Exact original basis is `100.00`; a separate authorized override decision sets `105.00` for successor Estimate v2. | v1 basis/value/digest and its review record remain unchanged; v2 pins override decision and new input digest, starts unapproved/PENDING_REVIEW; absent approval, no APPROVED state. |
| V18 idempotent replay | Same canonical Estimate command, pinned source/profile/engine digests and idempotency key submitted twice. | Return same persisted Estimate version, result digest and command receipt; one logical audit/outbox effect. Same key with a different input digest returns conflict and changes no canonical state. |
| V19 profile/source successor | Recalculate V3 with same monetary inputs but a different approved CalculationProfile digest or SourceRevision digest. | Numerical amount may be equal (`99.00` for unchanged formulas) but calculation identity differs; create successor Estimate, preserve v1, require new exact-version review/approval. |
| V20 compound inclusive extraction | Inclusive gross `115.50`; component A `10%` on base; component B `5%` on base plus A; both included. | Solve `G = B + 0.10B + 0.05(B + 0.10B) = 1.155B`; `B=100`; included taxes are `10` and `5.50`; sum exactly to `115.50` before discount. |
| V21 invalid quantity/price | Candidate profile receives zero or negative quantity, or a negative unit price without an approved credit-line policy. | `ESTIMATE_INPUT_INVALID`; no line amount or approval. A credit-line profile must be an explicitly approved successor with its own exact formulas. |
| V22 Decimal exponent boundary | Under candidate bounds, multiply `1E+999999 × 10` / `1E-999999 × 0.1`. | Overflow / subnormal-underflow traps respectively map to `CALCULATION_RANGE_EXCEEDED`; no line result. |

Qualification suite also needs vectors for: exact replay of identical pinned input/profile/engine digests gives byte-identical output digest; any source revision, policy, FX, tax, discount, profile or engine version change changes calculation identity and creates a successor Estimate; old approval never transfers; identical idempotency key returns one persisted decision; conflicting same key fails; non-finite/float inputs and Decimal trap conditions fail closed; discount above base fails; rounding adjustment outside bound fails; workspace/environment mismatch and current authorization denial leak no source values.

## 4. FX and currency decisions

Finance must supply explicit ISO currency allowlists for PricingBasis/source and Estimate/output currencies; exact source of currency identity and quantum/exponent by version; one approved FX source class/provider and authority; accepted pair direction; observation identity/revision/digest; `as_of` field, period and staleness policy; whether same-currency uses identity rate; and acceptance/change of after-tax conversion before quantization. An online lookup, unversioned market page, stale cache or model answer never establishes authority. Missing/stale/ambiguous evidence is `FX_RATE_UNAVAILABLE`.

Actual FX observations can be onboarded later as governed, approved source data; the authority and interpretation policy above are required for #586.

## 5. Tax and jurisdiction decisions

Finance/Tax must identify the supported jurisdiction set and its authorized tax-rule source, effective-date policy, service-category mapping owner, per-item inclusive/exclusive metadata authority, component order and exact formulas, compound-base references, extraction formula for inclusive components, exemption/special-regime evidence and rounding/invoice-display consistency.

- **Unsupported jurisdiction:** outside the explicitly approved jurisdiction set; outcome `TAX_JURISDICTION_UNSUPPORTED`. No tax amount is calculated or presented as zero.
- **Supported jurisdiction, missing/expired/ambiguous data:** outcome `TAX_RULE_UNRESOLVED`; no zero-tax fallback.
- **Mixed inclusion mode or incompatible category:** unsupported by this candidate unless a Finance/Tax-approved successor profile explicitly defines the complete formula; otherwise `TAX_RULE_UNRESOLVED`.
- No country, jurisdiction, rate or tax category is inferred from repo location, user location, currency, source language or contract address by an LLM.

Actual tax rates and exemption records are governed data that may be populated after policy approval. Policy approval must decide who may publish them and how their exact revisions become current.

## 6. Discounts, overrides and approval separation

Candidate: at most one discount rule per line; stacking disabled. Percentage is a finite fraction over the pre-tax base; fixed amount is denominated in source currency and measured against the same pre-tax base. `0 ≤ discount ≤ base`. Finance must approve allowed forms, precision, caps/scopes, precedence against contract terms and explicit no-discount behavior. Until required discount policy is resolved, `DISCOUNT_POLICY_UNRESOLVED`; never silently apply zero or stack.

Finance must define financial authority classes for request and approval, monetary threshold policy (values remain unset), required reason/evidence, acceptable prior/proposed basis and amount references, independent approver constraint, effectivity and what requires reapproval. Proposed control: requester, source maintainer and override approver are distinct actors; approval binds exact current Estimate predecessor and proposed successor input digest. Any edit after decision creates a successor Estimate and invalidates prior approval for the new version. Estimate approval binds exact version. Finance policy does not itself grant application permissions; Security independently approves access and segregation enforcement. No override changes a price source.

## 7. Finance/Tax decision form — exact response required

Record one attributable answer per section against the exact package and candidate profile digests. Do not leave an ACCEPT without resolving every required value in the section.

| Section | Proposed decision | Alternative | Rationale / consequence | Required owner input | Response |
|---|---|---|---|---|---|
| F1 Authority and source classes | Finance-approved exact immutable sources; classes and scope as §1. | Change classes, authority owner or evidence requirements. | Establishes which financial facts can enter resolver. | Accountable role/person authority ref; approval mechanism; exact source evidence fields; classes. | ACCEPT / ACCEPT_WITH_CHANGES / REJECT: PENDING |
| F2 Precedence, conflicts and contract scope | Contract exact-match → class book with approved specificity → workspace base; same-rank conflict blocks absent explicit equivalence. | Change order/scope or define explicit deterministic tie rule. | Prevents arbitrary source choice and contractual mismatch. | Confirm source order; client/contract key and exact applicability; class vocabulary; equivalence dimensions/evidence. | ACCEPT / ACCEPT_WITH_CHANGES / REJECT: PENDING |
| F3 Effective dates, supersession and revocation | `[from,to)` under explicit timezone; no universal TTL; revoked/superseded source unavailable to new calculation; historical Estimate remains immutable. | Specify different boundary/currentness and disposition. | Makes `pricing_as_of` replayable and prevents stale use. | Date/timezone policy; authoritative quote date; validity rule per source; revocation event/owner; disposition of approved unissued Estimates. | ACCEPT / ACCEPT_WITH_CHANGES / REJECT: PENDING |
| F4 FX and currency | Explicit ISO allowlists/quantum table; exact one governed FX authority; after-tax conversion, before quantization. | Change allowed currencies, source, direction, stage or staleness. | Ensures repeatable output-currency amount. | Source/output currencies; provider/class and authority; pair/direction; observation fields; as-of/period/staleness; currency quantum authority; stage. | ACCEPT / ACCEPT_WITH_CHANGES / REJECT: PENDING |
| F5 Decimal profile and serialization | Candidate 34 significant digits, `Emin`/`Emax`/clamp; finite string inputs; positive quantity/nonnegative price; stated traps; `Inexact`/`Rounded` operations use context rounding; output coefficient is preflighted against range; `ROUND_HALF_UP`; fixed-point canonical serialization; line outputs once quantized; sum rounded lines. | Change precision, exponent range/clamp, signs, traps/signals, rounding or quantization/serialization. | Defines exact replay and digest identity. | Precision, `Emin`/`Emax`/clamp, context rounding, trap/signal set, input scale/signs, output rounding, quantum table and supported numeric range/preflight approval. | ACCEPT / ACCEPT_WITH_CHANGES / REJECT: PENDING |
| F6 Tax | Exact approved jurisdiction/category sources; same-mode components only; explicit order/base/extraction; unknown blocks. | Change jurisdiction model, formula, inclusion, compounding or mixed-mode support. | Avoids double-counted tax and unsupported regimes. | Jurisdictions, authority, categories, effective dates, inclusivity, component order/base refs, exemption proof, invoice/display policy. Tax authority ref if distinct from Finance. | ACCEPT / ACCEPT_WITH_CHANGES / REJECT: PENDING |
| F7 Discount/override | Single discount, base measured pre-tax, no stacking; separate reasoned override and approval; no source mutation. | Change types, stacking, base, limits, role separation or approval flow. | Keeps discount and override auditable and deterministic. | Types; percentage/fixed semantics; numeric limits and scopes; no-discount representation; requester/approver authority classes; thresholds; evidence; independence. | ACCEPT / ACCEPT_WITH_CHANGES / REJECT: PENDING |
| F8 Missing/unsupported/conflict behavior | Fail closed: typed price/FX/tax/discount/currency/range/input outcomes; no historic fallback. | Define a different explicit blocked/draft policy without silent defaults. | Avoids false totals or approval. | Accept `CURRENCY_POLICY_UNRESOLVED`, `CALCULATION_RANGE_EXCEEDED`, `ESTIMATE_INPUT_INVALID`, `DISCOUNT_POLICY_UNRESOLVED`, `TAX_JURISDICTION_UNSUPPORTED`, `TAX_RULE_UNRESOLVED` and existing D8 outcomes; draft/display behavior allowed to authorized roles. | ACCEPT / ACCEPT_WITH_CHANGES / REJECT: PENDING |
| F9 CalculationProfile | Accept candidate JSON v1 digest above or accept with a versioned successor. | Reject profile and supply exact replacement payload/digest. | Activates one deterministic math contract only after parameters resolved. | Exact successor ID/version/digest if changed; rationale. | ACCEPT / ACCEPT_WITH_CHANGES / REJECT: PENDING |

**Role-scoped attribution fields:** Finance/Tax decision `PENDING`; decision-maker name/identity `PENDING`; Finance/Tax role authority reference `PENDING`; decision time UTC `PENDING`; exact package v1.3 SHA-256 `ae8916b72b6f8c2ffb9811262ac484415efb44f074586a49d52563e868828041`; profile ID/version/digest `r2-commercial-v1` v1 / `8452636abbf6f7a653b593583b9cb835e5ebb2636be09d3a59f9bcddd2312724`; accepted changes and rationale `PENDING`. A person acting in another role must record a separate decision for that role.

## 8. Policy decisions now vs governed data later

**Required to close #586 / enable detailed implementation specification:** authority owner/mechanism; accepted source classes/order/scope/conflict rules; date/currentness/revocation semantics; currency allowlists and FX authority/stage/evidence/staleness; Decimal profile/rounding/quantization; jurisdiction/tax rules and unsupported-vs-missing behavior; discount types/limits/approver authority; override approval controls; immutable Estimate/reapproval and display reconciliation semantics; required error outcomes and lineage.

**Can be onboarded later after those policies exist:** actual price-book amounts and contract rates; individual approved FX observations; effective tax-rate observations, exemption evidence and actual price rows. Each still requires exact authorized source revision and approval. These facts are not guessed or included in this proposal.

**Not Finance decisions:** Security permission grants/segregation implementation (`issue-586-security-approval-record-v1`); Data stable item identity, row mapping and provenance acceptance (`issue-586-data-approval-record-v1`); Architecture canonical ownership, CAS/outbox/workflow/ADR acceptance (`issue-586-architecture-acceptance-v1`).

## 9. Ready-to-use Finance Owner decision comment template

This is a blank template for the Finance/Tax decision owner to complete and post. It is not an approval and must not be posted with unfilled or guessed values.

```markdown
## Finance/Tax decision — Issue #586

Decision: ACCEPT / ACCEPT_WITH_CHANGES / REJECT (complete separately for F1–F9 below)
Finance/Tax decision-maker and role: [name + role]
Finance/Tax authority reference: [delegation or accountable-role evidence]
Decision time: [UTC timestamp]
Exact decision package: v1.3, SHA-256 `ae8916b72b6f8c2ffb9811262ac484415efb44f074586a49d52563e868828041`
CalculationProfile candidate: `r2-commercial-v1` v1, SHA-256 `8452636abbf6f7a653b593583b9cb835e5ebb2636be09d3a59f9bcddd2312724`

| Section | Decision | Accepted values / exact changes | Rationale |
|---|---|---|---|
| F1 Authority/source classes | [ACCEPT / ACCEPT_WITH_CHANGES / REJECT] | [...] | [...] |
| F2 Precedence/conflicts/contract scope | [...] | [...] | [...] |
| F3 Effective dates/supersession/revocation | [...] | [...] | [...] |
| F4 Currency/FX | [...] | [...] | [...] |
| F5 Decimal/rounding/serialization/range | [...] | [...] | [...] |
| F6 Tax/jurisdiction/formulas | [...] | [...] | [...] |
| F7 Discount/override financial policy | [...] | [...] | [...] |
| F8 Missing/unsupported/conflict outcomes | [...] | [...] | [...] |
| F9 CalculationProfile payload | [...] | [candidate digest or successor profile ID/version/digest] | [...] |

Actual tariffs, individual FX observations and tax-rate data are not approved or populated by this policy decision. Security permission grants, Data source mappings and Architecture/ADR acceptance remain separate role-scoped decisions.
```

## 10. Finance acceptance criteria

- [ ] Attributable Finance/Tax authority and exact package/profile digests recorded.
- [ ] Each source class, scope, order, specificity, conflict/equivalence, effective period, supersession/revocation and override authority accepted or replaced.
- [ ] Source/Estimate currencies, sole FX authority, exact observation and after-tax stage/staleness approved; no online implicit lookup.
- [ ] Decimal precision/range/traps/input scales, rounding mode, currency quantum, line rounding, total sum and canonical digest rules approved.
- [ ] Inclusive/exclusive equations, discount order/base, multiple/compound component formula, unsupported/missing tax outcomes and invoice/display semantics approved.
- [ ] Discount form, limits, stacking, override role/approver rules and missing-policy outcomes approved, without substituting for Security permissions.
- [ ] Synthetic vectors V1–V22 and failure/replay/successor invariants accepted or replaced with deterministic expected values.
- [ ] Actual prices, FX observations and tax-rate data are explicitly excluded from this policy approval and will be onboarded via governed authority.
