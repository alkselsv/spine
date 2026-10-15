# Issue #269 Context Broker qualification

Status: **qualified offline against deterministic fakes; PostgreSQL remains a
mandatory external gate; production approval is not granted**.

Issue #269 is the final qualification gate for the complete authorized Context
Broker assembled for Issue #27. The offline gate exercises the public broker,
authorization, publication-pin, physical-read, provenance, bundle,
revalidation and architecture seams without a network, real Cognee provider or
PostgreSQL server.

The machine-readable result is
`docs/qualification/issue-269-context-broker-report.json`. The executable
qualification checks are in `tests/qualification/test_issue269_qualification.py`.

## Qualification boundary

The offline evidence proves:

- complete authorized retrieval through an immutable ContextBundle reference;
- closed request/response outcomes and disclosure-safe errors;
- caller-controlled publication, namespace, partition and policy authority are
  rejected or ignored;
- human/service authorization intersection, purpose and environment checks,
  mixed-access behavior and workspace isolation;
- source revision, tombstone, retention, locator and publication provenance;
- pre-retrieval source isolation and exclusion of shadow/rebuilding/failed
  namespaces;
- exact bundle identity, immutable replay, conflicting replay rejection,
  cancellation, partial retrieval, revocation and bounded regeneration;
- deterministic retrieval merge, explicit score preservation and score absence;
- framework-independent domain boundaries.

## PostgreSQL gate

The required PostgreSQL contract and race tests are included in the traceability
matrix and run as part of the repository's mandatory `postgresql` tests. This
offline qualification does not silently substitute SQLite or an in-memory fake
for that gate. Running the PostgreSQL section requires either
`SPINE_TEST_DATABASE_URL` pointing at the exact disposable test database and
marker, or a working Docker/Testcontainers runtime. If that prerequisite is not
available, the result is recorded as an unverified external prerequisite and
the offline gate remains the only claim made here.

## AC1–AC8 traceability

| Criterion | Executable evidence | Qualified behavior |
| --- | --- | --- |
| AC1 | `tests/application/test_authorized_context_broker.py::test_allowed_request_persists_exact_bundle_and_replays_without_second_read`<br>`tests/contracts/memory/test_context_broker_v1.py::test_fake_broker_issues_an_explicit_fake_bundle_for_allowed_retrieval`<br>`tests/application/persistence/test_context_bundles.py::test_context_bundle_repository_replays_exact_immutable_attempt` | The complete offline flow returns and persists one exact, replayable bundle identity. |
| AC2 | `tests/contracts/memory/test_context_broker_v1.py::test_context_request_rejects_client_selected_projection_authority`<br>`tests/contracts/memory/test_context_broker_v1.py::test_fake_broker_maps_controlled_retrieval_outcomes_safely`<br>`tests/contracts/memory/test_context_broker_v1.py::test_context_response_rejects_bundle_on_denied_outcome`<br>`tests/application/test_authorized_context_broker.py::test_unpublished_active_projection_is_safe_unavailable` | Typed requests and responses enforce safe outcomes and fail closed when publication is unavailable. |
| AC3 | `tests/application/test_authorized_retrieval_scope.py::test_mixed_access_omits_unauthorized_source_without_denied_source_leakage`<br>`tests/application/test_authorized_retrieval_scope.py::test_interactive_processing_requires_human_and_service_grants`<br>`tests/application/test_authorized_retrieval_scope.py::test_scope_rejects_wrong_purpose_and_cross_environment_without_repository_call`<br>`tests/application/test_authorized_context_broker.py::test_workspace_mismatch_denies_without_leaking_other_workspace_state`<br>`tests/application/test_attempt_revalidation.py::test_same_idempotency_key_isolated_between_workspaces`<br>`tests/contracts/memory/test_context_broker_v1.py::test_fake_broker_keeps_two_workspace_requests_isolated` | Authorization is request-time, all-contributor and tenant/environment scoped; denied source details do not leak. |
| AC4 | `tests/application/test_authorized_retrieval_scope.py::test_superseded_revision_fails_closed_instead_of_using_old_publication_member`<br>`tests/application/test_authorized_retrieval_scope.py::test_missing_retention_or_tombstoned_current_revision_fails_closed`<br>`tests/memory/test_physical_read_plan.py::test_missing_receipt_lineage_fails_closed`<br>`tests/memory/test_physical_read_plan.py::test_cross_workspace_reference_fails_closed_without_repairing_the_reference`<br>`tests/memory/test_structured_retrieval.py::test_executor_keeps_incompatible_duplicate_provenance_as_separate_items` | Every contributing result remains tied to current source and complete provenance; invalid lineage fails closed. |
| AC5 | `tests/application/test_authorized_retrieval_scope.py::test_missing_provider_safety_proof_fails_closed`<br>`tests/memory/test_physical_read_plan.py::test_isolation_failure_is_typed_and_never_produces_a_plan`<br>`tests/memory/test_physical_read_plan.py::test_non_active_namespace_is_excluded_before_provider_boundary`<br>`tests/memory/test_physical_read_plan.py::test_changed_publication_pin_cannot_mix_with_the_authorized_scope`<br>`tests/memory/test_structured_retrieval.py::test_executor_rejects_unsafe_resolution_without_provider_call` | No unsafe physical provider call or mixed publication identity is allowed. |
| AC6 | `tests/application/test_authorized_context_broker.py::test_replay_returns_immutable_bundle_without_reinvoking_profile_or_publication`<br>`tests/application/persistence/test_context_bundles.py::test_context_bundle_repository_rejects_conflicting_replay`<br>`tests/application/test_attempt_revalidation.py::test_exact_duplicate_is_idempotent`<br>`tests/application/test_attempt_revalidation.py::test_concurrent_duplicate_submissions_share_one_logical_result`<br>`tests/application/test_attempt_revalidation.py::test_policy_revocation_rejects_reuse_without_a_regeneration_plan`<br>`tests/application/test_attempt_revalidation.py::test_tombstoned_source_fails_closed_even_when_the_old_evidence_is_present` | Replay, revocation, tombstone and race behavior preserve one immutable attempt or fail safely. |
| AC7 | `tests/application/test_attempt_revalidation.py::test_cancelled_request_has_no_protected_bundle_result`<br>`tests/application/test_attempt_revalidation.py::test_partial_bundle_replay_keeps_a_typed_partial_outcome_without_evidence`<br>`tests/memory/test_structured_retrieval.py::test_executor_reports_partial_provider_failure_without_failed_lane_content`<br>`tests/memory/test_structured_retrieval.py::test_executor_propagates_cancellation_before_starting_next_partition`<br>`tests/contracts/memory/test_retrieval_contract.py::test_retrieval_score_is_optional_and_round_trips_when_provider_supplies_it` | Cancellation, partial retrieval, deterministic ordering and explicit/absent scores remain typed and safe. |
| AC8 | `tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_context_bundle_replays_isolates_and_safe_reads`<br>`tests/infrastructure/persistence/test_postgresql_publication_resolution.py::test_active_publication_resolution_uses_one_consistent_statement_snapshot`<br>`tests/integration/postgresql/test_initial_tenancy_migration.py::test_workspace_rls_hides_and_rejects_another_workspace_rows`<br>`tests/integration/postgresql/test_initial_tenancy_migration.py::test_environment_rls_isolates_two_environments_in_one_workspace`<br>`tests/integration/postgresql/test_postgresql_unit_of_work.py::test_concurrent_duplicate_claims_have_one_owner_and_stable_replay`<br>`tests/integration/postgresql/test_postgresql_unit_of_work.py::test_lost_commit_response_replays_one_combined_effect`<br>`tests/integration/postgresql/test_postgresql_unit_of_work.py::test_access_policy_activation_has_one_concurrent_cas_winner_and_stable_replay`<br>`tests/integration/postgresql/test_authorization_directory.py::test_revoked_generation_stops_next_composed_request_before_handler`<br>`tests/test_architecture_scaffold.py::ArchitectureScaffoldTests::test_domain_layer_has_no_framework_imports` | PostgreSQL authority, publication consistency, RLS, transaction/idempotency boundaries and authorization races are separately gated; domain remains framework independent. |

## Verification

The focused offline command is:

```text
uv run pytest -q tests/qualification/test_issue269_qualification.py
```

The full offline evidence command is executed by that qualification test. The
repository handoff additionally requires:

```text
uv sync --extra dev
uv run pytest -q
uv run python -m compileall -q src tests
```

The PostgreSQL tests are mandatory in the full repository gate and are not
treated as passed by this artifact when the external prerequisite is absent.
