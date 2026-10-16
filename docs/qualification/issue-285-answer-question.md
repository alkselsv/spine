# Issue #285 `answer_question` capability qualification

Status: **qualified offline against deterministic provider fakes and qualified
against the disposable PostgreSQL harness; production qualification is not
approved**.

Issue #285 qualifies the complete `answer_question` capability assembled by
Issues #276 and #280–#284. The executable matrix is
`tests/qualification/test_issue285_qualification.py`; its machine-readable
result is `docs/qualification/issue-285-answer-question-report.json`.

## Qualification boundary

The offline matrix composes the public capability, candidate/runtime, Context
Broker handoff, citation and evidence validation, disclosure revalidation,
terminal outcomes, diagnostics, Q&A persistence contracts, and downstream
artifact compatibility. It uses deterministic fakes and does not call Cognee,
an LLM provider, or a physical retrieval adapter.

The matrix explicitly keeps these boundaries:

- the production Context Broker owns retrieval authorization, ContextBundle
  persistence and projection identity;
- Issue #26 API/SSE and Issue #22 Chat/Citation Inspector consume typed accepted
  outputs and safe terminal outcomes, but are not implemented here;
- Issue #19 receives versioned evaluation inputs, while global regression gates
  and Issue #17 quality thresholds remain out of scope;
- real model/provider qualification is opt-in and is never inferred from fake
  success.

## AC1–AC8 traceability

| AC | Executable evidence |
| --- | --- |
| AC1 | `tests/domain/capabilities/test_answer_question_contracts.py::test_fake_handler_covers_every_terminal_outcome_without_a_provider`<br>`tests/application/test_answer_question_context.py::test_allowed_context_consumes_exact_bundle_and_pin_without_caller_selectors`<br>`tests/application/test_answer_question_candidate.py::test_candidate_generation_delegates_to_pinned_runtime_as_repeatable_call`<br>`tests/application/test_answer_question_candidate.py::test_candidate_generation_returns_a_persisted_result_not_a_token_stream`<br>`tests/agents/test_answer_question_candidate_runtime.py::test_local_runtime_persists_a_valid_typed_candidate_once`<br>`tests/evals/test_answer_question_contract_evaluation.py::test_answer_question_contract_evaluation_fixture_covers_closed_outcome_sets` |
| AC2 | `tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_answer_question_run_create_and_replay_contract`<br>`tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_answer_question_run_conflicting_key_contract`<br>`tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_answer_question_attempt_immutability_contract`<br>`tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_answer_question_context_bundle_reference_contract`<br>`tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_answer_question_concurrent_completion_contract`<br>`tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_answer_question_run_isolation_contract`<br>`tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_answer_question_retry_cancellation_contract`<br>`tests/integration/postgresql/test_initial_tenancy_migration.py::test_initial_schema_has_named_tenancy_constraints_and_postgresql_types`<br>`tests/integration/postgresql/test_initial_tenancy_migration.py::test_workspace_rls_hides_and_rejects_another_workspace_rows`<br>`tests/integration/postgresql/test_initial_tenancy_migration.py::test_environment_rls_isolates_two_environments_in_one_workspace` |
| AC3 | `tests/application/test_citation_validation.py::test_fabricated_or_wrong_generation_citation_fails_closed_without_source_details`<br>`tests/application/test_evidence_lineage_verification.py::test_fabricated_evidence_digest_is_rejected`<br>`tests/application/test_evidence_lineage_verification.py::test_mixed_access_multi_source_entity_is_withheld_as_a_whole`<br>`tests/application/test_evidence_lineage_verification.py::test_all_authorized_multi_source_entity_is_verified_without_dropping_contributors`<br>`tests/evals/test_retrieval_replay.py::test_stale_projection_abstains_until_rebuild_selects_current_revision`<br>`tests/evals/test_retrieval_replay.py::test_unsafe_fixture_case_returns_no_protected_evidence[tombstone]`<br>`tests/evals/test_retrieval_replay.py::test_cross_workspace_query_returns_empty_without_leaking_other_workspace`<br>`tests/application/test_semantic_support.py::test_citation_presence_does_not_substitute_for_semantic_support`<br>`tests/application/test_semantic_support.py::test_validator_failure_is_fail_closed`<br>`tests/domain/capabilities/test_answer_evaluation.py::test_proven_exhaustive_coverage_produces_complete_answer`<br>`tests/domain/capabilities/test_answer_evaluation.py::test_incomplete_coverage_with_current_fallback_produces_partial_answer`<br>`tests/domain/capabilities/test_answer_evaluation.py::test_current_grounded_conflict_is_presented_without_selecting_a_winner`<br>`tests/domain/capabilities/test_answer_evaluation.py::test_explicit_coverage_gap_without_fallback_rejects_answer` |
| AC4 | `tests/application/test_disclosure_revalidation.py::test_current_ineligible_dependency_suppresses_whole_answer`<br>`tests/application/test_disclosure_revalidation.py::test_revision_replacement_requires_revalidation_without_disclosing_old_answer`<br>`tests/application/test_disclosure_revalidation.py::test_material_later_publication_requires_revalidation`<br>`tests/application/test_disclosure_revalidation.py::test_authority_generation_change_requires_revalidation`<br>`tests/application/test_disclosure_revalidation.py::test_current_requester_revocation_suppresses_history_without_content_reason`<br>`tests/application/test_disclosure_revalidation.py::test_superseded_dependency_suppresses_whole_answer`<br>`tests/application/test_disclosure_revalidation.py::test_cancelled_revalidation_fails_closed_without_stored_answer`<br>`tests/application/test_disclosure_revalidation.py::test_interrupted_revalidation_fails_closed_without_stored_answer`<br>`tests/application/test_disclosure_revalidation.py::test_missing_dependency_fails_closed_and_does_not_reuse_stored_answer`<br>`tests/application/test_attempt_revalidation.py::test_activation_change_alone_replays_the_pinned_bundle`<br>`tests/application/test_attempt_revalidation.py::test_tombstoned_source_fails_closed_even_when_the_old_evidence_is_present`<br>`tests/application/test_attempt_revalidation.py::test_retryable_observation_is_not_cached_and_later_retry_can_replay`<br>`tests/application/test_answer_disclosure.py::test_gate_suppresses_replay_after_current_authority_revocation`<br>`tests/application/test_answer_question_context.py::test_revalidation_failure_blocks_pre_disclosure_and_never_returns_answered` |
| AC5 | `tests/agents/test_answer_question_candidate_runtime.py::test_local_runtime_rejects_candidate_before_output_checkpoint`<br>`tests/agents/test_answer_question_candidate_runtime.py::test_local_runtime_rejects_untyped_or_source_free_candidate_output`<br>`tests/domain/capabilities/test_answer_disclosure.py::test_cancellation_never_reaches_accepted_output`<br>`tests/application/test_answer_outcomes.py::test_application_resolver_maps_typed_validator_fake_without_diagnostic_detail`<br>`tests/application/diagnostics/test_errors.py::test_unknown_error_is_generic_without_inspecting_or_leaking_exception`<br>`tests/contracts/memory/test_context_broker_v1.py::test_port_errors_map_to_safe_typed_responses[validator_error]`<br>`tests/application/test_answer_question_context.py::test_denied_context_returns_safe_denial_without_loading_bundle`<br>`tests/agents/test_local_runtime.py::test_failure_diagnostics_reject_negative_disclosure_corpus`<br>`tests/application/test_semantic_support.py::test_citation_presence_does_not_substitute_for_semantic_support`<br>`tests/application/test_semantic_support.py::test_validator_unavailable_is_a_mandatory_qna_failure`<br>`tests/application/test_semantic_support.py::test_validator_failure_is_fail_closed` |
| AC6 | `tests/domain/capabilities/test_answer_question_runs.py::test_answer_question_attempt_completion_pins_context_versions_usage_and_safe_events`<br>`tests/application/test_answer_question_context.py::test_context_outcome_mapping_keeps_broker_and_answer_outcomes_distinct`<br>`tests/evals/test_answer_question_contract_evaluation.py::test_answer_question_contract_evaluation_fixture_covers_closed_outcome_sets`<br>`tests/api/test_qa_sessions.py::test_session_create_replay_and_message_read_use_stable_scoped_contracts`<br>`tests/qualification/test_issue285_qualification.py::test_issue285_downstream_web_boundary_is_explicitly_compatibility_only` |
| AC7 | `tests/evals/test_qa_r1_dataset.py::test_r1_dataset_manifest_is_ordered_for_replay`<br>`tests/evals/test_qa_r1_dataset.py::test_r1_dataset_is_synthetic_and_does_not_define_release_thresholds`<br>`tests/evals/test_answer_question_contract_evaluation.py::test_answer_question_contract_evaluation_fixture_covers_closed_outcome_sets`<br>`tests/evals/test_regression_gate.py::test_offline_regression_gate_passes_reproducible_r1_contract`<br>`tests/qualification/test_issue285_qualification.py::test_issue285_regression_gate_input_contract_is_versioned` |
| AC8 | `tests/qualification/test_issue285_qualification.py::test_issue285_report_is_machine_readable_and_separates_readiness_claims`<br>`tests/qualification/test_issue285_qualification.py::test_issue285_report_names_unverified_external_prerequisites` |

The executable matrix verifies that every evidence node exists and is
collectable. Its offline test invokes the fake-backed targets. The ten AC2
PostgreSQL nodes pass against the disposable Docker harness; production Context
Broker and disclosure-revalidator configuration remain external prerequisites.

## PostgreSQL and production prerequisites

The AC2 tests run against the disposable real PostgreSQL harness. They are not
replaced by SQLite or an in-memory fake. The successful local prerequisite was
either `SPINE_TEST_DATABASE_URL` pointing at the exact disposable test database
and marker, or a working Docker/Testcontainers runtime.

The report names the remaining unverified prerequisites: a configured
PostgreSQL-backed Context Broker bundle/revalidation path, active publication
and `as_of` authority, the configured PostgreSQL + Neo4j + PGVector projection
profile, persisted disclosure validation facts, and an opt-in real model
provider. None of these are silently treated as production qualification.

## Verification

Focused qualification:

```text
uv run pytest -q tests/qualification/test_issue285_qualification.py
```

Repository handoff:

```text
uv sync --extra dev
uv run pytest -q
uv run python -m compileall -q src tests
```

The offline result is deliberately separate from production approval and from
the release qualification owned by the parent issue.

The repository CI must also run the web unit/type/build/browser jobs for the
compatibility boundary. The controlled web fixture is not an implementation of
Issue #26; it proves only that the typed accepted-output, citation and terminal
event boundary remains consumable by the future API/SSE adapter.

The web evidence nodes are
`web/src/features/chat/protocol.test.ts::test_protocol_rejects_candidate_output_and_provider_details`
and
`web/src/features/chat/transport.test.ts::test_transport_rechecks_disclosure_and_denies_before_protected_event`.
