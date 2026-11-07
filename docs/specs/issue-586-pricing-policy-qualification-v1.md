# Issue #586 — Pricing Policy and CalculationProfile Qualification v1

**Status:** deterministic specification for future offline qualification; no runtime pricing engine is implemented or tested here.
**Policy source:** Finance decision record v4.
**Candidate under qualification:** `r2-commercial-v3`, version 3, SHA-256 `afd16fb8806508a7a288f004994f6f446c06c03c7223a9cd31b70884a1e5765e` (candidate only; exact digest acceptance pending).
**Fixture constraints:** every price, rate, tax percentage, exchange observation and date example below is synthetic. Nothing is a current commercial price, official FX observation, real tax rate or approved tax position.

## Deterministic mathematical vectors

Decimal context: precision 34 significant digits; `ROUND_HALF_UP`; `Emin=-999999`; `Emax=999999`; `clamp=0`; input is finite decimal string only. Currency output quantum `.01` for RUB/USD/EUR/CNY. No input is silently rounded. The accepted intermediate trap/signal semantics from profile v1 remain pinned.

| ID | Input / scenario | Expected deterministic result |
|---|---|---|
| V1 | Inclusive gross `120`, one synthetic included tax `0.20`. | Base `100`, included tax `20`, diagnostic residual `0`, equation validates; no pre-discount tax added again after discount. |
| V2 | Inclusive gross `1`; compound rates `0.07`, then `0.03` on base + first tax. | Center base `0.9073586788857635423282823700208692`; tax1 `0.06351510752200344796297976590146084`; tax2 `0.02912621359223300970873786407766990`; diagnostic residual `1E-34`; recomposed outward interval `[0.9999999999999999999999999999999999, 1.000000000000000000000000000000001]`; both endpoint quantizations are `1.00`. Pass if no other issue. Residual remains diagnostic only. |
| V3 | Same compound formula but inclusive gross `1.005`. | Recomposition interval `[1.004999999999999999999999999999999, 1.005000000000000000000000000000001]`; endpoint output quantizations are `1.00` and `1.01`; block `TAX_RULE_UNRESOLVED` because final monetary result is not unique. |
| V4 | Inclusive gross `120`, rate `0.20`, percentage discount `0.10`. | Extract base `100`; discount `10`; taxable base `90`; recomputed tax `18`; `net_source=108`; diagnostic residual does not affect result. |
| V5 | Exclusive base `100`, rate `0.20`, no discount. | Tax `20`; `net_source=120`. |
| V6 | Exclusive base `100`; non-compound rates `0.10` and `0.05`. | Taxes `10`,`5`; total `115`. |
| V7 | Exclusive base `100`; rate1 `0.10`; rate2 `0.05` compounded on base + tax1. | Tax1 `10`; tax2 `5.5`; total `115.5`. |
| V8 | A line mixes inclusive and exclusive tax components without a separately approved complete formula. | Block `TAX_RULE_UNRESOLVED`; no Estimate total. |
| V9 | Base `0`, discount `0`. | `NO_DISCOUNT`; no additional discount approval. |
| V10 | Base `100`, percentage discount exactly `0.10`. | Equivalent discount is 10%; authorized manager approval required. |
| V11 | Base `100`, percentage discount exactly `0.20`. | Finance Owner approval required. |
| V12 | Base `100`, percentage discount `0.200001`. | Prohibited under standard policy; no approval or Estimate issuance. Error-code mapping is not added by this decision. |
| V13 | Base `0`, positive fixed discount `0.01`. | `DISCOUNT_POLICY_UNRESOLVED`; no division by zero. |
| V14 | Base `100`, fixed discount `10`. | Equivalent percentage `0.10`; manager approval required; taxable base `90`. |
| V15 | Discount exceeds pre-tax base. | `DISCOUNT_POLICY_UNRESOLVED`. |
| V16 | Negative discount. | `ESTIMATE_INPUT_INVALID`. |
| V17 | Approved synthetic FX observation: 900 RUB for nominal 10 USD. | Normalize to `90 RUB/USD`. This tests nominal handling only. |
| V18 | Convert `10 USD` using V17. | `900 RUB`; then one final `.01` line quantization. |
| V19 | Synthetic same-date official values: `90 RUB/USD`, `100 RUB/EUR`; convert USD→EUR. | `(90 RUB/USD)/(100 RUB/EUR) = 0.9 EUR/USD`; no intermediate money quantization. |
| V20 | Sunday business quote date; latest official effective date not later than quote is synthetic Friday. A newer downloaded record has a later effective date. | Select Friday by official effective date; ignore downloaded timestamp. All cross-rate components must use the selected same effective date. |
| V21 | A required observation is missing, ambiguous, or a cross-rate component does not match the selected effective date. | `FX_RATE_UNAVAILABLE`; no online lookup or rate fallback. |
| V22 | Final line amount `1.005`, `1.004999`, `1.005001`. | Quantized amounts `1.01`, `1.00`, `1.01` respectively under HALF_UP. |
| V23 | Input has 34 significant digits and meets the exact source scale policy; compare a 35-significant-digit input. | 34-digit input may proceed; 35-digit input is rejected `ESTIMATE_INPUT_INVALID`; never truncate/round it. |
| V24 | Input decimal string violates the exact source-specific versioned scale policy. | `ESTIMATE_INPUT_INVALID`; do not coerce or round to fit. |
| V25 | Arithmetic operation traps range/underflow/subnormal or output coefficient exceeds approved range. | `CALCULATION_RANGE_EXCEEDED`; no partial monetary result. |
| V26 | Same exact approved source/policy/profile snapshot and canonical input replayed twice. | Byte-identical result and canonical digest. A different source/profile digest is a different calculation identity. |
| V27 | Repeat command with same idempotency identity and same request digest; then reuse identity with another digest. | First retry returns same persisted receipt; second conflicts and creates no second Estimate/approval. |
| V28 | Source is explicitly superseded/revoked after an Estimate was created. | No new calculation uses it. Historical Estimate remains immutable. Affected undelivered Estimate requires successor calculation and exact-version reapproval. |
| V29 | Two approvals race on the same expected Estimate version and digest. | At most one CAS commits. The stale competing command leaves no approval/audit/outbox partial effect. (Future PostgreSQL test; not run.) |
| V30 | Revocation races with Estimate approval. | If revocation commits first, approval fails currentness. If approval commits first, historical artifact stays immutable and later revocation triggers the approved undelivered disposition. (Future PostgreSQL test; not run.) |
| V31 | Discount/override/source-publication/Estimate requester attempts self-approval using same canonical actor identity under another role label. | Deny; role-name difference does not bypass identity segregation. (Future authorization test; not run.) |
| V32 | Request in workspace A attempts to resolve/read PriceItem in workspace B or denied `AccessPolicy` source. | Non-disclosing denial/not-found; no price/source existence or value leaks. (Future tenant-isolation test; not run.) |
| V33 | Duplicate stable PriceItem identity or ambiguous row mapping/similar label. | Data Validation fails; no active PricingBasis; no LLM/name-based merge. |
| V34 | Import/sync receives new source content but no new Finance approval/Data validation. | New SourceRevision is evidence only; it cannot automatically supersede/activate pricing authority. |
| V35 | Display subtotal `10.005`, tax component `.005`, displayed discount `0`, final exact line amount `10.010`. | Displayed subtotal `10.01`, displayed tax `.01`, final line `10.01`, display reconciliation `-0.01`; for two signed components bound `(2+1)×.01/2=.015`, so pass. Extraction residual is not in this equation. |

## Interval algorithm acceptance

Use exact inputs as Decimal point intervals. For each operation, compute lower endpoints with `ROUND_FLOOR` and upper endpoints with `ROUND_CEILING` under the pinned precision/exponent/trap context. Addition and subtraction use directed endpoint bounds; multiplication evaluates four endpoint products and takes outward extrema; division evaluates endpoint quotients and blocks if its divisor interval contains zero. The interval trace is validation evidence, not the commercial result.

For inclusive tax, derive the exact approved component recurrence and require the input gross point to be inside the outward-rounded recomposition interval. For the final amount, propagate the validated interval through discount, post-discount tax and the exact authorized FX conversion. Quantize both output endpoints with the approved currency quantum and `ROUND_HALF_UP`; if the outputs differ, fail closed with `TAX_RULE_UNRESOLVED`. If they match, the separately computed HALF_UP Decimal center is known to produce that same final currency amount. Store the interval trace and extraction residual as diagnostic/audit data only. Never add or allocate the residual to a commercial amount.

Display reconciliation is calculated only after tax-equation and unique-output validation. It reconciles individually quantized display components to the final quantized line using the bounded formula pinned in the candidate. It cannot absorb the tax extraction residual or an unexplained formula error.

## Governance and persistence qualification

The future suite must also prove:

- **Authority activation:** Finance approval + Data Validation + current Security authorization are all required; source import, sync or row mapping alone never activates.
- **Scope and precedence:** exact workspace/environment, contract/service/project scope, half-open effective periods, no wildcard; equal-rank conflict fails `PRICE_CONFLICT`.
- **Revocation/currentness:** revoked source cannot be newly selected; no automatic reactivation; current `AccessPolicy` and PricingAuthority rechecked at commit.
- **Estimate lifecycle:** immutable successor, exact approval digest/version, new approval after material change, no source-price mutation.
- **Atomicity:** CAS of registry/run/artifact heads and required Audit Event/outbox intent share the canonical transaction; failed audit/outbox rolls back the protected transition.
- **Idempotency/recovery:** stable command identity; same digest replays; mismatch conflicts; lost commit response reconciles from canonical receipt.
- **Tenant and actor security:** two-workspace isolation, denied disclosure, canonical-actor non-self-approval, current access recheck for UI/API/workflow/background operations.
- **Workflow boundary:** approval wait and decision use the same R2 WorkItem/WorkflowRun; no second workflow engine.

These PostgreSQL/security/workflow cases are required future qualification. They have not run and are not represented by the synthetic Decimal checks.
