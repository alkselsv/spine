# Issue #586 — R2 Pricing Authority Decision Package v1

**Status:** PARTIALLY APPROVED — PRODUCT OWNER SCOPE ONLY; FINANCE / SECURITY / DATA / ARCHITECTURE PENDING
**Current decision-package snapshot:** v1.3 (Finance/Tax qualification overlay; Product Owner scope from v1.1 remains unchanged)
**Product Owner decision basis:** package v1.1, SHA-256 `73c04fc138dcb36c34fff57169085ddbd87ada19b15d8be31dc4eae54b4e8325`; conversational ACCEPT at 2026-11-07 11:11:07 UTC, recorded in [Product Owner decision record](issue-586-product-owner-decision-v1.md).
**Decision scope:** R2 commercial proposal pricing authority and deterministic commercial rules
**Repository baseline at initial analysis:** `main` at `a272e982db26cb92170cd633c279c5857ab112f1`; **reverified current main:** `07caa391bcc221de1c61f099d775db7a0ce2ef2b` (2026-11-07). The intervening commit changes R1 answer-quality evaluation and does not touch pricing, source, workflow or approval contracts reviewed here.
**Issue:** [#586](https://github.com/alkselsv/spine/issues/586)
**Parent:** [#584](https://github.com/alkselsv/spine/issues/584)

Separate role-scoped decision records: [Product Owner](issue-586-product-owner-decision-v1.md), [Finance](issue-586-finance-approval-checklist-v1.md), [Security](issue-586-security-approval-record-v1.md), [Data](issue-586-data-approval-record-v1.md), and [Architecture](issue-586-architecture-acceptance-v1.md). Product Owner scope is accepted against the exact v1.1 digest above; all other role decisions remain pending.

This is a human decision package and decision-status record, not a runtime implementation. The Product Owner acceptance is limited to product direction/principles listed in the role record. Recommendations do not establish actual price authority, a user's role, a tax/FX rule, or implementation readiness. Finance, Security, Data and Architecture approvals must be separately attributable and role-scoped. A single person's approval in one role does not imply approval in another role. This v1.2 status overlay does not retroactively alter the accepted v1.1 decision text.

## 1. Decision requested

Select **Option 2: versioned rule-set selection by service/project class with explicit deterministic precedence**. A new estimate is calculated only from one currently approved, applicable rule set and exact authorized price facts. Every line retains its exact basis, policy and calculation lineage. Historical estimates may inform analogue retrieval and comparison but never become current prices by inference. LLMs may propose work composition or candidate basis identifiers; deterministic code validates all inputs and computes every monetary amount.

### Alternatives

| Option | Model | Tradeoff |
|---|---|---|
| 1 | One active approved price book per workspace/environment/effective period; conflicts fail closed. | Simple selection, but does not directly express service/project-class variation or customer-specific rates. |
| **2 (recommended)** | Versioned approved rule sets selected by workspace, environment, service/project class, scope and effective period with declared precedence. | Expresses the R2 pricing cases while making selection replayable; requires explicit policy and conflict handling. |
| 3 | Manager selects among approved sources per estimate with justification/audit. | Flexible, but puts source arbitration into each transaction and makes consistent deterministic selection harder. |

## 2. Proposed decision summary

| Decision | Product-scope outcome | Remaining role decision(s) |
|---|---|---|
| D1 Pricing authority | Product policy accepts the listed source classes and historical references as non-authoritative. | Finance authority owners/evidence; Data identity and provenance. |
| D2 Selection | Product precedence and fail-closed equal-rank conflict are accepted. | Finance approval of commercial authority/order and Data mapping. |
| D3 Freshness | `pricing_as_of` means the business quote date; source validity/currentness governs, with no global TTL. | Finance/Data exact date normalization, boundaries and revocation policy. |
| D4 Currency/FX | Explicit estimate currency and pinned FX basis are required. | Finance currency allowlist, FX authority, rate policy and conversion stage. |
| D5 Arithmetic | Decimal-only deterministic calculation; no LLM arithmetic. | Finance precision, traps, rounding, quantization and aggregation profile. |
| D6 Tax | Explicit approved tax basis; unresolved applicability blocks approval. | Finance/Tax jurisdiction, rates, formulas, inclusion, order and exemptions. |
| D7 Discounts/overrides | Manual override requires separate human approval; it is not a fallback or source mutation. | Finance limits/approver authority; Security permissions and separation. |
| D8 Exceptions | Typed fail-closed outcomes; no historical-price fallback. | Security disclosure and audit visibility; Finance/Data exception policy. |
| D9 Review | Immutable Estimate versions, exact-version reapproval and existing R2 WorkflowRun accepted. | Architecture CAS/atomic boundary; Finance/Security approval authority. |
| D10 Security/audit | Authority is distinct from access; exact lineage and attribution required. | Security permissions/isolation; Data provenance; Architecture ADR/atomicity. |

## 3. Decisions D1–D10

### D1 — Pricing authority

- **Question:** Which records can establish a current price for an EstimateLine?
- **Alternatives:** (a) any observed/historical estimate; (b) approved contract, class and workspace price sources plus exceptional approved override; (c) manager chooses any visible source per estimate.
- **Recommendation:** (b). An approval must identify the authority owner, exact immutable source revision/version and digest, workspace/environment and permitted scope. Source visibility, ingestion acceptance, link association, analogue relevance, or model extraction alone never grants pricing authority.
- **Normative rules:**
  1. `approved contract-specific rate`: Finance-designated contract authority approves exact customer/contract, service/item, currency/unit and effective period. It outranks lower tiers only inside that explicit scope.
  2. `approved service/project-class price book`: Finance-designated authority approves an immutable version scoped to workspace/environment and declared service/project class. Within this tier the proposed specificity order is exact service+project class, then exact service class, then exact project class. A missing class field is not a wildcard.
  3. `approved workspace base price book`: Finance-designated authority approves the workspace/environment fallback scope.
  4. `manual override`: a separate, reasoned decision by a permitted manager and a separately authorized approver; it is not automatic fallback and does not mutate its source price book.
  5. `historical estimate`: read-only analogue/reference only; cannot be promoted to a PricingBasis by recency, similarity, model output or manager convenience. Promotion requires a new explicit approved source record through the authority process.
- **Parameters to approve:** authority-owner identity/role mapping; eligible source schemas; source approval evidence; allowed scope vocabularies; whether a customer-specific contract may override a more-specific service rule (the recommended precedence in D2 applies); required independent approver constraints.
- **Accountable roles:** Finance Owner (pricing fact authority); Product Owner (product scope); Data Owner (source identity/lineage); Security Owner (access); Architecture (contract fit).
- **Implementation impact:** New versioned pricing authority records and approval references; extensions to canonical source/association handoffs are required only if existing contracts cannot carry the approved binding. No second source registry.
- **Acceptance criteria:** Given an unapproved revision, historical estimate, inaccessible source or LLM-proposed numeric value, no `PricingBasis` is emitted. An approved exact source produces a basis only within its declared scope. Revocation prevents new use without rewriting historical artifacts.
- **Status:** PRODUCT SCOPE ACCEPTED; FINANCE / DATA AUTHORITY DETAILS PENDING.

### D2 — Source selection and precedence

- **Question:** How is one applicable price selected reproducibly?
- **Alternatives:** (a) one workspace-wide book; (b) ordered, versioned scoped tiers; (c) per-estimate human selection.
- **Recommendation:** (b), with this precedence:

  1. Approved special rate/contract that exactly matches customer, service and applicable period.
  2. Approved versioned rate set for the matching service class or project class.
  3. Approved workspace base price book.
  4. A manual price decision only after a separate override request and approval; it is not an automatic fallback.

- **Normative rules:** Before ranking, candidates must all match trusted `workspace_id` and `environment_id`, service/item identity, allowed project/client scope, effective date, current approval and source revision, currency and unit, and current read/process authorization for the actor/workflow. Select by the approved rule-set's stable precedence and specificity keys, never by arrival order, file order, title, freshness timestamp alone, or LLM ranking. Class specificity is exact service+project class, then exact service class, then exact project class; missing class fields are non-matches, never wildcards. If two candidates have equal approved rank and no explicitly approved tie-breaker proves equivalence, return `PRICE_CONFLICT`; do not select arbitrarily. An explicit authority decision must supersede/revoke one source or publish a successor policy/rule set; a workflow manager cannot silently resolve canonical source conflict.
- **Parameters to approve:** service/project class taxonomy; contract specificity rules; precedence within class; tie/equivalence definition; policy version activation semantics.
- **Accountable roles:** Finance Owner (selection semantics); Product Owner (class model); Data Owner (source keys); Security Owner (candidate visibility); Architecture.
- **Implementation impact:** New deterministic resolver contract and typed conflict outcome. The Context Broker supplies authorized evidence but does not choose commercial authority. Any `proposal.pricing.v1` Context Profile must select candidate evidence narrowly; it cannot grant price authority.
- **Acceptance criteria:** Golden candidate sets prove each tier, scope mismatch exclusion, exact tie conflict, and stable identical result independent of candidate ordering. Unauthorized candidates are not disclosed and cannot affect selection.
- **Status:** PRODUCT PRECEDENCE ACCEPTED; FINANCE / DATA VALIDATION PENDING.

### D3 — Price freshness and effective period

- **Question:** When is a price valid and how do changes affect estimates?
- **Alternatives:** (a) one global TTL; (b) source-specific effective periods and approval/currentness; (c) treat ingestion time as freshness.
- **Recommendation:** (b). Each estimate pins `pricing_as_of` (a date-only business parameter selected by approved policy) and each basis pins `effective_from` and optional exclusive `effective_to`; applicability is `effective_from <= pricing_as_of < effective_to`, in the approved business timezone/date convention. No universal TTL is defined.
- **Normative rules:** A source is eligible only when exact revision/version is approved, within its declared period and not superseded/revoked for new calculations under the source's authority policy. Effective dates are not inferred from file modified time, sync time or model extraction. Each authority policy must state whether end dates are exclusive and what business event supplies `pricing_as_of` (proposal date, quote date, or another explicit command field); no default is presumed. On source supersession, existing approved estimates retain their pinned basis. A recalculation creates a successor Estimate version using then-current authority and requires review/approval. A revocation makes affected unapproved drafts ineligible; an already-approved Estimate remains immutable but is flagged for a human disposition before re-use or external issuance if governing policy requires.
- **Parameters to approve:** date source and timezone; boundary convention; supersession/revocation behavior for approved-but-unsent estimates; any source-specific maximum-age rules, owned by the source approver (no TTL invented here).
- **Accountable roles:** Finance Owner (validity); Product Owner (business date); Data Owner (revision/current pointers); Security Owner (revocation access); Architecture.
- **Implementation impact:** Exact source-version and policy snapshot on every basis and calculation. Existing SourceRevision immutability/current-source patterns are reusable, but pricing approval/effective-period semantics are new contracts.
- **Acceptance criteria:** Boundary tests at start, exclusive end, supersession and revocation; replay against pinned basis reproduces the original calculation; new calculation refuses revoked/stale basis and does not edit old artifact.
- **Status:** PRODUCT `pricing_as_of` SEMANTICS ACCEPTED; FINANCE / DATA EFFECTIVE-DATE POLICY PENDING.

### D4 — Currency and FX authority

- **Question:** Which currency and conversion evidence determines the quote?
- **Alternatives:** (a) one currency per workspace; (b) explicit estimate currency and optional approved FX conversions; (c) implicit provider/current online conversion.
- **Recommendation:** (b). Every price and Estimate declares ISO currency. The Estimate declares its output currency. Same-currency pricing needs no FX. Cross-currency conversion is allowed only under one Finance-approved source policy and exact pinned rate record.
- **Normative rules:** Persist exact decimal rate, base/quote currencies, direction, as-of date/time, provider/source identity and version/digest, authority approval, retrieval/source revision and calculation stage. The proposed Finance profile converts the fully discounted and taxed line amount from source currency to Estimate currency after tax calculation and before final line quantization; Finance must explicitly approve or replace this stage. Do not perform ad hoc online lookup or infer missing rates. Reject ambiguous direction, unsupported pair, expired/unapproved source policy or missing as-of evidence as `FX_RATE_UNAVAILABLE`. Intermediate conversion precision and rounding stage are explicit rule-set inputs and part of calculation version.
- **Parameters to approve:** estimate base currency by workspace/environment or permitted set; one FX authority/source; pair coverage; as-of convention; permitted rate age from source policy; conversion/rounding stage.
- **Accountable roles:** Finance Owner (currency and FX authority); Data Owner (rate provenance); Security Owner (read policy); Product Owner (quote currency); Architecture.
- **Implementation impact:** New FX basis contract and calculator operation. No provider, bank, rate or numerical example is selected by this package.
- **Acceptance criteria:** Same exact inputs replay to identical converted amount; wrong pair, inverted rate, stale/unapproved rate and missing rate fail closed; no network request exists in calculator path.
- **Status:** PRODUCT REQUIREMENT ACCEPTED; FINANCE CURRENCY / FX POLICY PENDING.

### D5 — Decimal arithmetic and rounding

- **Question:** What arithmetic and rounding order is normative?
- **Alternatives:** (a) binary float/framework default; (b) Decimal with explicit per-currency versioned policy; (c) round each intermediate without recorded policy.
- **Recommendation:** (b); propose `ROUND_HALF_UP` and currency minor-unit precision as an initial candidate for Finance review only. It is not approved here.
- **Recommended CalculationProfile for Finance approval:** use finite `Decimal` values under a versioned decimal context (candidate precision 34 significant digits); calculate gross in PricingBasis currency; resolve tax mode and extract the pre-discount tax-exclusive base for inclusive prices; apply one approved discount to that base; recompute explicitly ordered tax components, with compound bases only where pinned references say so; calculate `net_source = taxable_base + sum(post-discount tax components)`; convert the completed line and report components to Estimate currency using one exact pinned FX basis **after tax and before output quantization**; quantize each final line total once and each displayed component once per line to the approved Estimate-currency quantum. Define Estimate `total` as the exact sum of quantized line totals with no second rounding. Sum already-quantized per-line display components; `rounding_adjustment` is the sum of formula-derived per-line residuals, bounded by the number of displayed components and quantum. Candidate output mode is `ROUND_HALF_UP`; precision and mode remain pending Finance approval. See the Finance v2 checklist for exact formulas and vectors. This is a proposal, not an approved financial policy.
- **Normative safeguards:** Validate finite Decimal quantity/unit price, unit compatibility and signs before calculation. Currency quantum, tax extraction formulas, component order, compound behavior, context precision, overflow/traps, quantization and reconciliation are explicit CalculationProfile inputs. No `float`, implicit locale conversion, or LLM arithmetic is permitted. Preserve exact/intermediate Decimal inputs and outputs needed for replay and audit.
- **Parameters to approve:** Decimal precision/scale/overflow behavior; currency exponent table/version; rounding mode; line-vs-total quantization stages; treatment of cash increments; negative/zero values.
- **Accountable roles:** Finance Owner (all arithmetic policy); Data Owner (input representation/locale); Architecture (deterministic implementation).
- **Implementation impact:** Pure Decimal calculator and immutable calculation profile; calculator cannot fetch sources or call models.
- **Acceptance criteria:** Independent hand-calculated golden vectors cover exact, repeating, half-way up/down, large and small boundary values, quantity fractions, discounts and tax ordering. Same versioned input always yields byte-stable canonical output; NaN/Infinity/float inputs are rejected.
- **Status:** PRODUCT DETERMINISM / NO-LLM-ARITHMETIC PRINCIPLE ACCEPTED; FINANCE PROFILE PENDING.

### D6 — Taxes

- **Question:** Which tax rule applies, and in what order?
- **Alternatives:** (a) tax omitted from system estimate; (b) approved versioned jurisdiction/category rules; (c) model or client document chooses current rate.
- **Recommendation:** (b). Store whether source unit prices are tax-inclusive or tax-exclusive; resolve jurisdiction and service category from approved typed inputs and an approved rule version.
- **Normative rules:** Tax rates/exemptions/special regimes come only from a Finance-/Tax-authorized versioned source. Resolve tax mode and, for inclusive prices, extract the pre-discount tax-exclusive base before applying the proposed discount-before-tax rule; then compute tax components sorted by explicit `tax_order`, with compound bases only from explicit prior-component references. Mixed inclusive/exclusive components are unsupported in the candidate absent a separately approved complete formula. An unsupported jurisdiction returns `TAX_JURISDICTION_UNSUPPORTED`; within a supported jurisdiction, missing/expired/ambiguous category or rate returns `TAX_RULE_UNRESOLVED`. Both block completion/approval; neither defaults to zero. FX conversion follows tax calculation. This package selects no country, regime or rate.
- **Parameters to approve:** jurisdiction resolution owner and source; service tax categories; tax authority source/version; inclusive/exclusive policy; exemption/special-regime evidence; order and rounding.
- **Accountable roles:** Finance/Tax Owner (rules and rates); Data Owner (jurisdiction/category source); Product Owner (required user inputs); Security Owner (sensitive tax data access); Architecture.
- **Implementation impact:** New tax rule/basis contract and typed unresolved outcome; do not embed country assumptions in the calculator.
- **Acceptance criteria:** Tests cover inclusive/exclusive prices, discount-before-tax policy, multiple applicable rules, exemption evidence, effective-date boundaries, rounding and unresolved classification. Unknown cases cannot produce an approvable total.
- **Status:** PRODUCT FAIL-CLOSED PRINCIPLE ACCEPTED; FINANCE / TAX POLICY PENDING.

### D7 — Discounts and manager overrides

- **Question:** Who can change selected pricing and how is the change authorized?
- **Alternatives:** (a) manager freely edits price; (b) policy-bounded discounts plus separately approved exception; (c) only Finance can edit any line.
- **Recommendation:** (b), preserving distinct read, calculate, discount/override request, override approval and final Estimate approval permissions. Discount policy may be percent, fixed amount or allowed contractual rule only if that representation and cap are explicitly approved.
- **Normative rules:** Numerical discount ceilings and role mappings remain unset pending Finance/Product approval. A manual price requires a non-empty reason code and explanatory rationale, prior basis/value/version, proposed value/version, actor, timestamp, evidence reference and approval reference. The override approval is a separate immutable decision; it never edits the underlying rate source or auto-approves the Estimate. A new override or price edit creates a new Estimate version and invalidates prior approval for that version. Recommend independent override approver from requester; Finance Owner must decide whether same-person multi-role approval is ever permitted and under what explicit role-scoped evidence.
- **Parameters to approve:** discount forms and numeric bounds; authorized requester/approver roles; independence/segregation; reason taxonomy and evidence; materiality thresholds; reapproval threshold (default recommendation: any changed price, basis, tax, FX, quantity, unit or discount is material).
- **Accountable roles:** Finance Owner (limits/approval); Product Owner (manager actions); Security Owner (permission and attribution); Data Owner (basis lineage); Architecture.
- **Implementation impact:** Workflow command/decision and artifact successor, using existing run and audit seams. No mutable price-list write path.
- **Acceptance criteria:** Unauthorized edit and self-approved/unapproved override are rejected under approved policy; every accepted override records before/after and approvals; source book digest remains unchanged; material successor cannot inherit predecessor approval.
- **Status:** PRODUCT SEPARATE-APPROVAL PRINCIPLE ACCEPTED; FINANCE LIMITS / SECURITY AUTHORITY PENDING.

### D8 — Missing, stale and conflicting pricing

- **Question:** How does R2 proceed when a valid price cannot be resolved?
- **Alternatives:** (a) historic/model fallback; (b) block calculation/approval with typed state; (c) ask manager to choose among candidates silently.
- **Recommendation:** (b), with manager clarification only through an authorized typed request. Manual override is permitted only through D7's separately approved path.
- **Normative outcomes:** `PRICE_NOT_FOUND`, `PRICE_SOURCE_STALE`, `PRICE_CONFLICT`, `CURRENCY_POLICY_UNRESOLVED`, `CALCULATION_RANGE_EXCEEDED`, `ESTIMATE_INPUT_INVALID`, `FX_RATE_UNAVAILABLE`, `TAX_JURISDICTION_UNSUPPORTED`, `TAX_RULE_UNRESOLVED`, `DISCOUNT_POLICY_UNRESOLVED`, `PRICE_ACCESS_DENIED`, `OVERRIDE_APPROVAL_REQUIRED`, `PRICE_BASIS_REVOKED`.

| Outcome | Draft behavior | Approval/calculation gate | Safe user information | Audit record |
|---|---|---|---|---|
| `PRICE_NOT_FOUND` | Draft may persist as incomplete with affected line unresolved. | No complete calculation or approval. | Missing eligible basis for visible service/work item; no guessed candidate price. | Safe reason, line/artifact refs, policy version, actor/run, candidate count only if disclosure-safe. |
| `PRICE_SOURCE_STALE` | Keep draft and pinned stale reference for diagnosis. | Re-resolution required; no approval. | Source is not current/valid for this quote. | Exact source revision/version and validity check outcome, access-controlled. |
| `PRICE_CONFLICT` | Keep unresolved conflict state. | Explicit authority resolution required. | More than one equally authoritative candidate; do not show protected candidate details absent access. | Candidate opaque IDs/digests and rank evidence in protected audit scope. |
| `FX_RATE_UNAVAILABLE` | Keep source-currency line unresolved. | No converted total or approval. | Approved conversion evidence unavailable. | Currency pair and failed policy reference, no invented rate. |
| `CURRENCY_POLICY_UNRESOLVED` | Keep line unresolved if currency identity or approved quantum is missing/unsupported. | No amount or approval. | Currency policy needs review. | Currency code and policy reference, without protected source details. |
| `CALCULATION_RANGE_EXCEEDED` | Keep line unresolved when an intermediate or quantized result exceeds the approved Decimal precision/range. | No amount or approval. | Calculation exceeds the supported numeric range. | Profile digest and safe range failure class; no hidden amount/source details. |
| `ESTIMATE_INPUT_INVALID` | Reject the line when quantity or another required calculation input violates the approved schema/profile. | No calculation or approval. | Estimate input needs correction. | Safe field/error code, actor/run/profile refs; do not include protected source values. |
| `TAX_JURISDICTION_UNSUPPORTED` | Keep tax applicability unresolved when jurisdiction is outside the approved supported set. | No tax-complete amount or approval. | This jurisdiction is not configured as supported. | Jurisdiction decision/policy ref, access-controlled. |
| `TAX_RULE_UNRESOLVED` | Keep tax status unresolved. | No approval of tax-complete amount. | Tax applicability requires review. | Rule-policy version and typed missing dimension. |
| `DISCOUNT_POLICY_UNRESOLVED` | Keep line unresolved if discount policy is missing/ambiguous; do not interpret missing policy as zero discount. | No complete calculation or approval. | Discount treatment requires review. | Discount policy ref and typed missing dimension; proposed values protected. |
| `PRICE_ACCESS_DENIED` | Preserve only safe unresolved state. | No candidate disclosure or approval. | Generic access-denied message that does not reveal source existence/content. | Denial decision under current authorization; no protected values. |
| `OVERRIDE_APPROVAL_REQUIRED` | Persist proposed override as pending human decision. | Not calculated as approved pricing; no estimate approval. | Pending authorized decision. | Request/prior/proposed state and actor/reason; approval ref absent until decided. |
| `PRICE_BASIS_REVOKED` | Mark draft for recalculation/review; retain historical reference. | No new approval/reuse until resolved. | Basis no longer eligible for a new calculation. | Revocation authority/version and affected artifact refs, protected by authorization. |

No status falls back silently to an old estimate, adjacent row, LLM-generated price, broader-scope unauthorized source or a lower tier after a same-tier conflict. Whether an incomplete draft can be saved is allowed; it cannot be presented as a complete estimate.
- **Parameters to approve:** Which safe metadata can be shown to each role; conflict-resolution authority; whether a previously approved but unissued proposal must be withdrawn after a source revocation.
- **Accountable roles:** Product Owner (experience); Finance Owner (pricing resolution); Security Owner (disclosure); Data Owner (source state); Architecture.
- **Implementation impact:** Typed outcomes and run read-model states; context/access failures must not leak source metadata.
- **Acceptance criteria:** Every outcome has a deterministic transition and no-money-leak test; all blocked cases preserve source/evidence lineage and cannot pass approval gate.
- **Status:** PRODUCT FAIL-CLOSED PRINCIPLE ACCEPTED; FINANCE / DATA / SECURITY PARAMETERS PENDING.

### D9 — Estimate review and reapproval

- **Question:** How are calculations, manager review, approval and revisions represented?
- **Alternatives:** (a) mutable estimate and boolean approved flag; (b) immutable versioned artifact and exact-version human decision in the R2 run; (c) separate pricing workflow engine.
- **Recommendation:** (b). Proposed lifecycle: `DRAFT → CALCULATED → PENDING_REVIEW → APPROVED`; alternate outcomes `REJECTED`, `REQUIRES_RECALCULATION`, `SUPERSEDED`. These are artifact/review states composed within the existing canonical `WorkflowRun` and `WorkItem`, not a second workflow.
- **Normative rules:** Every calculation creates an immutable artifact version. Approval binds the exact Estimate version, its pricing/rule-set/FX/tax/discount/calculation versions, current source and policy validations, reviewer identity/authority and decision time. Recalculation or any material source/input/rule change creates a new version and requires a new approval. Prior approval remains attached only to its exact predecessor version. Rejection/rework uses typed commands on the same run. No approved artifact is edited in place.
- **Parameters to approve:** Final approver roles; whether preparer and approver must differ; which changes are material (recommended all amount-affecting or evidence/authority changes); approval expiration/reuse rule; unsent approved estimate handling.
- **Accountable roles:** Product Owner (lifecycle); Finance Owner (financial approval); Security Owner (authority); Architecture (WorkflowRun/artifact mapping).
- **Implementation impact:** Extend WorkflowCommand, event/read-model and artifact schemas after confirming the R2 workflow command contract. Existing requirements artifact precedent (ADR-0047/0058) is reusable; a generic proposal approval service is not present and must not be assumed.
- **Acceptance criteria:** Approval binds one exact artifact version; stale optimistic-concurrency command is rejected; duplicate command replays one stable decision; successor version is not approved by predecessor decision; rejection/recalc remains in the same run.
- **Status:** PRODUCT IMMUTABILITY / EXACT-REAPPROVAL / SINGLE-WORKFLOW PRINCIPLES ACCEPTED; ARCHITECTURE / FINANCE / SECURITY DETAILS PENDING.

### D10 — Security, provenance and audit

- **Question:** Which actors can read, calculate, alter and approve prices, and what lineage must remain?
- **Alternatives:** (a) source ACL implies pricing role; (b) content authorization and pricing business authority are distinct; (c) workspace administrator may access all pricing.
- **Recommendation:** (b). Current `AccessPolicy` authorizes source-content reads/processing. A separate versioned pricing authority policy controls business operations. Neither document access nor administrator status alone confers pricing authority.
- **Normative rules:** Server derives `workspace_id`, `environment_id`, acting identity, principal, purpose and operation. Every query, command, audit and artifact is tenant-scoped. Cross-tenant source candidate or identity is rejected before disclosure or calculation. Authorization is checked when reading source evidence and again at any protected disclosure/decision boundary. Each line and total links to exact `PricingBasis`, source revision/version+digest, authority/rule-set identity+digest, price item, unit/currency, effective period, current approval decision, selection policy and rank, FX/tax/discount references, calculation profile/version, exact input artifact versions and actor/decision refs. Historical analogues are separately typed and cannot enter the active basis field. Audit is append-only and records decision kind, exact target versions, actor, reason, outcome and correlation/idempotency identity; diagnostics are content-free.
- **Role matrix (proposed; role assignment itself requires approval):** reader sees only currently authorized price content; calculator service can process only approved inputs and no source mutation; pricing maintainer proposes new immutable source versions; Finance authority approves source/rule/discount policy; manager requests scoped override; independent authorized approver approves override; Estimate approver approves exact result version. No role inherits access solely from being administrator or artifact owner.
- **Parameters to approve:** Role-to-principal mapping; delegated service authority; separation of duties; permitted pricing metadata in audit/read models; retention of pricing artifacts and legal/tax evidence; export/disclosure audience.
- **Accountable roles:** Security Owner (authorization/roles); Finance Owner (business authority); Data Owner (identity and provenance); Product Owner (manager scopes); Architecture (boundary and audit fit).
- **Implementation impact:** Reuse trusted execution context, current AccessPolicy, immutable SourceRevision, tenant-scoped artifacts, Context Broker authorization, canonical AuditEvent and idempotent command digest/outbox. New pricing authority and artifact contracts are needed. No Cognee projection is canonical for prices.
- **Acceptance criteria:** Two-workspace isolation; denied protected content absent from output/error/audit metadata; every successful line validates exact lineage; changing any pinned digest/version changes calculation identity; repeated identical command/key has one logical effect and stable response; no source mutation on pricing operation.
- **Status:** PRODUCT SEPARATION / PROVENANCE PRINCIPLES ACCEPTED; SECURITY / DATA / ARCHITECTURE DECISIONS PENDING.

## 4. Formal contract summary

Detailed contract sketches and mapping to existing types are in the companion [implementation handoff](issue-586-pricing-implementation-handoff-v1.md). Finance's proposed operation order, tax reconciliation and required decisions are in the [Finance approval checklist](issue-586-finance-approval-checklist-v1.md); the exact candidate payload is [CalculationProfile r2-commercial-v1](issue-586-calculation-profile-candidate-v1.json). The expected graph is:

```text
authorized SourceRevision + explicit authority approval
    → PricingRuleSet / PriceItem
    → PricingBasis (one exact selected fact per EstimateLine)
    → deterministic CalculationProfile + Decimal engine
    → immutable Estimate artifact version
    → exact-version review/approval decision on existing R2 WorkflowRun
```

No second workflow, price store, registry, or authorization system is recommended. The source revision remains canonical source evidence; the approved immutable pricing contract binds its authority and business scope. Cognee/Context Broker may retrieve candidates only after authorization; candidate retrieval is not selection or approval.

## 5. Compatibility and decision validation

| Area | Current `main` evidence | Proposed compatibility / required change |
|---|---|---|
| Source identity and provenance | `SourceObject`, immutable `SourceRevision`, provenance and canonical acceptance exist; source revision is observed evidence, not authority. | Reuse exact refs/digests; add a new approval/price-eligibility contract. No change to accepted source semantics. |
| Google Sheets / R2 sources | #585 research and #598 specify governed ingestion; #613 associations pin revisions and explicitly deny PricingBasis authority. | Compatible. Association and access must not be promoted to pricing authority. Structured price rows still need an approved schema/authority mapping. |
| Context Broker / Context Profile | Context Profiles are immutable, approved, scoped retrieval restrictions applied after authorization; they do not grant access. | Add `proposal.pricing.v1` only as an approved retrieval profile once exact inputs are defined. It carries no pricing source approval or access grant. |
| Workflow / WorkItem | One canonical `WorkItem` + version-pinned `WorkflowRun`, typed commands, run events and read model exist. Temporal is the durable orchestrator. | Compatible by adding estimate events/commands/artifact handoffs on the same run. Proposal-specific lifecycle types are new; no second engine. |
| Artifacts | Generic tenant-scoped immutable artifact envelope, versions/lineage and requirements artifacts exist. | Reuse envelope and lineage; new typed schemas `PricingBasis`, `EstimateLine`, `Estimate`, `PricingDecision` required. Confirm immutable persistence/approval transaction boundary in specification. |
| AccessPolicy | Versioned source access is canonical and authorization uses trusted request context; administrator is not content superuser. | Compatible; pricing business permission is an additional domain decision, never a substitute for AccessPolicy. |
| Audit/outbox/idempotency | Append-only canonical audit, diagnostics separation, transactional outbox and command digests/replay patterns exist. | Reuse for pricing decisions, override and approval. Add event schemas/registry fields; do not create parallel audit trail. |
| Retention | `RetentionApplicability` is a versioned source/artifact retention-policy binding, not an access or pricing authority. | Reuse only if pricing evidence falls within its approved classes; do not reuse retention policy as price freshness or approval policy. |
| Decimal/money | No commercial `Money`, `PricingBasis`, tax, rounding or Estimate implementation found. Decimal exists in unrelated telemetry/provider cost paths. | New contract and pure engine required. Existing Decimal usage is not a financial policy precedent. |
| ADRs | ADR-0011/0018/0020/0029/0047/0048/0058 and source/provider ADRs define access, lineage, audit, context handoff and exact human decision boundaries. No accepted ADR defines pricing authority. | No direct conflict found. The proposed Architecture record requires a new ADR before implementation approval for canonical ownership/activation of pricing authority and atomic exact-version approval/CAS. This is not an accepted ADR. Do not modify accepted ADRs. |
| Roadmap/product case | R2 calls for deterministic money, per-line pricing evidence, human review/edit/approve; #584 says pricing implementation waits for #586. | Directly aligned. This package does not change roadmap checkboxes or unblock implementation until approvals and follow-up specification exit criteria pass. |

### Explicit blockers / gaps

1. **Human authority is partially recorded:** the Product Owner accepted product-scoped principles against the exact v1.1 package digest. Finance, Security, Data and Architecture decisions remain unrecorded and cannot be inferred from that acceptance.
2. **No pricing runtime contracts exist:** no `PricingBasis`, `EstimateLine`, `Estimate`, price-source approval, FX/tax/discount rules or deterministic commercial calculator found in current `main`.
3. **Approval workflow is not generic today:** existing exact-version requirements acceptance is a precedent, not a reusable proposal approval implementation. R2 needs its own typed command/event contract inside the same WorkflowRun.
4. **No ADR conflict identified.** Existing ADRs explicitly keep source read authority and source associations separate from PricingBasis authority. Should implementation decisions change canonical storage, authority, workflow or audit boundaries, record a new ADR after approvals; do not amend existing ADRs here.
5. **Native blocker status:** #586 itself states no native blockers. Parent #584 marks pricing implementation as dependent on Finance/data-owner authority. #585 and #588 are closed input decisions; #598/#613 source work does not approve price use.

## 6. Traceability and implementation readiness

| #586 acceptance criterion | Decision | Contract | Future implementation/test | Current readiness |
|---|---|---|---|---|
| Product/Finance selects authority, precedence, freshness | D1–D3 | PricingAuthority, PricingRuleSet, PriceSourceRef | Authority binding and versioned resolver; approved source classes/scope/effective-boundary tests | Product scope accepted; Finance + Data pending |
| Security/Data confirms read/override roles | D1, D7, D10 | PricingPermissionPolicy, ApprovalRef, AccessPolicy refs | Reauthorize source read and commands; two-workspace/denial tests | Pending Security + Data + Finance |
| Currency, discount, tax, rounding deterministic | D4–D7 | Money, FXBasis, TaxBasis, DiscountPolicy, CalculationProfile | Pure Decimal engine and boundary golden vectors | Pending Finance/Tax + Data parameters |
| Missing/stale/conflict and override audited | D7–D9 | PricingOutcome, OverrideDecision, EstimateReviewDecision | Typed blocked outcomes, approval transitions, replay/idempotency tests | Contract spec can follow after human decisions; implementation not approved |
| Facts distinguished from model conclusions/historical analogues | D1, D2, D8, D10 | Authority-bound PricingBasis vs HistoricalEstimateRef | Adversarial retrieval/resolver tests; no fallback and no LLM money arithmetic | Product principle accepted; Data + Security details pending |

### Issue #586 closure blockers

Keep #586 **OPEN / PENDING_APPROVAL**. Product Owner scope has been accepted and recorded against v1.1 digest `73c04fc138dcb36c34fff57169085ddbd87ada19b15d8be31dc4eae54b4e8325`. Closure still requires attributable Finance decisions for authority, currency/FX, precision/rounding, taxes, discount/override limits and effective-date policy; Security decisions for pricing-source read, calculation, override, independent approval and disclosure; Data decisions for source identity, revision, mapping and provenance; and Architecture confirmation of reuse/extension boundaries. Each role decision identifies decision IDs, exact package version/digest, actor's role, decision time and accepted parameters or explicit rejection/change. Product Owner approval does not substitute for any other role.

Implementation readiness additionally requires approved parameter values and an approved detailed implementation specification with test vectors. No issue is closed and no `IMPLEMENTATION_APPROVED` status is implied by this proposal or documentation PR.

## 7. Human approvals required and unresolved parameters

Use the role-specific forms; do not infer approvals across roles: [Product Owner](issue-586-product-owner-decision-v1.md), [Finance](issue-586-finance-approval-checklist-v1.md), [Security](issue-586-security-approval-record-v1.md), [Data](issue-586-data-approval-record-v1.md), [Architecture](issue-586-architecture-acceptance-v1.md).

| Owner | Must explicitly approve | Cannot be supplied by this package |
|---|---|---|
| Project Owner / Product Owner | **Recorded ACCEPT** for Option 2, product source classes/scope/order, quote-date meaning, fail-closed behavior, separate override approval, immutable Estimate/reapproval, no LLM arithmetic and existing R2 WorkflowRun. | Finance authority, actual rates, currency allowlist, FX source/rates/stage, Decimal/rounding, tax rules, discount/override limits, Security/Data permissions and mappings, Architecture/ADR acceptance. |
| Finance / Pricing Data Owner | Source classes/owners; authority approval evidence; rule precedence details; effective date source; quote currency; FX authority/rate policy; Decimal precision and rounding; tax source/jurisdiction/categories/inclusive rules; discount limits; override and estimate approvers. | Security grants or Data source identity decisions unless separately assigned those roles and recorded separately. |
| Security Owner | Price-content reader/calculator/maintainer/approver principals and policy; least privilege; protected metadata and audit visibility; segregation constraints; delegated service identity. | Finance rate/tax/discount values or Data source correctness. |
| Data Owner | Canonical structured-source identity/mapping, stable price-item IDs and UOM/currency/tax metadata, revision semantics, provenance quality and data-change/revocation semantics. | Business approval of prices or access grants. |
| Architecture | Reuse of SourceRevision, Context Profile, Artifact, WorkflowRun, AccessPolicy, Audit/outbox seams; any needed new ADR; transaction/idempotency boundary. | Product/Finance/Security/Data policy approval. |

Unresolved parameter register: (1) role/principal map and delegation, (2) service/project class taxonomy, (3) price item/UOM schema, (4) pricing-as-of source/timezone and interval boundary, (5) output currency set, (6) FX source/pairs/as-of/staleness/conversion policy, (7) decimal precision and rounding profile, (8) tax jurisdiction/category/source/period and inclusion mode, (9) discount form/caps/materiality, (10) override reason/evidence/segregation, (11) safe error/audit disclosure, (12) retention class and approved-estimate revocation disposition. None receives an invented value here.

### Sequential approval plan

1. **Product Owner — recorded.** ACCEPT is limited to Option 2 and the product principles listed in the role record, bound to the v1.1 digest above.
2. **Finance/Tax — next.** Decide authority and precedence for financial facts; allowed source/output currencies; FX authority, exact rate evidence, and after-tax conversion stage; Decimal precision/traps/input scales; rounding mode, quantum and quantization/aggregation points; tax-inclusive/exclusive formulas, jurisdictions, multiple/compound ordering and exemptions; discount forms/limits; override and Estimate approver authority; effective periods, conflicts and missing-rate behavior. No actual tariff/rate/limit is presumed.
3. **Data Owner.** Approve stable PriceItem identity, exact SourceRevision and row mapping, UOM/currency/tax metadata, provenance quality, source authorization/currentness binding, and revocation/supersession/ambiguity semantics.
4. **Security Owner.** Approve role-scoped read/calculate/maintain/request/approve permissions, separation of duties, audit disclosure and workspace/environment isolation against the settled source and command contracts.
5. **Architecture.** Decide canonical pricing-authority ownership and immutable rule-set activation; currentness model; atomic boundary for authorization, exact Estimate approval, CAS, audit and idempotency; reuse of R2 workflow/artifacts. Record a new ADR for this hard-to-reverse authority/atomicity decision before implementation approval; no ADR is approved here.

Each decision is recorded separately by role against the exact current package version/digest. If an approval changes a previously accepted Product Owner principle, return that changed scope to the Project Owner for an attributable decision before implementation readiness.

## 8. Issue decision comment content

This is the status and content to publish as an assistant-authored issue comment. It reports the supplied conversational Product Owner decision with its provenance limitation; it does not represent a GitHub-authored decision or electronic signature by the Project Owner. Separate role-scoped Finance, Security, Data and Architecture decisions remain required.

```markdown
## Product Owner scope accepted — Issue #586 (HUMAN_APPROVAL_PENDING)

The Project Owner explicitly accepted Option 2 and the product-scoped D1–D10 principles on 2026-11-07 11:11:07 UTC. The accepted package was v1.1, SHA-256 `73c04fc138dcb36c34fff57169085ddbd87ada19b15d8be31dc4eae54b4e8325`. Exact question and answer, scope and attribution limitations are in the [Product Owner decision record](issue-586-product-owner-decision-v1.md). This reports conversational evidence; it is not a comment authored by the Project Owner, not an electronic signature, and not approval of remaining role decisions.

Recommended source order:

1. Exact approved customer/service/period contract rate.
2. Approved versioned service/project-class price book.
3. Approved workspace base price book.
4. Manual price only as a separately requested and approved override; never an automatic fallback.

Historical estimates are analogue/reference evidence only. Source access, Google row association, retrieval relevance or an LLM result does not grant pricing authority. LLMs do not calculate or invent money. A same-rank conflict fails closed and requires an explicit authority decision.

Each calculation pins its pricing-as-of date, exact source revision/version and digest, approval, scope, currency/unit, rule-set/policy digest, FX/tax/discount bases and calculation version. No global freshness TTL is proposed; the approved source policy and effective interval govern. Supersession/recalculation creates a new Estimate version. An approval applies only to its exact version.

Use Decimal-only arithmetic and a versioned rounding profile. `ROUND_HALF_UP` is only a candidate for Finance review. Currency, FX provider/rates, tax jurisdiction/rates, rounding precision, discounts and override limits are intentionally unset pending Finance/Data approval. No online FX lookup or silent tax inference is allowed. Missing, stale, conflicting, inaccessible or revoked basis blocks complete calculation/approval with a typed outcome; it never falls back to a historical estimate.

Pricing-source authorization remains distinct from current source-content AccessPolicy. Estimates, overrides and approvals are immutable, tenant-scoped and auditable in the existing R2 WorkflowRun/artifact lifecycle; this creates no second workflow or mutable price-list path.

### Role-scoped decisions requested

| Role | Decision requested | Status |
|---|---|---|
| Product Owner | Option 2 and product-scoped D1–D10 direction. | ACCEPTED, exact scope only |
| Finance Owner | Approve source authority/precedence, FX authority, currency, Decimal/rounding, tax rules, discounts, override and approval limits. | PENDING_APPROVAL |
| Security Owner | Approve read/calculate/maintain/override/approve permissions, segregation and safe disclosure/audit scope. | PENDING_APPROVAL |
| Data Owner | Approve source identity, item/UOM mapping, revision/provenance and revocation semantics. | PENDING_APPROVAL |
| Architecture | Confirm existing SourceRevision, Context Profile, WorkflowRun/artifact, AccessPolicy and audit seams; identify any ADR required. | PENDING_APPROVAL |

Record each decision against `issue-586-r2-pricing-decision-package-v1` and identify the approving person's role, decision IDs, accepted parameters and date. One role's approval does not imply another role's approval. Until the required decisions and implementation parameter values are recorded, #586 remains open and implementation approval is withheld.
```

## 9. Evidence reviewed

- Initial upstream baseline for the architecture review: `a272e982db26cb92170cd633c279c5857ab112f1`. Re-fetched current `origin/main` at `07caa391bcc221de1c61f099d775db7a0ce2ef2b`; intervening R1 evaluation changes do not touch reviewed pricing boundaries. The working checkout was on a separate feature branch with pre-existing untracked files; those were left untouched.
- #586 full issue description: Option 2 is its working recommendation, acceptance criteria are unchecked, no native blockers, no comments at review time; [issue #586](https://github.com/alkselsv/spine/issues/586).
- Parent #584 defines R2 boundaries and says pricing implementation waits for #586; requirements-first slice and #588 interaction decision do not depend on pricing. [Issue #584](https://github.com/alkselsv/spine/issues/584), [#584 handoff comment](https://github.com/alkselsv/spine/issues/584#issuecomment-6467689955).
- #585 is closed research for governed Google source identity/revision/access and explicitly separates PricingBasis authority. [Issue #585](https://github.com/alkselsv/spine/issues/585).
- #588 is closed; its interaction decision uses one canonical WorkItem/WorkflowRun; proposal approval is still not a generic implemented service. [Issue #588](https://github.com/alkselsv/spine/issues/588).
- #598 is a closed connector specification; Google ingestion uses canonical SourceObject/SourceRevision and does not approve pricing source use. [Issue #598](https://github.com/alkselsv/spine/issues/598).
- #613 accepted register-to-material association claims; explicitly no PricingBasis authority. [Issue #613](https://github.com/alkselsv/spine/issues/613).
- Source contracts inspected in `src/spine/domain/sources/models.py`; Context Profile in `src/spine/domain/context_profiles/models.py`; artifacts in `src/spine/domain/artifacts/models.py`; workflow/run contracts in `src/spine/domain/workflows/runs.py`, application WorkflowCommand and requirements artifacts.
- Architecture references: ADR-0011 (AccessPolicy), ADR-0018 (source/projection lifecycle), ADR-0020 (audit/diagnostics), ADR-0029 (ContextBundle), ADR-0047 (immutable requirements versions), ADR-0048 (current evidence authorization), ADR-0058 (exact requirements acceptance), ADR-0044/0050/0053 (provider and association authority boundaries). No accepted ADR currently defines commercial pricing authority.
- Product references: `docs/ROADMAP.md` R2; `docs/AGENT_DEVELOPMENT.md` `proposal.pricing.v1` and deterministic Decimal code step; `docs/ARCHITECTURE.md`; `docs/INTERFACE.md`; `CONTEXT.md`.

## 10. Change and approval history

| Version | Date | Change | Approval |
|---|---|---|---|
| v1 | 2026-11-07 | Initial recommendation and D1–D10 review package based on main `a272e982`. | None. |
| v1.1 | 2026-11-07 | Added a single concrete proposed Finance CalculationProfile, FX-after-tax stage, separate role-scoped approval records and an explicit pre-implementation ADR question after Standards/Spec review. | Product Owner ACCEPT for product scope only, 11:11:07 UTC, exact SHA-256 recorded above; other roles pending. |
| v1.2 | 2026-11-07 | Status/attribution overlay, granular D1–D10 role status, correction of stale “no approvals” language, and sequential approval plan. Product Owner scope unchanged from v1.1. | Product Owner ACCEPT remains limited to product scope; other roles pending. |
| v1.3 | 2026-11-07 | Finance/Tax qualification, exact candidate CalculationProfile payload, resolved inclusive/exclusive and reconciliation definitions, synthetic vectors, and typed currency/range/tax-jurisdiction/discount blockers. Product Owner principles remain unchanged. | PROPOSED / PENDING_FINANCE_APPROVAL; Security, Data and Architecture still pending. |
