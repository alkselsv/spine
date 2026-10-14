# Issue #90 canonical source and projection lifecycle qualification

Status: **qualified by the deterministic matrix and full CI repository gate**.

Issue #90 is the cross-slice qualification for the canonical source and
projection lifecycle described by Issue #7. It composes the typed domain
contracts, application repositories and Unit of Work, audit/outbox boundaries,
ingestion and projection ledgers, PostgreSQL catalog/RLS tests, and the
already-qualified downstream document and ContextProjection slices. It adds no
second persistence implementation.

The executable matrix is
`tests/qualification/test_issue90_qualification.py`. Its machine-readable
report is
`docs/qualification/issue-90-source-projection-lifecycle-report.json`.

## Qualification boundary

The matrix covers:

- bootstrap, retained Alembic revisions, canonical source/revision and
  projection ledger catalogues;
- typed repository, Unit of Work, audit and transactional-outbox composition;
- two-workspace/two-environment isolation, authorization scope and forced RLS;
- idempotency, concurrent ownership, fault injection, rollback, tombstones,
  stale evidence and replay after a lost response;
- domain import boundaries, cross-slice traceability and repository gates.

The PostgreSQL evidence is mandatory integration evidence. It must run against
the repository's disposable PostgreSQL harness in the full CI gate; a passing
offline fake/contract run does not replace it. External Cognee, connector,
OIDC and object-storage providers remain separately qualified integrations.

## AC1–AC5 traceability

| AC | Executable evidence |
|---:|---|
| AC1 | `tests/integration/postgresql/test_initial_tenancy_migration.py::test_every_retained_revision_upgrades_to_one_final_head`<br>`tests/integration/postgresql/test_initial_tenancy_migration.py::test_initial_schema_has_named_tenancy_constraints_and_postgresql_types`<br>`tests/contracts/persistence/test_source_observation_contract.py::test_source_observation_replay_is_stable`<br>`tests/contracts/persistence/test_projection_contract.py::test_projection_candidate_seal_freezes_evidence_and_replays` |
| AC2 | `tests/contracts/persistence/test_unit_of_work_contract.py::test_explicit_commit_makes_workspace_visible`<br>`tests/contracts/persistence/test_canonical_acceptance_contract.py::test_canonical_happy_path_and_exact_replay`<br>`tests/contracts/persistence/test_outbox_writer_contract.py::test_canonical_mutation_and_intent_become_visible_only_after_commit`<br>`tests/contracts/persistence/test_audit_writer_contract.py::test_audit_failure_rolls_back_mutation_and_outbox_intent`<br>`tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_adapter_satisfies_shared_contract`<br>`tests/integration/postgresql/test_postgresql_audit.py::test_mutation_audit_and_outbox_roll_back_together_at_every_boundary` |
| AC3 | `tests/contracts/persistence/test_unit_of_work_contract.py::test_worker_and_interactive_contexts_have_same_workspace_isolation`<br>`tests/contracts/persistence/test_audit_writer_contract.py::test_audit_reader_does_not_reveal_event_to_another_workspace`<br>`tests/application/persistence/test_ingestion.py::test_ingestion_scope_mismatch_fails_without_disclosing_ledger_rows`<br>`tests/application/test_content_authorization.py::test_revoked_current_policy_is_denied_on_next_decision_without_cache`<br>`tests/application/test_content_authorization.py::test_changed_authority_generation_requires_reauthorization`<br>`tests/application/test_access_policy_activation.py::test_stale_baseline_has_no_activation_audit_or_outbox_effect`<br>`tests/memory/cognee/test_retrieval.py::test_retrieval_rejects_shadow_namespace_without_physical_call`<br>`tests/integration/postgresql/test_initial_tenancy_migration.py::test_workspace_rls_hides_and_rejects_another_workspace_rows`<br>`tests/integration/postgresql/test_initial_tenancy_migration.py::test_environment_rls_isolates_two_environments_in_one_workspace`<br>`tests/integration/postgresql/test_postgresql_audit.py::test_runtime_rls_denies_cross_workspace_audit_read_and_insert`<br>`tests/integration/postgresql/test_authorization_directory.py::test_revoked_generation_stops_next_composed_request_before_handler`<br>`tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_policy_change_wins_before_acceptance_and_stale_evidence_fails_closed` |
| AC4 | `tests/contracts/persistence/test_unit_of_work_contract.py::test_concurrent_duplicate_commit_has_one_logical_effect`<br>`tests/contracts/persistence/test_ingestion_repository_contract.py::test_ingestion_binding_race_contract`<br>`tests/contracts/persistence/test_outbox_delivery_repository_contract.py::test_concurrent_workers_cannot_claim_the_same_delivery`<br>`tests/application/persistence/test_transaction_retry.py::test_retry_restarts_complete_unit_of_work_and_commits_one_effect`<br>`tests/application/persistence/test_failure_paths.py::test_commit_copy_failure_is_atomic_and_terminal`<br>`tests/application/test_projection_result_mapping.py::test_partial_failure_retry_replays_stable_receipt_without_becoming_validation`<br>`tests/application/storage/test_reconciliation.py::test_reconciliation_reports_safe_orphan_and_preserves_restart_cursor`<br>`tests/domain/projection/test_publication_models.py::test_drift_requires_a_specific_canonical_acceptance_and_detection_source`<br>`tests/domain/projection/test_publication_models.py::test_unpublished_resolution_does_not_fabricate_freshness_or_health`<br>`tests/memory/cognee/test_rebuild.py::test_abandoned_shadow_rebuild_is_recoverable_without_becoming_readable`<br>`tests/memory/cognee/test_graph.py::test_graph_writer_failed_shadow_rebuild_does_not_change_active_namespace`<br>`tests/memory/cognee/test_recovery.py::test_repair_fences_stale_source_without_reintroducing_artifacts`<br>`tests/integration/postgresql/test_postgresql_unit_of_work.py::test_concurrent_duplicate_claims_have_one_owner_and_stable_replay`<br>`tests/integration/postgresql/test_postgresql_unit_of_work.py::test_lost_commit_response_replays_one_combined_effect`<br>`tests/integration/postgresql/test_postgresql_outbox_delivery.py::test_postgresql_concurrent_workers_do_not_overlap_claims`<br>`tests/integration/postgresql/test_postgresql_unit_of_work.py::test_access_policy_activation_has_one_concurrent_cas_winner_and_stable_replay`<br>`tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_boundary_capture_fences_acceptance_at_counter_lock`<br>`tests/integration/postgresql/test_initial_tenancy_migration.py::test_k0_deferred_validator_rollback_removes_child_and_retry_succeeds` |
| AC5 | `tests/test_architecture_scaffold.py::ArchitectureScaffoldTests::test_domain_layer_has_no_framework_imports`<br>`tests/application/test_source_lifecycle.py::test_deletion_records_observation_and_delegates_canonical_transition_atomically`<br>`tests/application/test_source_lifecycle.py::test_reappearance_requires_existing_tombstone_and_fresh_canonical_evidence`<br>`tests/application/test_projection_result_persistence.py::test_duplicate_delivery_replays_without_second_effect`<br>`tests/application/test_access_policy_activation.py::test_concurrent_successors_have_one_cas_winner`<br>`tests/qualification/test_issue154_qualification.py::test_issue154_qualification_record_covers_the_five_r1_formats`<br>`tests/qualification/test_issue211_qualification.py::test_issue211_report_is_machine_readable_and_disclaims_production_approval`<br>`tests/contracts/diagnostics/test_sink_contract.py::test_sink_emits_the_same_validated_snapshot[recording]`<br>`tests/application/diagnostics/test_errors.py::test_unknown_error_is_generic_without_inspecting_or_leaking_exception`<br>`tests/qualification/test_issue90_qualification.py::test_issue90_migration_graph_has_one_dynamic_head`<br>`tests/qualification/test_issue90_qualification.py::test_issue90_repository_gates_are_documented` |

The exact `path::test_node` evidence for each row is canonical in the
executable matrix. The matrix verifies that every node exists and is collected
by pytest; offline nodes are executed by the qualification suite, while the
PostgreSQL nodes are executed by the full repository gate.

## Verification

Focused qualification:

```text
uv run pytest -q tests/qualification/test_issue90_qualification.py
```

Repository handoff:

```text
uv sync --extra dev
uv run pytest -q
uv run python -m compileall -q src tests
```

The report records `release_gate.status: qualified` after the full CI gate;
`production_approval: false` still separates repository qualification from
operator-owned production approval. External provider and deployment
qualification remains outside this deterministic artifact.
