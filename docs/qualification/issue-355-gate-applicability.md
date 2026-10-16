# Issue #355 versioned regression-gate applicability

Status: **qualified offline; provider quality confirmation is intentionally not
claimed**.

The policy is `r1-gate-applicability:v1`, implemented at the evaluation-plane
boundary in `spine.evaluations.release_gates.applicability`. It binds component versions, evaluator
catalog, dataset identity, suite versions and execution-evidence identifiers.
Unknown, stale, incomplete or inconsistent input is rejected without exposing
provider or protected evidence details.

The mandatory `r1-offline-safety:v1` suite is selected for every invocation.
`run_r1_applicability_gate` invokes the existing deterministic offline gate even
when no changed path matches a rule. Path classification can only add suites;
it cannot remove or weaken the mandatory suite.

## Coverage map

| Change class | Component pin | Additional suites |
| --- | --- | --- |
| parser / locator | `parser` | Issue #194 ingestion |
| SourceRevision | `source-revision` | Issue #194 ingestion |
| projection / publication | `projection` | Issues #90 and #211 |
| retrieval | `retrieval` | G02 and G06 |
| ACL | `acl` | G02 and G06 |
| prompt | `prompt` | offline safety |
| model | `model` | offline safety; provider quality remains not confirmed |
| capability | `capability` | G03 and G06 |
| evaluator | `evaluator` | evaluator contract and offline safety |
| dataset | `dataset` | dataset contract and offline safety |
| gate / report configuration | `gate` | applicability and offline safety |

## Executable traceability

| Criterion | Evidence |
| --- | --- |
| versioned manifest and all supported classes | `tests/evals/test_applicability.py::test_parser_change_selects_versioned_parser_coverage_and_evidence` |
| mandatory offline suite is independent of changed paths | `tests/evals/test_applicability.py::test_empty_change_set_still_runs_mandatory_offline_safety_suite` |
| unknown paths fail closed | `tests/evals/test_applicability.py::test_unmatched_changed_path_fails_closed_and_cannot_cancel_mandatory_suite` |
| stale and mismatched evaluator metadata fail closed | `tests/evals/test_applicability.py::test_stale_manifest_and_evaluator_catalog_fail_closed` |
| omitted component version pins fail closed | `tests/evals/test_applicability.py::test_missing_component_version_pin_fails_closed` |
| missing execution evidence fails closed | `tests/evals/test_applicability.py::test_missing_required_execution_evidence_fails_closed` |
| model changes do not imply provider-quality confirmation | `tests/evals/test_applicability.py::test_model_change_can_pass_offline_but_is_not_provider_quality_confirmation` |

## Verification

```text
uv run pytest -q tests/evals/test_applicability.py
uv run python -m compileall -q src tests
```

This policy does not execute real Cognee, LLM providers or product quality
thresholds. Those remain explicit downstream gate/profile concerns.
