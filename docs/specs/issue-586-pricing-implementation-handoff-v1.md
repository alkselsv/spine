# Issue #586 — Pricing contracts and implementation handoff v1

**Status:** DESIGN PROPOSAL / NOT IMPLEMENTATION-APPROVED
**Decision basis:** [Issue #586 decision package v1](issue-586-r2-pricing-decision-package-v1.md)
**Parent:** [R2 map #584](https://github.com/alkselsv/spine/issues/584)
**Current main reverified:** `27fd8539284a2b1c5c6674662aef5cd402685ea3` (2026-11-07)

Separate decisions are recorded in the [Product Owner record](issue-586-product-owner-decision-v1.md), [Finance decision record v4](issue-586-finance-decision-record-v4.md), [Security record v2](issue-586-security-approval-record-v2.md), [Data record v2](issue-586-data-approval-record-v2.md), and [Architecture record v2](issue-586-architecture-acceptance-record-v2.md). Product Owner scope is accepted. Finance/Tax F1–F8 are ACCEPT and F9 ACCEPT_WITH_CHANGES against package v1.3, with subsequent Finance decisions F9-T1 and F10–F16 recorded; profile v3 digest approval remains pending. Data-1..4 and Security-1..4 are accepted. Architecture-1..3 principles are accepted, while exact ADR-0062 acceptance remains pending.

This handoff makes the proposed Option 2 reviewable as implementation contracts. It does not authorize schemas, database tables, migrations or runtime code before the role decisions and unresolved parameters listed here are approved. These are contract sketches; use existing canonical source, artifact, authorization, WorkflowRun and audit boundaries.

## 1. Target boundary

```text
SourceObject / exact SourceRevision
        + explicit authority approval and source scope
                    ↓
        approved PricingRuleSet / PriceItem
                    ↓ authorized deterministic resolution
             exact PricingBasis
                    ↓ pure Decimal CalculationProfile
         immutable Estimate + EstimateLines
                    ↓ exact-version review/decision
       existing WorkItem + one canonical WorkflowRun
```

- PostgreSQL remains canonical for Spine-owned pricing authority decisions and Estimate state. No independent pricing database/store.
- Source artifacts remain immutable `SourceObject` / `SourceRevision` evidence; price facts become eligible only through explicit pricing authority binding. A SourceRevision/current pointer does not itself mean approved price.
- Context Broker/Profile retrieves only access-authorized candidate evidence. It is not the authority resolver, does not grant price permission and cannot approve a price.
- A pure deterministic code step takes validated snapshots and returns a new typed artifact. It has no LLM arithmetic, provider call, implicit lookup or source mutation.
- Temporal runs within the existing R2 `WorkflowRun`. A human decision is a typed WorkflowCommand on that same run, with exact artifact version, trusted actor, optimistic concurrency and idempotency.

## 2. Proposed typed contract sketches

Names and fields are candidates for the detailed specification. IDs use established UUID/value conventions; all tenant-owned contracts carry `workspace_id` and `environment_id`; external references include exact version/revision and digest. Pydantic v2 versioned contracts should be frozen, `extra="forbid"`, finite Decimal, canonical-digest checked and explicit about schema version. Do not serialize Decimal through binary float.

### PricingRuleSet

Immutable approved selection/commercial policy snapshot, not a mutable price table.

```text
PricingRuleSet:v1
  rule_set_id, version, schema_version, digest
  workspace_id, environment_id
  service_class_set, project_class_set
  allowed_customer_contract_scope
  effective_from, effective_to_exclusive
  authority_reference, approval_decision_reference, approval_status
  source_selection_policy_version, precedence_profile
  price_source_refs[]                 # exact SourceObject + SourceRevision/version + digest
  currency_policy_ref, fx_policy_ref
  tax_policy_ref, discount_policy_ref, calculation_profile_ref
  predecessor_rule_set_ref?, created_at, created_by
```

`approval_status` is an immutable decision state/ref, not a caller-set boolean. `precedence` is defined in the versioned policy and cannot be ambiguous. The proposed four classes are contract-specific, class price book, workspace base, and separately approved override (manual case bypasses automatic source selection). A rule set is eligible only in its exact workspace/environment and scope.

### PriceItem (supporting contract)

```text
PriceItem:v1
  item_id, service_class, project_class?, source_item_key
  description_ref?                   # protected source reference, not authority
  unit_of_measure, unit_price: Decimal, currency
  tax_category_ref, price_inclusion: inclusive | exclusive
  effective_from, effective_to_exclusive
  source_revision_ref, locator_ref, source_value_digest
```

The item must bind to a stable source key and stable locator inside an exact revision, such as the governed Google Sheet business-row/cell reference. Row number/A1 alone is not stable identity. A missing unit, currency or item identity is not inferred from formatting or neighboring rows.

### PricingBasis

One resolved authority-bearing price fact for one EstimateLine; historical reference is a different type.

```text
PricingBasis:v1
  basis_id, workspace_id, environment_id
  authority_kind: contract_rate | class_price_book | workspace_base | approved_override
  source_object_id, source_revision_id, source_revision_digest
  source_version, source_locator, source_item_identity
  rule_set_id, rule_set_version, rule_set_digest
  authority_reference, approval_decision_reference
  service_class, project_class?, client_contract_scope?
  unit_of_measure, unit_price: Decimal, currency
  effective_from, effective_to_exclusive, pricing_as_of
  selection_policy_ref, precedence_rank, selection_result_digest
  access_decision_reference, provenance_refs[]
```

For an approved override, source reference can identify the prior official basis and the approved override decision is mandatory; the override is not inserted into the source book. `HistoricalEstimateReference` should only include evidence and comparison metadata, never `unit_price` in the active basis slot.

### CalculationProfile

```text
CalculationProfile:v1
  profile_id, version, digest, workspace_id, environment_id
  decimal_precision, input_scale_policy, overflow_policy
  currency_exponent_table_ref
  rounding_mode, line_quantization_stage, total_quantization_stage
  quantity_sign/zero policy, discount basis/order, tax inclusion/formula policy
  intermediate_precision, fx_conversion_stage, currency quantum policy
  engine_semver, canonicalization_version
```

Finance/Tax accepted precision 34 significant digits, `ROUND_HALF_UP`, exponent bounds and the listed Decimal semantics in F5. The original v1 profile remains immutable. Successor candidate `r2-commercial-v3` and digest are recorded in `issue-586-finance-decision-record-v4.md`; F9-T1 is accepted as diagnostic-only residual with directed interval validation, while the exact v3 JSON digest remains unapproved and inactive. A profile cannot omit a behavior that affects numeric output. Engine version identifies formula behavior; rule/tax/FX data versions identify input policy.

### EstimateLine

An immutable line in an Estimate artifact version.

```text
EstimateLine:v1
  line_id, requirement_ref, work_item_ref, description_ref
  quantity: Decimal, unit_of_measure
  pricing_basis_ref                 # exact PricingBasis ID/version/digest
  unit_price: Decimal, currency
  fx_basis_ref?                     # exact rate record if line conversion occurs
  discount_ref?, discount_amount: Decimal
  taxable_base: Decimal, tax_basis_ref, tax_amount: Decimal
  gross_amount, net_amount, line_total: Decimal
  calculation_profile_ref, calculation_trace_ref
  review_state, unresolved_issue_refs[]
```

Line currency semantics and whether source and estimate currencies coexist must be explicit. Calculated values are derived snapshots; they cannot diverge from recalculation with pinned input digest/version.

### Estimate

An immutable typed artifact carried by the existing artifact envelope and linked to one R2 WorkItem/WorkflowRun.

```text
Estimate:v1
  estimate_id, schema_version, artifact_id, artifact_version
  workspace_id, environment_id, work_item_id, workflow_run_id
  predecessor_estimate_ref?, supersedes_estimate_ref?
  pricing_as_of, output_currency
  rule_set_ref, calculation_profile_ref, engine_version
  lines[], subtotal, discount_total, tax_total, total
  estimate_state: draft | calculated | pending_review | approved |
                 rejected | requires_recalculation | superseded
  unresolved_pricing_issues[]
  exact_source_and_policy_provenance[]
  review_decision_ref?, approval_decision_ref?
  idempotency_identity, created_by, created_at
```

`approval_decision_ref` is populated only by an authorized decision that binds the exact version/digest. The estimate does not mutate to replace its contents. A recalculation creates an immutable successor. `approved` is a derived/decision-bound lifecycle state and cannot be asserted by input data.

### FXBasis, TaxBasis, DiscountDecision and EstimateReviewDecision

These are separate versioned records, referenced by exact identity/version/digest:

- `FXBasis`: exact pair, direction, Decimal rate, as-of instant/date, source identity/revision, source authority and approval, policy version, precision/conversion stage.
- `TaxBasis`: jurisdiction, service tax category, rate/regime, inclusive/exclusive behavior, effective period, source revision/version, authority decision and tax formula policy.
- `DiscountDecision`: policy version, scope, type/value/cap, proposed/applied amount, actor/reason/evidence, prior basis, independent approval and exact estimate predecessor/successor.
- `EstimateReviewDecision`: exact estimate artifact version/digest, actor and approved role, decision (`approve`, `reject`, `request_recalculation`), reason, policy snapshot, source/currentness validation references, timestamp, workflow command/idempotency identity.

## 3. Deterministic resolution and calculation algorithms

### Basis resolution (proposed)

```text
resolve(request, trusted_context):
  validate server-derived workspace/environment/actor/purpose
  resolve only source candidates admitted by current AccessPolicy and permitted purpose
  filter by exact workspace/environment, item/service, class/customer scope,
    pricing_as_of interval, approved/current authority decision, source revision,
    unit, currency, and all mandatory policy dimensions
  rank eligible official sources using the pinned rule-set policy:
    exact customer+service contract > class rule set > workspace base
    within class tier: exact service+project > exact service-only > exact project-only
  treat missing class fields as non-matches, never as wildcards
  if none: PRICE_NOT_FOUND (or stale/revoked typed reason where known safely)
  if equal top rank has non-equivalent candidates: PRICE_CONFLICT
  if equal candidates are equivalent only under an approved equivalence rule,
    resolve through that exact rule and preserve every candidate reference
  return one immutable PricingBasis with exact input digest and authorization refs
```

Authorization denial is resolved before protected candidate disclosure and must not reveal hidden candidate counts or identifiers. Manual override is not in `rank`: it requires a separate WorkflowCommand and independent approval decision, then becomes an explicit basis for a successor Estimate calculation.

### Calculator (proposed)

```text
calculate(estimate_input_snapshot, approved_profile):
  validate all Decimal values are finite and all refs/digests/units/currencies match
  for each line in stable input order:
    gross_exact = quantity * unit_price
    tax_mode = resolve_exact_tax_mode_and_ordered_components(exact_tax_basis)
    base_before_discount = gross_exact if exclusive else extract_inclusive_base(gross_exact, exact_tax_basis)
    discount_exact = apply_one_approved_discount(base_before_discount, approved_policy)
    taxable_base = base_before_discount - discount_exact
    tax_components = calculate_exact_components(taxable_base, exact_tax_basis)
    net_source = taxable_base + sum(tax_components)
    converted_line = apply_exact_FX_after_tax(net_source, exact FXBasis)
    line_total = quantize_once(converted_line, output_currency_quantum, output_rounding)
    displayed_components = quantize_each_converted_line_component_once(...)
    line_rounding_adjustment = line_total - reconciled_displayed_components
  Estimate.total = exact_sum(line_total in stable line identity order) # no second rounding
  summary components = exact_sum(per-line rounded display components)
  rounding_adjustment = exact_sum(line_rounding_adjustment)
  enforce reconciliation identity and quantization-derived adjustment bound
  emit Estimate successor, exact provenance, calculation trace and outcome
```

This describes the original profile-v1 proposal, not the current Finance status. F5 subsequently accepted precision 34 and `ROUND_HALF_UP`; F1–F8 are recorded ACCEPT and F9 ACCEPT_WITH_CHANGES. The successor profile v3 is a candidate only; its exact digest has not been approved and source/configuration bindings remain. See the [Finance decision record v4](issue-586-finance-decision-record-v4.md), the [historical Finance checklist](issue-586-finance-approval-checklist-v1.md), and the immutable [profile v1](issue-586-calculation-profile-candidate-v1.json).

### Idempotency identity

Canonical command identity binds workspace, environment, run, command kind, expected run/artifact version, input artifact digests, source/rule/FX/tax/discount/profile versions, engine version and caller idempotency key. Same key with same canonical payload returns the original receipt/result. Same key with a different payload is a conflict. Recalculation after an input, policy, source or engine version changes has a new identity and creates a successor artifact. Existing command-digest rules for finite Decimal and canonical values should be reused/extended, not replaced.

## 4. Context Profile and source authorization

Candidate `proposal.pricing.v1` is a versioned Context Profile identity for a purpose-scoped, bounded retrieval of approved pricing-source evidence. Proposed restrictions:

- exact workspace/environment, approved source classes and explicit rule-set reference;
- only current eligible candidate evidence, stable source/item locators and required provenance fields;
- bounded retrieval strategy and result count; no historical analogue payload in active-price result;
- freshness policy describes source-approved effective-date/currentness checks, not a global ContextProfile TTL substitute;
- no output of inaccessible source existence or content; no LLM-generated price becomes a result;
- profile is immutable, digest-bound and server-approved. It narrows broker retrieval after authorization and never grants `read_content`, `process_content`, manager override or Finance approval.

Implementation should call the existing authorized Context Broker pathway and exact source current authorization. `AccessPolicy` is evaluated per source/revision and derived evidence; pricing policy separately authorizes business operations. Cognee datasets/node sets/profile allowlists are not security authority. A service principal's authority must be explicit and operation/purpose-scoped.

## 5. Approval and audit boundary

Use one canonical R2 `WorkItem` and `WorkflowRun` for request, estimates, clarification, rework and final proposal review. Add typed WorkflowCommands such as `calculate_estimate`, `request_price_override`, `decide_price_override`, `submit_estimate_for_review`, `approve_estimate`, `reject_estimate`, and `request_estimate_recalculation` only after command ownership and permission checks are specified. Expected run version and exact artifact version/digest provide optimistic concurrency. Temporal handles durable waits/retries; it does not become canonical authority store.

Record decisions through canonical AuditEvent contracts and the existing transactional outbox when a cross-component notification is needed. Audit fields should include opaque exact object/decision refs, actor, role-scoped authority ref, policy/digest, reason code, outcome, source revision/version refs and command/correlation identity. Keep customer details, price sheet values and raw source excerpts out of disclosure-safe diagnostics. No mutable `approved=true` flag, separate workflow, duplicate audit DB or direct price-book write path.

Reapproval gate: any changed unit price, quantity, unit, scope, PricingBasis, currency/FX, tax basis/rate, discount, calculation profile/engine, pricing rule set or source approval/currentness check creates a successor version and requires review. A purely presentational edit may be classified non-material only by an approved policy; it still creates an artifact version and exact diff. Prior decision continues to identify only its prior version.

## 6. Compatibility map

| Proposed need | Existing reusable contract | Change needed |
|---|---|---|
| Exact source revision/provenance | `SourceObject`, `SourceRevision`, `SourceRevisionProvenance`, canonical acceptance and stable locators | Price-specific authority/approval mapping and structured price item identity; do not alter source identity semantics. |
| Governed sheet source | Google source contracts/connector (#598) and row association handoff (#613) | Source row links do not establish price authority; add policy-qualified price mapping. |
| Authorization | Trusted authorization context, current `AccessPolicy`, authorized Context Broker | Add business permission policy for pricing operations, distinct from source content permission. |
| Retrieval policy | `ContextProfileVersion`/`ContextProfileRef`, `ContextBundle`, authorized broker | Add approved R2 profile/version; enforce source policy separately. |
| Work and durable execution | `WorkItem`, PostgreSQL canonical `WorkflowRun`, typed `WorkflowCommand`, Temporal/outbox composition | Add Estimate command/event states to existing run, no second workflow. |
| Artifact versions | Generic `Artifact`, schema references, upstream artifact IDs, RequirementsArtifact versioning/acceptance precedent | New typed Estimate/PricingBasis and immutable version/approval persistence; verify transaction contract. |
| Audit/idempotency | AuditEvent registry/writer; canonical audit vs diagnostic separation; command digest; outbox | Add event types/fields and replay behavior for calculation/override/review. |
| Retention | `RetentionApplicability` and source/artifact lineage | Only map pricing evidence into approved existing classes; retention is not freshness/authority. |
| Financial arithmetic | Decimal serialization/metrics exist but no commercial model | Implement calculator and Finance-approved policies from scratch; unrelated Decimal modes are not defaults. |

No direct accepted ADR conflict found. The [Architecture acceptance record](issue-586-architecture-acceptance-v1.md) recommends a new ADR before implementation approval to settle canonical ownership and activation of approved price facts/rule sets and the atomic CAS boundary joining current source authorization, exact Estimate version, approval, audit and idempotency. The ADR must not create another workflow or authority for source content. This is a proposed requirement, not an accepted ADR; do not amend existing ADRs here.

## 7. Future implementation decomposition (do not create issues in this task)

| Work package | Goal and dependencies | Inputs / ownership boundary | Expected result | Required tests |
|---|---|---|---|---|
| A. Pricing contracts | Establish versioned schemas after D1–D10 and parameters approved. | Approved authority, class taxonomy, Money/FX/tax/discount/profile fields; domain owns invariants. | Frozen contracts, canonical digests, refs and typed outcomes; no persistence assumptions. | Schema rejection, digest vectors, finite Decimal, scope/effective-date validation, no unapproved basis. |
| B. Price source/rule-set registration | Bind exact accepted source revisions and price items to approved immutable rule sets. | SourceRevision + Data Owner mapping + Finance approval evidence; reuse canonical source store. | Idempotent append-only approved source/rule-set versions; no parallel store. | Revision replay, conflict, supersession/revocation, tenant isolation, missing identity/UOM/currency. |
| C. Exact PricingBasis resolver | Resolve only eligible authorized sources by pinned precedence. | PricingRuleSet, trusted context, current AccessPolicy, source revision, date/contract/class input. | One basis or typed fail-closed result; no call to LLM. | All precedence tiers, candidate order permutation, same-rank conflict, denied candidate leakage, stale/revoked/unapproved, no historical fallback. |
| D. Deterministic Decimal calculator | Pure code-step from exact validated basis and policy snapshots. | PricingBasis, CalculationProfile, FX/Tax/Discount policy versions. | Calculation trace and immutable calculated Estimate candidate. | Exact golden vectors, quantization boundaries, half-way cases, large/small limits, FX/tax/discount order, repeatability, no float. |
| E. Estimate artifact/version persistence | Persist line/estimate artifacts in existing PostgreSQL artifact/run tenancy and lineage. | Typed Estimate schemas and current artifact envelope; source + calculation provenance. | Append-only versions with predecessor, stable idempotent identity and protected readback. | Workspace/environment isolation, stale version CAS, replay, partial failure/outbox atomicity, no in-place update. |
| F. Override and reapproval commands | Add human commands/decisions to same R2 WorkflowRun. | Approved role policy, override reason/limits, exact Estimate version and run version. | Immutable override decision and exact-version review; no price-book mutation. | Permission denial, independent approval policy, duplicate key, payload mismatch, rejection, new version and approval invalidation. |
| G. Audit/idempotency integration | Link canonical audit and outbox for pricing decisions. | Existing AuditWriter, command digest and outbox ports. | Content-safe attributed decisions and replay receipts. | Stable replay, retry after partial failure, one logical event/effect, no sensitive values in diagnostics. |
| H. Context Broker integration | Resolve pricing evidence via an approved versioned Profile after authorization. | Pricing Profile, AccessPolicy, Context Broker and exact SourceRevision refs. | Authorized bounded source snapshot to resolver; no price authority in profile. | Wrong tenant, revoked source/policy, stale profile, denied source, hidden existence, stale projection, profile narrows only. |
| I. Synthetic deterministic qualification | Qualify full resolver → calculator → Estimate → review path offline. | Synthetic price books/contracts only; approved policy fixture versions. | Reproducible metric-free conformance report and regression fixtures. | Full decision table; no production/corporate values, network/LLM; exact provenance and role-specific approval. |

Dependencies: A precedes B–I; B precedes C; approved D4–D7 parameters precede financial calculator qualification; C and D precede E; E plus approved D7/D9/D10 role maps precede F; G/H integrate at their existing seams; I is a release gate after the component contracts. UI editor/diff and export remain separate full-R2 work as scoped by #584. No child issues are created here.

## 8. Synthetic qualification acceptance criteria

1. Same input bytes, exact source/policy digests, command identity and engine version always reproduce the same Estimate output and digest.
2. Source candidate permutations do not change selected basis; equal top rank conflicts without an approved equivalence/authority decision.
3. Historical estimate can be retrieved as an analogue but cannot satisfy a PricingBasis ref or calculator input validation.
4. LLM output containing any unit price/total is ignored as authority; calculator accepts typed Decimal basis only.
5. Every output line points to a stable price item locator in exact SourceRevision, including source revision digest, rule-set digest and approval ref.
6. Effective period start/end, superseded and revoked source, stale approval, missing unit/currency, and current authorization are covered.
7. Missing FX/tax/discount authority yields typed block; no request reaches an external provider from calculator.
8. Decimal boundary vectors prove Finance-approved rounding order exactly; do not assert approximate float values.
9. Workspace/environment access denied, hidden-source non-disclosure and role denial are proven; two-tenant fixtures share no output or decision.
10. Same command/key replay has one logical effect; same key/different payload conflicts; crash/retry does not duplicate Estimate or approval.
11. Changed amount-bearing input/policy/basis creates a successor and cannot reuse the previous approval.
12. Override records actor, reason, prior/proposed versions, evidence and approval; source book stays unchanged; automatic approval is impossible.
13. Audit can identify exact actor/decision/policy/run/artifact lineage while diagnostics contain no protected prices or source text.
14. Source revocation after approval preserves immutable history and surfaces an explicit disposition, never rewrites the prior Estimate.

## 9. Implementation readiness gates

Implementation may be specified after each required role decision has an attributable reference, the open parameters that affect contract shape are resolved, and Architecture confirms the canonical transaction boundary. Production-ready claim additionally requires the deterministic synthetic suite, current authorization and tenancy contracts, idempotent audit/outbox handling, artifact successor/reapproval behavior, and a reviewable manager UI/API surface for the R2 slice. Actual tariff data, FX rates and tax values must enter only through approved governed sources; they do not belong in this handoff or synthetic fixture as factual claims.

**Decision state:** Product Owner scope for Option 2 and product-scoped D1–D10 principles is ACCEPTED against package v1.1 SHA-256 `73c04fc138dcb36c34fff57169085ddbd87ada19b15d8be31dc4eae54b4e8325`; Finance/Tax F1–F8 are recorded ACCEPT and F9 ACCEPT_WITH_CHANGES against package v1.3; the profile v3 exact digest and ADR-0062 text remain pending. Data and Security role policies are accepted; Architecture principles are accepted but ADR text is pending. See the [attributed Product Owner decision record](issue-586-product-owner-decision-v1.md), [Finance decision record v4](issue-586-finance-decision-record-v4.md), [Data v2](issue-586-data-approval-record-v2.md), [Security v2](issue-586-security-approval-record-v2.md), and [Architecture v2](issue-586-architecture-acceptance-record-v2.md). **Execution state:** no pricing engine, migration, table or runtime contract is implemented by this package.
