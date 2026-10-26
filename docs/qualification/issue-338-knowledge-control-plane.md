# Issue #338 Knowledge Control Plane qualification

Status: **qualified at contract-test, fake-backed UI and offline ASGI API
boundaries; production qualification is not granted**.

Issue #338 assembles the already delivered Knowledge document slices into one
controlled administrator journey. The executable traceability checks are in
`tests/qualification/test_issue338_qualification.py`; the machine-readable
result is `docs/qualification/issue-338-knowledge-control-plane-report.json`.

## Qualification boundary

The controlled Playwright journey uses the production web build, router,
generated client, query cache and scope/authentication contexts. API responses
are deterministic route fixtures. It therefore qualifies fake-backed UI
behavior, not a deployed API or provider. The Python API evidence calls FastAPI
in-process through ASGI with application-port fakes and qualifies the public API
contract and authorization boundary offline. It does not qualify production
PostgreSQL, parsers on untrusted real documents, the configured Cognee projection
stack, a real model provider or a deployed environment.

The journey covers discovery, multi-file upload, polling, partial outcomes,
idempotent retry/replay, detail and immutable revision navigation, protected
locator inspection, projection freshness/health/recovery, lifecycle commands and
safe next actions. A dedicated revocation scenario first renders an authorized
exact-revision excerpt, revokes `read_content` within the same owned scope,
reloads against current authorization and proves that the prior excerpt leaves the DOM even
though the shell subject is an administrator.

## AC1–AC7 traceability

| Criterion | Test level | Executable evidence |
| --- | --- | --- |
| AC1 | controlled-browser-journey | `web/e2e/app.spec.ts::test_documents_discovery_filters_paginates_and_navigates_without_content_identity`<br>`web/e2e/app.spec.ts::test_documents_upload_reviews_batch_and_polls_processing`<br>`web/e2e/app.spec.ts::test_ingestion_run_displays_partial_outcomes_retryability_and_safe_diagnostics`<br>`web/e2e/app.spec.ts::test_document_retry_polls_durable_operation_and_reloads_its_run`<br>`web/e2e/app.spec.ts::test_document_detail_deep_link_reloads_and_preserves_scope_and_subview_navigation`<br>`web/e2e/app.spec.ts::test_document_revision_history_direct_url_reloads_exact_selected_revision`<br>`web/e2e/app.spec.ts::test_projection_status_scope_switch_reload_and_safe_degradation`<br>`web/e2e/app.spec.ts::test_projection_recovery_receipt_polls_status_until_server_reports_completion`<br>`web/e2e/app.spec.ts::test_documents_loading_state_is_named_while_the_controlled_response_is_pending`<br>`web/e2e/app.spec.ts::test_documents_empty_state_offers_a_safe_retry_action`<br>`web/e2e/app.spec.ts::test_documents_validation_failure_hides_protected_diagnostics`<br>`web/e2e/app.spec.ts::test_documents_partial_state_distinguishes_stale_degraded_and_unpublished`<br>`web/e2e/app.spec.ts::test_documents_failed_state_hides_provider_diagnostics` |
| AC2 | protected-content-disclosure | `web/e2e/app.spec.ts::test_document_content_inspection_renders_exact_revision_and_locator`<br>`web/e2e/app.spec.ts::test_revoked_content_authorization_removes_cached_excerpt_for_administrator`<br>`web/src/features/knowledge/ParsedContentInspector.test.tsx::test_denied_inspection_hides_payload_and_exposes_permission_state`<br>`tests/application/test_parsed_content_inspection.py::test_administrator_without_current_read_content_receives_no_segment` |
| AC3 | lifecycle-idempotency-and-lineage | `web/e2e/app.spec.ts::test_document_lifecycle_actions_are_scope_owned_and_preserve_tombstone_lineage`<br>`web/e2e/app.spec.ts::test_document_reprocess_uses_current_revision_and_conflict_requires_refresh`<br>`web/e2e/app.spec.ts::test_document_retry_replay_failed_cancelled_and_denied_states_are_safe`<br>`web/e2e/app.spec.ts::test_document_retry_reuses_idempotency_key_and_returns_stable_replay_receipt`<br>`tests/api/test_document_lifecycle_commands.py::test_reused_key_with_changed_lifecycle_data_returns_safe_conflict`<br>`tests/application/test_document_lifecycle_commands.py::test_tombstone_same_key_replays_without_rechecking_the_old_current_head` |
| AC4 | contracts-accessibility-and-build | `tests/api/test_openapi_handoff.py::test_generation_export_matches_backend_openapi_document`<br>`web/src/app/states.test.tsx::test_%s_state_has_a_named_safe_panel`<br>`web/src/features/knowledge/DocumentLifecycleActions.test.tsx::test_action_forms_expose_accessible_labels_without_physical_delete_control`<br>`web/src/features/knowledge/ProcessingCommandAction.test.tsx::test_retry_posts_server_marked_failure_polls_status_and_preserves_scope_in_run_link` |
| AC5 | scope-authentication-and-url-isolation | `web/e2e/app.spec.ts::test_control_plane_navigation_preserves_scope_and_generated_client_context`<br>`web/e2e/app.spec.ts::test_documents_discovery_filters_paginates_and_navigates_without_content_identity`<br>`web/e2e/app.spec.ts::test_ingestion_run_displays_partial_outcomes_retryability_and_safe_diagnostics`<br>`web/e2e/app.spec.ts::test_document_detail_deep_link_reloads_and_preserves_scope_and_subview_navigation`<br>`web/e2e/app.spec.ts::test_document_lifecycle_actions_are_scope_owned_and_preserve_tombstone_lineage`<br>`web/e2e/app.spec.ts::test_projection_status_scope_switch_reload_and_safe_degradation`<br>`web/e2e/app.spec.ts::test_authentication_denial_hides_administrator_shell_without_api_request`<br>`tests/api/test_documents.py::test_missing_and_foreign_documents_fail_without_enumeration`<br>`tests/api/test_batch_ingestion.py::test_valid_second_workspace_cannot_observe_first_workspace_run`<br>`tests/api/test_projection_control.py::test_projection_control_rejects_foreign_scope_before_service_execution` |
| AC6 | qualification-boundaries | `tests/qualification/test_issue338_qualification.py::test_issue338_report_separates_all_readiness_levels`<br>`tests/qualification/test_issue338_qualification.py::test_issue338_report_names_unexercised_external_prerequisites` |
| AC7 | parent-and-dependency-guardrails | `tests/qualification/test_issue338_qualification.py::test_issue338_parent_and_related_issues_remain_unchanged`<br>`tests/qualification/test_issue338_qualification.py::test_issue338_direct_blockers_and_api_prerequisites_are_closed` |

## Controlled state coverage

The controlled browser and React Testing Library suites distinguish loading,
empty, partial, stale, degraded, permission-denied, validation-failed, failed,
ready, cancelled, retrying, unpublished and unavailable states. Role-based
queries, named landmarks, labelled controls, live status regions and the absence
of physical-delete controls form the accessibility contract for this slice.

## Verification and prerequisites

The recorded gate commands are:

```text
corepack pnpm web:api:check
corepack pnpm web:typecheck
corepack pnpm web:build
corepack pnpm web:test
corepack pnpm web:test:e2e
uv run pytest -q tests/api tests/application/test_parsed_content_inspection.py tests/qualification/test_issue338_qualification.py
uv run pytest -q
uv run python -m compileall -q src tests
```

Issue #23 remains open. Issues #16 and #22 also remain open, while the API parent
#26 is closed. This ticket changes none of them. The nine direct UI dependencies
#326, #328, #329, #331, #333–#337 and API prerequisites #339–#344 were observed
closed on 2026-10-16.

Production approval remains withheld until the same journey is exercised against
the configured PostgreSQL-backed deployment, real parser corpus, projection
stack, provider and deployed API with production-operational prerequisites.
