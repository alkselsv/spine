# Issue #357 grounded answers, citations and abstention

Status: **qualified offline against deterministic G01 application-flow
observations; real Cognee, LLM and production-provider qualification is not
claimed**.

The existing `OfflineEvaluationRunner` remains the only runner and report source
of truth. Production-contract observations continue through
`r1-production-contract-observation:v1`. The report now carries a separate
`g03-grounded-answer-safety:v1` result for deterministic outcome, citation and
claim-support failures. These safety results fail the case independently of
quality aggregates and contain only bounded codes.

## Acceptance coverage

| Requirement | Executable evidence |
| --- | --- |
| Claims have exact, current citation evidence and supported coverage | `tests/evals/test_grounded_answer_safety.py::test_grounded_answer_safety_accepts_exact_current_citation_and_claim_support`; `tests/evals/test_grounded_answer_safety.py::test_grounded_answer_safety_rejects_unsupported_answer_claim`; `tests/evals/test_production_contract.py::test_production_contract_rejects_unattributed_answer_prose`; `tests/application/test_semantic_support.py::test_citation_presence_does_not_substitute_for_semantic_support`; `tests/domain/capabilities/test_answer_evaluation.py::test_explicit_coverage_gap_without_fallback_rejects_answer` |
| Source revision, digest, locator and bundle lineage are exact | `tests/evals/test_grounded_answer_safety.py::test_grounded_answer_safety_accepts_exact_current_citation_and_claim_support`; `tests/application/test_citation_validation.py::test_fabricated_or_wrong_generation_citation_fails_closed_without_source_details`; `tests/evals/test_production_contract.py::test_production_contract_observation_rejects_stale_persisted_bundle` |
| Fabricated, stale, inaccessible and wrong-locator evidence cannot pass | `tests/application/test_evidence_lineage_verification.py::test_fabricated_evidence_digest_is_rejected`; `tests/evals/test_retrieval_replay.py::test_stale_projection_abstains_until_rebuild_selects_current_revision`; `tests/application/test_citation_validation.py::test_fabricated_or_wrong_generation_citation_fails_closed_without_source_details`; `tests/evals/test_grounded_answer_safety.py::test_grounded_answer_safety_rejects_inaccessible_citation`; `tests/evals/test_grounded_answer_safety.py::test_grounded_answer_safety_rejects_wrong_locator`; `tests/evals/test_production_contract.py::test_production_contract_observation_rejects_stale_persisted_bundle` |
| Insufficient evidence and unresolved conflicts are typed no-content outcomes | `tests/evals/test_grounded_answer_safety.py::test_grounded_answer_safety_preserves_typed_non_answer_outcomes`; `tests/evals/test_production_contract.py::test_production_contract_grounded_safety_preserves_unresolved_conflict_abstention`; `tests/domain/capabilities/test_answer_disclosure.py::test_unsupported_semantics_return_safe_non_content_result`; `tests/evals/test_production_contract.py::test_production_contract_mapping_preserves_non_answer_outcome` |
| Disclosure must contain the accepted, bundle-pinned answer validation | `tests/evals/test_production_contract.py::test_answered_output_requires_an_accepted_disclosure_record`; `tests/domain/capabilities/test_answer_disclosure.py::test_grounded_answer_requires_all_validation_and_current_authority_facts` |
| Safety failure is independent of quality metrics | `tests/evals/test_production_contract.py::test_production_contract_grounded_safety_fails_independently_of_quality_evaluators` |
| G01 observations, not dataset gold answers, are the observed values | `tests/evals/test_production_contract.py::test_production_contract_mode_uses_observed_output_without_expected_answer`; `tests/evals/test_production_contract.py::test_production_contract_bridge_exception_is_safe_and_non_disclosing` |
| Denial and non-answer disclosure does not leak protected content | `tests/evals/test_unauthorized_retrieval_safety.py::test_safety_gate_accepts_denied_observation_without_source_disclosure`; `tests/evals/test_production_contract.py::test_production_contract_bridge_exception_is_safe_and_non_disclosing` |

## Verification

```text
uv run pytest -q tests/evals/test_grounded_answer_safety.py tests/evals/test_production_contract.py tests/evals/test_offline_runner.py
uv run python -m compileall -q src tests
uv run pytest -q
```

The production-contract bridge uses deterministic fakes and requires no network,
provider credentials or real LLM execution. Real Cognee, LLM and production
provider qualification remain outside this ticket.
