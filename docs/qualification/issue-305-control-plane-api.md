# Issue #305 R1 control-plane API qualification

Status: **qualified offline with the disposable PostgreSQL harness; the web
client gate remains required CI evidence; production approval is not granted**.

Issue #305 qualifies the administrator-only `/api/v1` surface assembled for
Issue #26. The executable traceability checks are in
`tests/qualification/test_issue305_qualification.py`; the machine-readable
result is `docs/qualification/issue-305-control-plane-api-report.json`.

The qualification covers the generated-contract-compatible routes for
documents and revisions, uploads and ingestion history, projection status and
recovery, sessions and messages, Ask, runs and evidence, citation inspection,
feedback, parsed content, health/configuration summaries, structured failures,
legacy compatibility and resumable SSE. It consumes the existing application
and persistence contracts; it does not create a second API authority.

## Qualification boundary

The offline gate proves deterministic behavior with ASGI and port fakes for:

- scope-bound authorization, administrator-without-content-grant behavior and
  workspace/environment isolation;
- bounded and tamper-resistant cursors, idempotent mutation replay/conflict,
  tombstones, supersession and stale/degraded/unpublished publication;
- all six answer outcomes, pinned run/attempt lineage, citation evidence
  validation, disclosure revalidation and whole-answer suppression;
- ordered SSE, `Last-Event-ID` resume, deduplication, heartbeat retry, terminal rehydration,
  cancellation races, authorization changes and candidate-output suppression;
- safe diagnostics, legacy `/ask` and `/ingest` compatibility, persistence
  contracts, generated OpenAPI export and the domain framework boundary.

It does not treat deterministic fakes as evidence for a configured production
PostgreSQL deployment, a real provider, or the broader R1 release.

## AC1–AC8 traceability

| Criterion | Test level | Executable evidence |
| --- | --- | --- |
| AC1 | api-surface-and-generated-contract | `tests/api/test_openapi_handoff.py::test_generation_export_matches_backend_openapi_document`<br>`tests/api/test_contracts.py::test_response_envelopes_are_versioned_scoped_closed_and_immutable`<br>`tests/api/test_authorization.py::test_protected_route_openapi_declares_bearer_and_required_scope_headers`<br>`tests/api/test_documents.py::test_authorized_document_collection_returns_only_safe_operational_metadata`<br>`tests/api/test_ingestion.py::test_authorized_upload_returns_stable_status_reference_and_streams_chunks`<br>`tests/api/test_projection_control.py::test_projection_status_is_scoped_and_recovery_returns_typed_command_result`<br>`tests/api/test_qa_sessions.py::test_session_create_replay_and_message_read_use_stable_scoped_contracts`<br>`tests/api/test_authorized_ask.py::test_authorized_ask_returns_only_accepted_answer_and_citation_references`<br>`tests/api/test_qa_runs.py::test_run_detail_exposes_pinned_attempt_metadata_and_safe_timeline`<br>`tests/api/test_feedback.py::test_feedback_route_records_exact_citation_feedback`<br>`tests/api/test_health_summary.py::test_administrator_health_summary_is_scoped_and_discloses_only_safe_metadata` |
| AC2 | authorization-and-tenant-environment-isolation | `tests/api/test_authorization.py::test_valid_bearer_resolves_trusted_actor_scope_and_trace`<br>`tests/api/test_authorization.py::test_missing_or_foreign_scope_returns_safe_403_before_handler`<br>`tests/api/test_documents.py::test_missing_and_foreign_documents_fail_without_enumeration`<br>`tests/api/test_ingestion.py::test_cross_workspace_upload_is_denied_without_leaking_service_or_content`<br>`tests/api/test_batch_ingestion.py::test_valid_second_workspace_cannot_observe_first_workspace_run`<br>`tests/api/test_ingestion_history.py::test_history_denies_foreign_scope_without_enumerating_rows`<br>`tests/api/test_projection_control.py::test_projection_control_rejects_foreign_scope_before_service_execution`<br>`tests/api/test_qa_sessions.py::test_administrator_without_content_grant_receives_metadata_only_message`<br>`tests/application/test_citation_inspector.py::test_citation_identity_isolated_between_workspaces_and_environments`<br>`tests/api/test_feedback.py::test_feedback_denies_foreign_scope_and_unknown_target_without_enumeration`<br>`tests/api/test_health_summary.py::test_health_summary_isolated_for_two_authorized_workspace_environments` |
| AC3 | pagination-idempotency-and-lifecycle | `tests/api/test_contracts.py::test_cursor_is_bound_to_scope_operation_limit_and_filters`<br>`tests/api/test_contracts.py::test_idempotency_key_is_bounded_and_rejects_ambiguous_values`<br>`tests/api/test_documents.py::test_document_cursor_is_bound_to_exact_filters`<br>`tests/api/test_documents.py::test_revision_history_is_cursor_paginated_and_cursor_tampering_fails_closed`<br>`tests/api/test_ingestion.py::test_same_upload_replays_with_stable_identity`<br>`tests/api/test_ingestion.py::test_same_upload_key_with_changed_content_returns_safe_conflict`<br>`tests/api/test_batch_ingestion.py::test_same_batch_replays_with_stable_run_id`<br>`tests/api/test_batch_ingestion.py::test_same_idempotency_key_with_changed_content_conflicts`<br>`tests/api/test_ingestion_history.py::test_history_cursor_is_scope_bound_and_replay_safe`<br>`tests/api/test_projection_control.py::test_projection_recovery_is_unavailable_when_worker_service_is_not_composed`<br>`tests/api/test_qa_sessions.py::test_message_route_requires_idempotency_and_safe_not_found`<br>`tests/api/test_authorized_ask.py::test_authorized_ask_same_key_replays_stable_result`<br>`tests/api/test_feedback.py::test_feedback_route_records_safe_result_and_replays_idempotently`<br>`tests/api/test_parsed_content.py::test_parsed_content_ready_response_has_typed_locator_and_scope_bound_cursor`<br>`tests/api/test_documents.py::test_document_detail_represents_tombstone_processing_and_stale_degraded_publication` |
| AC4 | qa-outcomes-lineage-and-disclosure | `tests/api/test_authorized_ask.py::test_authorized_ask_maps_each_terminal_outcome_without_renaming`<br>`tests/api/test_qa_runs.py::test_run_detail_returns_metadata_only_and_safe_not_found`<br>`tests/api/test_citation_inspector.py::test_citation_inspector_uses_safe_error_for_unavailable_content`<br>`tests/application/test_answer_outcomes.py::test_application_resolver_maps_typed_validator_fake_without_diagnostic_detail`<br>`tests/application/test_answer_question_context.py::test_revalidation_failure_blocks_pre_disclosure_and_never_returns_answered`<br>`tests/application/test_disclosure_revalidation.py::test_current_requester_revocation_suppresses_history_without_content_reason`<br>`tests/application/test_citation_inspector.py::test_fabricated_citation_identity_or_digest_fails_closed_without_excerpt`<br>`tests/application/test_qa_sessions.py::test_message_content_is_hidden_when_current_disclosure_is_revoked` |
| AC5 | resumable-sse-and-terminal-safety | `tests/api/test_qa_stream.py::test_sse_streams_accepted_ask_events_and_resumes_after_last_event_id`<br>`tests/api/test_qa_stream.py::test_sse_revalidates_protected_events_and_emits_only_safe_denial`<br>`tests/api/test_qa_stream.py::test_sse_rejects_a_cursor_from_another_message_without_event_leakage`<br>`tests/application/test_qa_streaming.py::test_publishing_safe_ask_result_creates_resumable_terminal_stream`<br>`tests/application/test_qa_streaming.py::test_publishing_same_ask_result_is_idempotent_and_does_not_duplicate_terminal`<br>`tests/application/test_qa_streaming.py::test_retry_after_partial_event_persistence_has_one_logical_effect`<br>`tests/application/test_qa_streaming.py::test_completion_wins_a_later_cancellation_without_a_second_terminal_event`<br>`tests/application/qa_events/test_event_log.py::test_stream_rehydrates_terminal_event_after_disconnect`<br>`tests/application/qa_events/test_event_log.py::test_heartbeat_retry_is_idempotent_after_reconnect`<br>`tests/application/qa_events/test_event_log.py::test_authorization_change_replaces_protected_emission_with_safe_denial`<br>`tests/application/qa_events/test_event_log.py::test_stream_rejects_candidate_output_before_the_disclosure_gate` |
| AC6 | diagnostics-and-legacy-compatibility | `tests/api/test_diagnostics.py::test_unexpected_api_failure_is_generic_and_keeps_request_trace`<br>`tests/api/test_diagnostics.py::test_invalid_cursor_uses_safe_structured_400_without_cursor_details`<br>`tests/api/test_diagnostics.py::test_legacy_ask_bodies_remain_compatible_with_trace_headers`<br>`tests/api/test_diagnostics.py::test_legacy_ingest_body_remains_compatible_with_trace_header`<br>`tests/api/test_health_summary.py::test_public_readiness_timeout_returns_the_existing_safe_failure_shape`<br>`tests/api/test_ingestion.py::test_status_exposes_acceptance_processing_and_receipt_without_content`<br>`tests/api/test_batch_ingestion.py::test_batch_polling_returns_all_items` |
| AC7 | persistence-evaluation-and-architecture-gates | `tests/application/persistence/test_idempotency_replay_authorization.py::test_replay_revalidates_current_authorization_and_stores_only_reference`<br>`tests/application/persistence/test_answer_question_runs.py::test_context_bundle_lineage_belongs_to_exact_answer_question_attempt`<br>`tests/contracts/persistence/test_idempotency_repository_contract.py::test_independent_workspaces_environments_and_keys_do_not_collide`<br>`tests/contracts/persistence/test_ingestion_repository_contract.py::test_ingestion_run_item_contract`<br>`tests/infrastructure/persistence/test_answer_question_run_schema.py::test_answer_question_run_mapping_keeps_scope_idempotency_and_safe_payload`<br>`tests/infrastructure/persistence/test_answer_disclosure_schema.py::test_answer_disclosure_mapping_is_tenant_scoped_and_payload_backed`<br>`tests/evals/test_qa_r1_dataset.py::test_r1_dataset_manifest_is_ordered_for_replay`<br>`tests/evals/test_regression_gate.py::test_offline_regression_gate_passes_reproducible_r1_contract`<br>`tests/test_architecture_scaffold.py::ArchitectureScaffoldTests::test_domain_layer_has_no_framework_imports` |
| AC8 | qualification-boundaries-and-reporting | `tests/qualification/test_issue305_qualification.py::test_issue305_report_is_machine_readable_and_separates_readiness_claims`<br>`tests/qualification/test_issue305_qualification.py::test_issue305_report_names_external_prerequisites_and_native_dependencies`<br>`tests/qualification/test_issue305_qualification.py::test_issue305_parent_relationship_remains_open_and_blockers_are_closed` |

## Verification and external gates

The focused offline command is:

```text
uv run pytest -q tests/qualification/test_issue305_qualification.py
```

The repository handoff additionally requires:

```text
uv sync --extra dev
uv run pytest -q
uv run python -m compileall -q src tests
pnpm web:api:check
pnpm web:typecheck
pnpm web:build
pnpm web:test
pnpm web:test:e2e
```

The PostgreSQL evidence passed in the local full gate using Docker/Testcontainers;
CI repeats the mandatory gate using `SPINE_TEST_DATABASE_URL` or its disposable
database service. The web commands verify the generated OpenAPI/client handoff
and browser contract. Neither gate is substituted by a local fake when its
prerequisite is unavailable.

The configured Context Broker/Cognee provider and real model/provider remain
opt-in integrations. Issue #26 remains open; this artifact does not approve
the broader R1 release or downstream UI work.
