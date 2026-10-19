# Issue #17: R1 threshold decision and executable release gate

Status: implementation-ready; published for review in the documentation PR and
not yet integrated into `main`.

Originating issue: GitHub Issue #17, `[Release gate] Baseline quality and approve final R1 thresholds`.

## Problem Statement

R1 has deterministic evaluation contracts and an approved private real Q&A
dataset, but no real production-flow benchmark report and no product-approved
quality thresholds. Synthetic results, dataset validation, and benchmark
qualification are not evidence of real R1 quality. Without a versioned policy
and deterministic decision boundary, operators cannot reproduce a release
decision or distinguish a failed threshold from missing or inapplicable
evidence.

## Solution

Use the existing opt-in real benchmark and evaluator contracts to produce a
metadata-only baseline from actual authorized production Q&A observations. The
Product Owner reviews the measured results and explicitly approves any proposed
product or operational thresholds. Until then, threshold policy remains
inactive and release approval remains absent.

Add one pure decision seam to the existing release-gate package. It consumes a
versioned approved policy, compatible validated benchmark and operational
measurements, and the existing mandatory safety-gate results. It does not
execute retrieval, generation, or a benchmark. Hard safety invariants run first
and cannot be changed by product thresholds.

## User Stories

1. As the Product Owner, I want each threshold decision tied to measured real
   baseline evidence, so that product policy reflects observed R1 behavior.
2. As the Product Owner, I want to approve or reject each proposed policy
   explicitly, so that a draft can never silently become release policy.
3. As an evaluation operator, I want the baseline pinned to the approved
   dataset version and digest, so that the run is reproducible.
4. As an evaluation operator, I want observations to come from the authorized
   production Q&A application flow, so that the baseline represents the shipped
   behavior.
5. As a security owner, I want the benchmark to stop before observation when
   trusted identity, scope, current authorization, or disclosure cannot be
   verified, so that benchmark configuration cannot grant access.
6. As a data owner, I want source mappings and corpus readiness checked against
   canonical SourceRevisions and the active projection, so that stale or
   unrelated content cannot enter the evaluation.
7. As an evaluation operator, I want incomplete, denied, failed, and unavailable
   observations reported separately, so that they cannot inflate quality
   results.
8. As a Product Owner, I want retrieval recall and MRR to show their evaluator
   versions and exact eligible populations, so that threshold comparisons use
   stable semantics.
9. As a Product Owner, I want groundedness to use explicit claim-evidence
   relationships, so that citation presence is not mistaken for claim support.
10. As a Product Owner, I want answer and abstention quality to preserve their
    distinct expected-outcome populations, so that response coverage does not
    reward unsupported answers.
11. As a Product Owner, I want citation recall to include all 177 approved
    case-citation references, including supplementary citations, while keeping
    those supplementary citations out of claim-evidence relationships.
12. As an evaluation operator, I want PDF, DOCX, XLSX, outcome, and existing
    scenario strata reported where labels support them, so that aggregate scores
    do not conceal a concentrated failure cluster.
13. As an evaluation operator, I want observer and worker latency kept as
    distinct measurements, so that queue/readback time is not confused with
    worker execution time.
14. As an operator, I want cost, indexing lag, freshness, and administrator
    usefulness marked unavailable when their measurements are incomplete, so
    that absent telemetry is never treated as zero or success.
15. As a release operator, I want each metric decision to report passed, failed,
    not applicable, insufficient evidence, not approved, or unavailable, so
    that a missing measurement cannot produce a positive decision.
16. As a release operator, I want dataset, evaluator, runtime, and policy
    versions included in a deterministic metadata-only result, so that a gate
    decision can be reproduced without exposing private content.
17. As a release operator, I want mandatory safety results evaluated before
    product thresholds, so that no quality or operational score can override a
    safety failure.
18. As a maintainer, I want the gate to consume existing benchmark and
    operational contracts, so that there is one evaluator and one benchmark
    execution path.
19. As a release owner, I want benchmark qualification, threshold-gate results,
    and R1 end-to-end qualification to remain distinct, so that this gate does
    not claim completion of Issue #16.

## Implementation Decisions

- The current Product Owner is the user who initiated the task. Only that
  Product Owner's explicit, versioned approval makes a policy active.
- The approved dataset is immutable `0.3.9-user-approved`, SHA-256
  `7c2787272fd34643ff5168a46f5672b5a091f3070888546fa7b6ed0c918b3a62`.
- A local real benchmark is permitted only after the accepted Issue #453
  preflight reports every mandatory prerequisite ready. It is never run in CI.
- The benchmark reuses the authorized production Q&A composition, same-execution
  capture, canonical corpus readiness, and the existing gold-blind observer.
  No fake observer, second retrieval, or synthetic fallback may count as real.
- The deterministic release decision is a single pure seam in the existing
  release-gate package. It consumes an immutable threshold policy, a validated
  metadata-only benchmark report, required operational measurements, and
  existing hard-safety gate evidence.
- The policy binds an immutable policy version/status; dataset identity,
  version, and digest; evaluator and metric versions; metric direction and
  comparison semantics; statistic; applicable population and denominator;
  minimum coverage; runtime identity and scope constraints; and approval
  provenance.
- Draft and proposed threshold values are inactive. An absent approval yields
  `not_approved`; missing or incompatible evidence, incomplete coverage, and
  unavailable measurements cannot pass a required metric.
- Per-metric gate statuses are `passed`, `failed`, `not_applicable`,
  `insufficient_evidence`, `not_approved`, and `unavailable`.
- Denial quality remains not applicable for this dataset (`eligible_count=0`).
  Abstention quality uses 13 eligible cases. Citation Recall uses 177 expected
  case-citation references; the 19 supplementary citations do not become
  claim-evidence relationships. The approved data has 158 claim-evidence
  relationships; groundedness remains based on explicit claim links.
- Hard safety invariants remain separate, mandatory, and higher priority than
  all product and operational thresholds. No threshold can override access,
  isolation, disclosure, evidence provenance, locator validity, or safety-gate
  failure.
- The current real evaluation contract supplies retrieval, citation,
  groundedness, and answer-outcome aggregates with evaluator versions and
  denominator fields. The real benchmark report carries runtime pins and v2
  observer/worker timing samples. New report work is limited to safe strata,
  explicit missing/coverage counts, and operational measurements required by
  this decision; do not duplicate evaluator logic.
- Operational cost, indexing lag/freshness, and feedback usefulness must reuse
  existing usage, watermark, and feedback contracts. If they cannot be tied to
  a complete eligible population, report them as unavailable.
- Reports contain aggregate values, bounded reason codes, versions, coverage,
  and safe run metadata only. They omit private content, source/case identities,
  credentials, and mappings.
- Threshold values remain `decision_required` until a valid real baseline is
  reviewed. Presence in a report, issue, or configuration file is not approval.
- Passing this gate does not qualify the R1 release or close Issue #16.
- Implement as two delivery tickets: (1) metadata-only real-baseline aggregates
  and coverage; (2) versioned approved policy plus the pure release decision
  seam. Ticket 2 is blocked by ticket 1 because its evidence binding depends on
  the finalized aggregate/report contract. The local benchmark is an operator
  action after a fully ready qualification preflight, not a ticket execution
  side effect or CI job.
- The existing release-gate package currently contains the applicability gate;
  extend that boundary for this decision rather than adding another release
  evaluator. The real evaluator, real benchmark runner, #453 qualification
  contract, and operational metrics read model remain their own authorities for
  observations, benchmark execution, preflight, and operational measurement.
- Preserve the explicit state boundary: offline/synthetic contract checks prove
  deterministic contracts only; real benchmark observations prove measured
  application-flow behavior only; Product Owner approval activates policy;
  executable threshold evaluation applies that approved policy; Issue #16
  remains the final end-to-end R1 qualification.
- No public API or UI is required for this release-decision contract. It is a
  local/operator evaluation artifact until a separate product interface scope
  is approved.

## Testing Decisions

Tests assert public policy and gate outcomes, not private call order or internal
implementation shape. The highest useful seam is the pure release decision
function receiving validated report, measurement, safety, and policy contracts.

- Test minimum and maximum comparisons, exact-boundary behavior, deterministic
  decisions, denominator handling, applicability, and required coverage.
- Test missing/partial/failed/unavailable measurements, incompatible dataset,
  evaluator, metric, or runtime pins, zero denied coverage, the 13-case
  abstention population, and citation-recall inclusion of supplementary
  references.
- Test draft/proposed policy rejection, missing/invalid approval provenance,
  and explicit approval activation.
- Test that safety failure blocks quality evaluation and cannot be overridden by
  a threshold policy.
- Test report serialization contains metadata only and no private content or
  opaque source/case identifiers.
- Reuse the real evaluator, benchmark runner/qualification, operational metrics,
  applicability gate, and baseline-comparison test suites as prior art. Add no
  duplicate retrieval, generation, authorization, or evaluator tests.
- Private real benchmark execution remains an explicit local opt-in action and
  is not a default test or CI gate.

## Out of Scope

- Changing the approved Golden Dataset, its answers, evidence, mappings, or
  permission scope.
- Weakening authorization, disclosure, evidence, tenancy, or other hard safety
  invariants.
- Creating identities, grants, mappings, source revisions, or projection
  memberships to make a preflight pass.
- Adding a second evaluator, benchmark runner, retrieval/generation path, or
  telemetry platform.
- Treating synthetic evaluation, dataset validation, or qualification-contract
  tests as real production measurements.
- Automatically approving proposed thresholds or claiming R1 end-to-end
  qualification.
- Publishing private benchmark contents or running it in CI.

## Further Notes

- The Product Owner approved the local-run authorization after successful
  preflight and the proposed pure decision seam. Numerical thresholds and
  release approval remain open.
- Current local dataset structural validation passed for the pinned dataset,
  but reported `evaluation_status: not_run`. It is not a quality baseline.
- The accepted qualification runbook uses `preflight_unavailable` when no
  trusted local production preflight can be obtained. The current workspace has
  no configured PostgreSQL/local-auth/Temporal runtime values or local
  qualification composition factory, so no observation was started.
- This specification describes a benchmark gate only. It does not authorize
  implementation before ticket claim or imply threshold approval.
- The architecture and roadmap already assign numerical quality thresholds to
  a versioned evaluation baseline and release decision; this specification
  supplies that decision boundary without changing the roadmap's R1 end-to-end
  exit criteria or hard safety contracts.
