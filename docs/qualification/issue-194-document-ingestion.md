# Issue #194 document-ingestion qualification

Status: **passed for the supported local production profile**.

This gate qualifies the complete Issue #3 ingestion lifecycle assembled from
the single-file, batch, canonical lifecycle, original-storage, parser, and
durable-outbox slices. The evidence is executable and keeps each criterion
traceable to a public seam. PostgreSQL and filesystem entries are real adapter
tests; the offline entries use deterministic fakes where the provider or an
external service is not part of this ticket.

The gate does not qualify ContextProjection, Cognee as a production provider,
Q&A, the Knowledge UI, or the complete R1 release. Those boundaries remain
explicit so this report cannot be read as provider or product-release approval.

## Evidence matrix

| Criterion | Evidence |
| --- | --- |
| AC1 | `tests/application/test_document_ingestion.py::test_document_ingestion_completes_each_r1_format_with_parser_metadata`, `tests/ingestion/test_pdf_parser.py::test_pdf_locator_round_trip_reopens_the_exact_extracted_page_range`, `tests/ingestion/test_docx_parser.py::test_docx_nested_tables_and_duplicate_text_have_exact_structural_paths`, `tests/domain/artifacts/test_xlsx_parser.py::test_xlsx_parser_preserves_physical_coordinates_and_resolves_exact_cells` |
| AC2 | `tests/application/test_batch_ingestion.py::test_batch_keeps_one_run_and_exposes_independent_partial_outcomes`, `tests/api/test_batch_ingestion.py::test_batch_upload_streams_files_and_exposes_independent_outcomes`, `tests/api/test_batch_ingestion.py::test_batch_polling_returns_all_items` |
| AC3 | `tests/application/test_document_ingestion.py::test_document_ingestion_completes_text_path_and_replays_stably`, `tests/application/test_document_ingestion.py::test_projection_request_is_not_visible_when_its_transaction_fails`, `tests/application/test_document_ingestion.py::test_stale_revision_is_rejected_before_parser_and_projection_request`, `tests/application/outbox/test_dispatch.py::test_projection_request_redelivery_has_one_logical_consumer_effect`, `tests/application/outbox/test_dispatch.py::test_concurrent_projection_request_dispatch_has_one_logical_consumer_effect`, `tests/contracts/persistence/test_idempotency_repository_contract.py::test_independent_workspaces_environments_and_keys_do_not_collide`, `tests/integration/object_storage/test_filesystem.py::test_concurrent_finalizers_publish_one_complete_immutable_object` |
| AC4 | `tests/application/test_source_lifecycle.py::test_deletion_records_observation_and_delegates_canonical_transition_atomically`, `tests/application/test_source_lifecycle.py::test_reappearance_requires_existing_tombstone_and_fresh_canonical_evidence`, `tests/application/test_source_lifecycle.py::test_repeating_same_lifecycle_command_returns_delegated_replay_without_new_effect`, `tests/application/persistence/test_canonical_acceptance.py::test_canonical_acceptance_advances_head_and_replays_without_second_effect`, `tests/contracts/persistence/test_canonical_acceptance_contract.py::test_canonical_rejects_stale_expected_current_without_success_effect` |
| AC5 | `tests/application/test_document_ingestion.py::test_projection_request_payload_rejects_content_bearing_fields`, `tests/application/test_document_ingestion.py::test_projection_request_is_not_visible_when_its_transaction_fails`, `tests/application/outbox/test_dispatch.py::test_projection_request_redelivery_has_one_logical_consumer_effect`, `tests/application/outbox/test_dispatch.py::test_concurrent_projection_request_dispatch_has_one_logical_consumer_effect`, `tests/api/test_batch_ingestion.py::test_batch_polling_returns_all_items` |
| AC6 | `tests/api/test_batch_ingestion.py::test_cross_workspace_upload_is_denied_before_parsing_or_service`, `tests/api/test_batch_ingestion.py::test_cross_workspace_status_is_denied_before_service`, `tests/api/test_batch_ingestion.py::test_valid_second_workspace_cannot_observe_first_workspace_run`, `tests/application/test_content_authorization.py::test_revoked_current_policy_is_denied_on_next_decision_without_cache`, `tests/application/test_content_authorization.py::test_changed_authority_generation_requires_reauthorization`, `tests/integration/object_storage/test_filesystem.py::test_corrupt_and_missing_finalized_bytes_fail_closed`, `tests/api/test_diagnostics.py::test_unexpected_api_failure_is_generic_and_keeps_request_trace` |
| AC7 | `tests/api/test_diagnostics.py::test_legacy_ingest_body_remains_compatible_with_trace_header`, `tests/domain/artifacts/test_xlsx_parser.py::test_parse_document_selects_xlsx_adapter_without_using_legacy_loader` |
| AC8 | `tests/qualification/test_issue194_qualification.py::test_issue194_postgresql_evidence_is_mandatory_and_real`, `tests/integration/postgresql/test_postgresql_outbox_delivery.py::test_postgresql_concurrent_workers_do_not_overlap_claims`, `tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_adapter_satisfies_ingestion_repository_contract`, `tests/integration/object_storage/test_filesystem.py::test_interrupted_upload_is_resumable_after_adapter_restart`, `tests/api/test_batch_ingestion.py::test_batch_openapi_declares_bounded_multipart_and_polling_contract`, `tests/test_architecture_scaffold.py::ArchitectureScaffoldTests::test_domain_layer_has_no_framework_imports` |
| AC9 | `tests/qualification/test_issue194_qualification.py::test_issue194_report_names_unexercised_external_integrations` |

## Verification commands

The mandatory repository gates are:

```bash
uv sync --extra dev --locked
uv run pytest -q
uv run python -m compileall -q src tests
git diff --check
```

The PostgreSQL integration suite is mandatory in CI and requires the test
database/container prerequisite. If it cannot start, the result is an
infrastructure block, not a passed production qualification. The local
qualification also exercises the filesystem adapter, API contracts, parser
contracts, security/authorization cases, and domain-boundary checks.

Unexercised external integrations are Cognee provider qualification and any
live external identity or connector service. They are not hidden behind this
ticket's passed status and require their own opt-in qualification gates.
