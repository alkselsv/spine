# Issue #211 ContextProjection offline qualification

Status: **qualified offline against deterministic fakes; production approval is
not granted**.

Issue #211 is the release gate for the complete provider-neutral
`ContextProjection` adapter contract delivered by Issues #195–#207 and #214.
The gate runs without a real Cognee process, PostgreSQL, Neo4j, PGVector or
external LLM. It combines the reusable #197 conformance suite with the
provider-shaped Cognee adapter fakes, physical artifact validator, shadow
rebuild/recovery tests and the Issue #7 result-mapping bridge.

The machine-readable result is
`docs/qualification/issue-211-context-projection-report.json`. The executable
qualification checks are in
`tests/qualification/test_issue211_qualification.py`.

## Qualification boundary

The offline gate proves deterministic contract behavior at these seams:

- write lifecycle for chunks and graph entities/relations;
- structured retrieval of chunks, entities, relations and evidence references;
- source revision, processing receipt, revision digest/number and stable-locator
  lineage round-trip;
- shadow namespace exclusion from ordinary retrieval;
- validation of missing, fabricated, stale, tombstoned, ambiguous and
  incomplete evidence;
- idempotency, command-digest conflicts, revision fencing, tombstones, replay,
  partial physical failure and recovery;
- workspace/environment/candidate isolation and disclosure-safe diagnostics;
- mapping of adapter results into canonical Issue #7 handoff inputs.

Cognee calls are represented by deterministic SDK-shaped fakes. A passing fake
qualification is not a production approval: live Cognee behavior, configured
storage backends, PostgreSQL authority, access-policy enforcement and external
provider reliability remain unverified and require their own operator-controlled
qualification.

## AC1–AC8 traceability

| Criterion | Executable evidence | Qualified behavior |
| --- | --- | --- |
| AC1 | `tests/contracts/memory/test_conformance.py::test_context_projection_contract_exposes_all_operations`<br>`tests/memory/cognee/test_indexing.py::test_indexing_preserves_revision_locator_and_namespace_provenance`<br>`tests/memory/cognee/test_graph.py::test_graph_writer_preserves_approved_artifacts_and_evidence_lineage` | The complete provider-neutral write contract and both chunk/graph physical lanes preserve typed source lineage. |
| AC2 | `tests/contracts/memory/test_conformance.py::test_retrieval_round_trips_chunks_entities_relations_and_provenance`<br>`tests/memory/cognee/test_retrieval.py::test_retrieval_maps_public_structured_search_to_provider_neutral_result`<br>`tests/application/test_projection_result_mapping.py::test_valid_adapter_result_maps_to_issue7_receipt_and_source_reference` | Structured retrieval returns chunks, entities, relations and evidence with revision, processing-receipt and stable-locator lineage; the handoff preserves it. |
| AC3 | `tests/contracts/memory/test_conformance.py::test_missing_evidence_abstains_without_returning_content`<br>`tests/memory/cognee/test_validation.py::test_duplicate_missing_and_incomplete_sets_are_deterministic`<br>`tests/memory/cognee/test_retrieval.py::test_retrieval_rejects_reference_without_physical_provenance_mapping`<br>`tests/memory/cognee/test_retrieval.py::test_retrieval_returns_typed_incomplete_result_for_missing_locator` | Missing, fabricated or incomplete evidence fails closed with typed outcomes and no projected content. |
| AC4 | `tests/contracts/memory/test_conformance.py::test_shadow_namespace_is_not_visible_to_ordinary_retrieval`<br>`tests/memory/cognee/test_retrieval.py::test_retrieval_rejects_shadow_namespace_without_physical_call`<br>`tests/memory/cognee/test_rebuild.py::test_abandoned_shadow_rebuild_is_recoverable_without_becoming_readable`<br>`tests/memory/cognee/test_graph.py::test_graph_writer_failed_shadow_rebuild_does_not_change_active_namespace` | Shadow, rebuilding and failed candidates remain isolated and cannot serve ordinary retrieval or replace the active namespace. |
| AC5 | `tests/contracts/memory/test_conformance.py::test_same_command_replays_without_a_second_logical_effect`<br>`tests/contracts/memory/test_conformance.py::test_non_current_revision_is_fenced_without_an_artifact`<br>`tests/contracts/memory/test_conformance.py::test_partial_failure_and_lost_response_are_safe_to_retry`<br>`tests/memory/cognee/test_indexing.py::test_remove_tombstones_source_and_repeated_remove_does_not_readd_content`<br>`tests/memory/cognee/test_retrieval.py::test_retrieval_does_not_return_content_when_evidence_is_stale`<br>`tests/memory/cognee/test_retrieval.py::test_retrieval_does_not_return_content_for_unsafe_evidence`<br>`tests/memory/cognee/test_recovery.py::test_repair_fences_tombstoned_source_without_reintroducing_artifacts` | Idempotent replay, revision fencing, tombstone exclusion, crash recovery and pending-effect retry produce one safe logical effect. |
| AC6 | `tests/contracts/memory/test_acl_leakage_contract.py::test_denied_scope_has_no_content_or_sensitive_diagnostic`<br>`tests/memory/cognee/test_retrieval.py::test_retrieval_fails_closed_for_cross_scope_physical_metadata`<br>`tests/memory/cognee/test_indexing.py::test_foreign_workspace_effect_is_denied_before_physical_write`<br>`tests/application/test_projection_result_mapping.py::test_foreign_workspace_effect_envelope_is_rejected` | Workspace/environment isolation is enforced at physical and mapping boundaries; denied results do not leak protected content or provider diagnostics. |
| AC7 | `tests/contracts/memory/test_backend_contract.py::test_backend_implements_every_context_projection_operation`<br>`tests/memory/cognee/test_indexing.py::test_adapter_rejects_an_unqualified_cognee_version`<br>`tests/memory/cognee/test_retrieval.py::test_public_sdk_bridge_uses_only_public_search_and_search_type` | The offline gate uses provider-shaped fakes and public-SDK-shaped seams only; no real provider is needed or silently approved. |
| AC8 | `tests/qualification/test_issue211_qualification.py::test_issue211_report_is_machine_readable_and_disclaims_production_approval`<br>`tests/qualification/test_issue211_qualification.py::test_issue211_traceability_evidence_nodes_are_collectable`<br>`tests/qualification/test_issue211_qualification.py::test_issue211_offline_adapter_suite_passes` | The report, traceability matrix and release-gate result are machine-readable, collectable and executable. |

## Verification

The focused qualification command is:

```text
uv run pytest -q tests/qualification/test_issue211_qualification.py
```

The adapter evidence exercised by the qualification is:

```text
uv run pytest -q tests/contracts/memory tests/memory/cognee tests/application/test_projection_result_mapping.py
```

Repository handoff additionally requires:

```text
uv sync --extra dev
uv run pytest -q
uv run python -m compileall -q src tests
```

No live provider integration is part of this offline release gate.
