# Issue #502 versioned threshold release gate

Status: implemented as a deterministic release-gate contract. No R1 numerical
threshold policy has been approved, and this artifact does not qualify R1 or
close Issue #16.

## Contract

`spine.evaluations.release_gates.thresholds` defines immutable policy, approval,
metadata-only evidence and decision records. `evaluate_threshold_policy` is a
pure function over the metadata aggregate produced by Issue #501 and typed hard
safety outcomes. It does not run benchmarks or evaluators.

A policy pins its identity/version, dataset identity/version/digest,
metric/evaluator versions, runtime identity/scope, and each metric's comparison,
statistic, denominator, expected eligible count, applicability, minimum coverage
and optional threshold. The gate checks the baseline's 149-case population
against the pinned approved dataset and each metric's eligible count against
the approved policy.

Issue #559 adds policy/evidence v2. V2 binds the canonical production runtime
profile digest/version, deployment fingerprint, commit SHA and every accepted
`RuntimeVersionPins` value. Its evidence is built from ready
`TrustedProductionRuntimePreflight` plus a completed typed benchmark report.
Legacy v1 remains parseable but cannot qualify positively. V2 requires typed
approval evidence from the trusted GitHub Decision Record verifier; the legacy
`PolicyApproval` fields are not authority evidence. The verifier checks the
numeric Product Owner actor from Issue #17, exact policy/dataset/runtime/scope,
the current unedited decision record and revocation/supersession state. See
[`issue-559-threshold-runtime-approval.md`](issue-559-threshold-runtime-approval.md)
for the decision record format and migration contract.

The gate checks all supplied mandatory safety outcomes first. Missing, failed,
unavailable or conflicting safety results prevent quality threshold evaluation.
After safety passes, incompatible dataset/runtime/evaluator pins or incomplete
population or any failed baseline observation produce `unavailable`. Partial
metric coverage remains `insufficient_evidence` even when it exceeds a policy's
minimum coverage. Metrics report their own status, value,
threshold semantics, denominator and coverage. Zero eligible population is
`not_applicable`; it cannot make an otherwise empty gate pass. Failure reasons
are bounded codes, and results contain no source or case identities.

## Threshold values and evidence

No Product Owner threshold values are checked into the repository. The decision
package records every numerical threshold as `decision_required`; synthetic
test values exercise comparison behavior only and are not product policy.
Current Issue #501 evidence reports operational cost, indexing lag, freshness
and usefulness as unavailable until linked measurements exist. Such evidence
cannot pass a required metric.

## Verification

Focused deterministic coverage is in
`tests/evals/test_release_threshold_gate.py`. It covers approval, exact min/max
boundaries, safety priority, applicability, coverage, pin compatibility,
reproducibility and metadata-only output. Repository qualification is separate
from real provider or production qualification.
