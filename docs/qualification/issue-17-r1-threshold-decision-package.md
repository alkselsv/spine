# Issue #17 R1 threshold decision package

Status: published for review in [documentation PR #500](https://github.com/alkselsv/spine/pull/500); not yet integrated into `main`.

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

Issue #17 remains open with `wayfinder:task`, no assignee, `ready-for-agent`, or
`in-progress`. It has two native child issues: #501 and #502. Issue #502 is
natively blocked by #501. Both children are open, unassigned, and not marked
`ready-for-agent` because documentation PR #500 is not yet integrated; #502 is
also blocked by #501. Issue #16 remains open and is blocked by #17.

The product decisions and technical seam needed to finish grilling are settled.
The implementation-ready spec and two approved ticket bodies are published in
PR #500 and issues #501/#502. Numerical threshold decisions are not required to
implement the policy/evaluation infrastructure; they require a future valid
real baseline. No architectural or product question remains open. This package
preserves a separate release-gate decision from full R1 end-to-end qualification
in Issue #16.

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

## Implementation tickets

The Product Owner approved the two-ticket breakdown:

| Ticket | Scope | Dependency | Status |
| --- | --- | --- | --- |
| [#501 — Baseline measurement and coverage aggregates](https://github.com/alkselsv/spine/issues/501) | Metadata-only real baseline, strata, metric applicability/denominators/coverage, latency, existing operational measurements, and privacy-safe failure clusters | None | Open; unassigned; no `ready-for-agent` while PR #500 is unintegrated |
| [#502 — Versioned approved thresholds and pure release gate](https://github.com/alkselsv/spine/issues/502) | Immutable policy, Product Owner approval provenance, compatibility checks, safety-first deterministic decision, and metadata-only results | Natively `blocked by #501` | Open; unassigned; no `ready-for-agent` while PR #500 is unintegrated and #501 is open |

Both are native sub-issues of #17. Ticket 1 can become claimable after the
documentation PR is integrated and the normal ownership check passes. Ticket 2
also requires #501 to be completed and closed.

## Publication and current-state record

Read-only GitHub refresh on 2026-10-19 found no open duplicate implementation
tickets. The documents are published for review in PR #500. Issues #501 and
#502 were created and linked as native sub-issues; the native dependency #502
blocked by #501 was written and read back successfully. Parent #17 remains open
and now carries `wayfinder:task` instead of `wayfinder:grilling`. No assignee
or `in-progress` claim was added. PR #500 remains open and unmerged.

The three published documents are the specification, this decision package,
and the operator retry checklist. The issue drafts remain outside the docs
commit; no protected dataset artifacts were published.

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
6. Documentation PR #500 needs review and integration under the repository's
   documentation review process. No merge was performed. Ticket #501 is the
   first implementation candidate after PR integration and a valid claim;
   #502 additionally waits for #501 to close.
