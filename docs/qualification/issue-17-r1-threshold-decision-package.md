# Issue #17 R1 threshold decision package

Status: local decision package; not published to GitHub.

## Decisions recorded

- Product Owner for R1 thresholds: the user who initiated this task.
- Approval authority: explicit Product Owner decision; no other approval is
  implied.
- Private benchmark execution: authorized locally only after a successful
  accepted Issue #453 runtime preflight.
- Dataset: immutable `0.3.9-user-approved`, SHA-256
  `7c2787272fd34643ff5168a46f5672b5a091f3070888546fa7b6ed0c918b3a62`.
- Hard safety invariants: immutable; never configurable by product thresholds.
- Numerical product and operational thresholds: `decision_required`.
- Release approval: absent.
- Proposed pure release decision seam in the existing `release_gates` package:
  approved policy + validated metadata-only benchmark/operational evidence +
  mandatory safety results → deterministic per-metric and overall gate result.
  Product Owner confirmed this seam.

## Current issue and workflow

Issue #17 remains open with `wayfinder:grilling`, no assignee, no
`ready-for-agent`, no `in-progress`, and no comments. Its native blockers #19,
#20, and #25 are closed; Issue #16 remains open and is blocked by #17. No
ownership claim or GitHub mutation has been made.

The product decisions and technical seam needed to finish grilling are now
settled. The implementation-ready spec and two approved ticket drafts are
prepared locally. Numerical threshold decisions are not required to implement
the policy/evaluation infrastructure; they require a future valid real
baseline. No architectural or product question currently blocks publication.
The package preserves a separate release-gate decision from full R1 end-to-end
qualification in Issue #16.

## Dataset check and runtime preflight

A local structural validation of the explicitly pinned dataset completed
successfully. Its metadata-only result remains in operator-controlled local
temporary storage and is not part of this publication:

| Field | Result |
| --- | --- |
| Dataset | `0.3.9-user-approved` |
| SHA-256 | `7c2787272fd34643ff5168a46f5672b5a091f3070888546fa7b6ed0c918b3a62` |
| Sources | 30 |
| Cases | 149 |
| Answered / abstained / denied | 136 / 13 / 0 |
| Structural validation | `passed` |
| Evaluation | `not_run` |

**No production preflight or benchmark was run.** The accepted qualification
contract could not obtain a trusted local production composition/preflight
factory. The safe runbook reason is `preflight_unavailable`. Current local
configuration inspection found no PostgreSQL URL, local authentication
configuration, Temporal address/namespace, `.env`, or `.env.local`; no local
qualification factory was found in the repository. Credentials and their
values were not inspected or printed.

As a result, trusted principal/scope, current authorization and AccessPolicy,
PostgreSQL canonical state, Temporal/workers, production composition, capture
channel, owner-approved source mapping, canonical accepted/current revisions,
active healthy projection membership, and no-fallback composition could not be
verified through the trusted preflight. No Q&A observation was started. The
current result is not a `ready` qualification and no benchmark report exists.

## Baseline measurement status

There are no real observations, quality aggregates, real failure clusters,
runtime-linked operational measurements, or comparable real-run deltas. The
synthetic offline baseline remains out of scope for product threshold values.

| Metric | Version / statistic | Eligible population and denominator | Real measurement | Decision state |
| --- | --- | --- | --- | --- |
| Retrieval Recall@k | `retrieval.recall_at_k:v1`; relevant evidence found in top-k / eligible relevant evidence references | Answered cases with expected evidence; real reference denominator not measured | Unavailable | `decision_required` |
| Retrieval MRR | `retrieval.mrr:v1`; mean reciprocal rank of first relevant result | Eligible answered cases with expected evidence | Unavailable | `decision_required` |
| Groundedness | `qa.groundedness:v1`; supported claims / eligible expected claims, using explicit claim-evidence links | Claims evaluated via the 158 approved relationships; claim denominator not inferred from link count | Unavailable | `decision_required` |
| Answer outcome accuracy | `qa.answer_outcome:v1`; expected outcome matches observed outcome | 149 expected outcomes: 136 answered, 13 abstained, 0 denied | Unavailable | `decision_required` |
| Abstention quality | `qa.answer_outcome:v1`; correct abstention / eligible abstention cases | 13; direct abstention coverage is XLSX-only, with no PDF or DOCX abstention cases | Unavailable | `decision_required` |
| Denial quality | Existing outcome slice; zero eligible examples | `not_applicable`, `eligible_count=0` | Not applicable | No threshold evidence; never PASS by absence |
| Citation precision | `qa.citation_precision:v1`; valid emitted citations / emitted citations | Emitted citation count; no observations so denominator unavailable | Unavailable | `decision_required` |
| Citation recall | `qa.citation_recall:v1`; valid expected references found / all expected references | 177 references, including 19 supplementary citations | Unavailable | `decision_required` |
| Locator validity | `qa.locator_validity:v1`; valid locators / emitted citations | Emitted citation count; no observations so denominator unavailable | Unavailable | `decision_required` |
| Observer latency | execution-record v1/v2 `observer_elapsed_ms`; wall-clock duration | Complete observed cases with observer timing | Unavailable | `decision_required` |
| Worker latency | execution-record v2 `worker_execution_elapsed_ns`; worker-local duration | Complete observed attempts with worker timing | Unavailable | `decision_required` |
| Cost | Existing operational usage measurements, Decimal plus currency | Complete usage-linked observations by source and currency | Unavailable | `decision_required` |
| Indexing lag / freshness | `projection.indexing_lag`; existing source/projection watermark difference and coverage state | Projection events with both watermarks; benchmark-linked population not measured | Unavailable | `decision_required` |
| Administrator usefulness | Existing canonical feedback outcomes | Helpful feedback numerator and eligible feedback denominator not linked to a benchmark population | Unavailable | `decision_required` |

Coverage strata and real failure clusters cannot be reported without observed
results. The real benchmark report must add only privacy-safe aggregate strata
for approved document formats and existing scenario/outcome tags. Do not publish
opaque source or case identities. No metric deltas are computed without a
comparable real baseline.

## Threshold decision rules

- Do not propose numerical values before the Product Owner has reviewed a valid
  real baseline and its denominators, coverage, failure clusters, and trade-offs.
- Mark a metric unavailable when its data is not observed or its eligible
  population cannot be established. Do not substitute zero, an LLM estimate,
  or a synthetic score.
- Keep product thresholds separate from mandatory hard safety invariants.
- A draft/proposed threshold policy is inactive. Only explicit, attributable,
  versioned Product Owner approval can activate it.
- Missing approval, incompatible pins, incomplete required population, or
  unavailable required measurements must block a positive decision.
- A benchmark gate result does not qualify R1 end-to-end or close Issue #16.

## Local implementation ticket drafts

The Product Owner approved a two-ticket breakdown. Ticket 2 is explicitly
blocked by ticket 1 so the decision gate consumes the finalized metadata-only
evidence contract. These are local drafts only; no GitHub issues or dependency
records have been created.

### Ticket 1 — Produce reproducible metadata-only R1 baseline aggregates

**Goal:** Extend the existing real benchmark report/evaluator output to expose
the privacy-safe strata, failure-cluster summaries, exact eligible and measured
denominators, coverage limitations, and linked operational measurements that
the Product Owner needs to decide thresholds.

**Scope:** Reuse the approved dataset and existing evaluator, benchmark runner,
observer, and operational metric contracts. Report unavailable measurements as
`unavailable`; do not add a telemetry platform or new evaluation/execution
path. Include evaluator/observer/application and runtime pins and benchmark
execution scope. Keep reports metadata-only.

**Acceptance criteria:**

- Aggregates identify metric version, statistic, eligible count, measured
  denominator, unavailable count, applicability, and coverage.
- Supported strata include PDF/DOCX/XLSX and existing expected-outcome or
  scenario labels, without publishing private content or opaque identities.
- Abstention metrics use the 13 eligible cases; denial quality is
  `not_applicable` with eligible count 0; missing PDF/DOCX abstention coverage
  is explicit.
- Citation recall uses all 177 approved case-citation references, including 19
  supplementary citations. Those supplementary citations do not become
  claim-evidence relationships; groundedness uses the separate 158 approved
  relationships.
- Observer and worker latency remain separately measured. Cost,
  indexing/freshness, and administrator usefulness are reported as
  `unavailable` unless their existing contracts provide complete applicable
  populations and measurements.
- Partial, failed, or unavailable observations cannot be reported as a
  completed baseline. No metric deltas are emitted without a comparable real
  run.
- Deterministic tests cover denominator, applicability, coverage, supported
  strata, incomplete observations, and metadata-only serialization.
- The approved private benchmark is run locally only after successful #453
  preflight; it is not run in CI.

**Blocked by:** none. Existing authorized real benchmark execution still
requires a successful runtime preflight.

### Ticket 2 — Add versioned approved R1 threshold policy and decision gate

**Goal:** Add a pure deterministic decision seam under the existing
`src/spine/evaluations/release_gates/` package that evaluates an explicitly
approved immutable policy against compatible baseline and operational evidence
after mandatory hard safety gates pass.

**Scope:** Define the smallest typed policy and result contracts that bind
policy version/status and approval provenance; dataset identity/version/digest;
metric/evaluator versions; direction and comparison semantics; population,
denominator and coverage requirements; runtime/scope constraints; and safe,
metadata-only gate outcomes. Do not duplicate the evaluator, benchmark runner,
or qualification gate.

**Acceptance criteria:**

- Draft and proposed policies remain inactive. Missing or invalid Product Owner
  approval produces `not_approved`; only explicit attributable, versioned
  approval activates policy.
- Quality thresholds are evaluated only after mandatory hard-safety evidence
  passes. No threshold can override or weaken a safety invariant.
- Per-metric and overall outcomes distinguish `passed`, `failed`,
  `not_applicable`, `insufficient_evidence`, `not_approved`, and `unavailable`.
- Missing values, incompatible dataset/evaluator/metric/runtime pins, partial
  populations, or unmet coverage requirements cannot yield a positive result.
- Zero eligible denial cases produce `not_applicable`, never a pass; required
  abstention and citation-recall populations follow ticket 1's report
  contract.
- The decision is deterministic and records policy/evidence versions,
  thresholds applied, coverage, and safe reason codes without private content.
- Focused tests cover min/max and boundary semantics, unavailable and partial
  evidence, approval, applicability/denominators, runtime identity, safety
  priority, determinism, and metadata-only output.
- A threshold-gate result does not imply R1 end-to-end qualification or close
  Issue #16.

**Blocked by:** ticket 1, because the gate must bind to the finalized baseline
report and evidence contract. Numeric values remain `decision_required` until
the Product Owner reviews real measurements; implementation must support
inactive thresholds without manufacturing values or approval.

## Publication and current-state caveat

Read-only GitHub refresh on 2026-10-19 confirmed Issue #17 remains open with
`wayfinder:grilling`, no assignee, no comments, and no `ready-for-agent` or
`in-progress` label. An issue search found no open duplicate for either drafted
ticket. Related prior-art issues #358, #370, #452, #453, #469, and #478 are
closed; they do not establish a real baseline. No issue, label, ownership, or
dependency mutation was attempted.

The specification and ticket bodies are now locally publication-ready. Before
publishing, create Ticket 1 first and apply `ready-for-agent` only if it is
claimable under the issue tracker policy. Create Ticket 2 with a native
`blocked by Ticket 1` relationship; leave it unclaimable until Ticket 1 closes.
Then update the parent #17 with both child links and the dependency, remove
`wayfinder:grilling`, and apply the agreed parent tracking label (recommended:
`wayfinder:map`) while leaving #17 open. The parent update should state that
the threshold question is decomposed into baseline measurement and executable
policy work, that numeric values and release approval remain outstanding, and
that #16 remains the end-to-end release qualification. Do not self-assign or apply
`in-progress` absent the documented claim transition. Ticket implementation
must start only after the ticket is open, `ready-for-agent`, unassigned, has no
open blocker, and is explicitly claimed.

The two implementation tickets will be linked here after publication. The
operator retry checklist is published alongside this package.

## Remaining blockers

1. Runtime owner must supply a locally configured trusted production
   qualification factory and PostgreSQL, auth/scope, Temporal/worker, and
   authenticated capture-channel configuration.
2. Through that trusted runtime, preflight must verify current authorization and
   AccessPolicy, approved source mapping, canonical current revisions, active
   healthy exact projection membership, and production/fake identity.
3. After a fully ready preflight, the authorized local real benchmark can run
   once and produce a metadata-only report. Partial/unavailable/failed
   observations do not qualify as a complete baseline.
4. Gaps in stratified real-report aggregates and benchmark-linked operational
   cost, indexing lag/freshness, and usefulness need minimal contract extensions
   or must remain `unavailable`.
5. The Product Owner must review the actual evidence and explicitly approve or
   reject proposed numerical thresholds and the versioned release policy.
6. The local spec and two-ticket drafts need the repository's `to-spec` and
   `to-tickets` publication steps. No issue mutation or publication has been
   made under this authorization. Implementation only starts after the valid
   claim transition.
