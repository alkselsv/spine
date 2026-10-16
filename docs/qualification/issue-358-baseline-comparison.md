# Issue #358 versioned baseline comparison

Comparison policy: `r1-baseline-comparison:v1`, implemented by
`spine.evals.baseline_comparison.compare_r1_reports` over the existing
`OfflineEvaluationReport` contract.

The report compares evaluator results by stable case ID, evaluator key and
version, and metric semantics. Dataset ID/version and schema, report/runner
versions, execution protocol, evaluator catalog and evaluation configuration
must match. A dataset digest change with unchanged case definitions is
incomparable. Added, removed and changed case definitions are listed explicitly;
their per-case statuses and both aggregate denominators remain visible.

Prompt, model-profile and retrieval-implementation versions are recorded for
both reports. They may differ without making a comparison incomparable. Safety
violations appear in a separate list and set `safety_gate_failed` independently
of metric deltas. Failure clusters contain bounded identifiers and fixed reason
codes; they do not copy failure messages, answers, source excerpts or evidence.

## Coverage

| Requirement | Executable evidence |
| --- | --- |
| Component versions can change while comparison remains deterministic | `tests/evals/test_baseline_comparison.py::test_component_version_changes_remain_comparable_and_have_deterministic_deltas` |
| Added, removed and changed cases are explicit, and denominators account for additions | `tests/evals/test_baseline_comparison.py::test_case_changes_are_explicit_and_aggregate_denominators_are_visible` |
| Recall cutoffs retain distinct metric semantics | `tests/evals/test_baseline_comparison.py::test_metric_semantics_distinguish_recall_cutoffs` |
| Missing baseline, protocol, dataset and evaluator incompatibilities are explicit | `tests/evals/test_baseline_comparison.py::test_missing_baseline_and_incompatible_protocol_are_explicit`; `tests/evals/test_baseline_comparison.py::test_dataset_and_evaluator_semantic_changes_are_incomparable` |
| Protected failure text is rejected and cannot escape cluster output; safety remains a gate failure | `tests/evals/test_baseline_comparison.py::test_failure_clusters_omit_protected_text_and_safety_failures_stay_separate` |
| Duplicate case IDs and invalid scores are rejected | `tests/evals/test_baseline_comparison.py::test_duplicate_case_definitions_and_invalid_metric_values_are_rejected` |

This comparison does not define quality thresholds, select a product release
gate, or run real providers.
