# Issue #115 local Agent Runtime qualification

Status: implemented (repository qualification; the ticket completes after its
branch is merged)

Issue #115 qualifies the local runtime seam delivered by Issues #105–#114
against AC1–AC20 from the authoritative Issue #104 specification.

## Primary behavioral contract suite

The primary behavioral contract suite is `tests/agents/test_local_runtime.py`. It exercises
the public `LocalAgentRuntime.invoke()` boundary with deterministic in-memory
fakes and observes typed requests/results, persisted Artifacts, diagnostics,
audit, cancellation, retry scheduling and idempotency effects. Focused contract
tests cover pre-invocation registration, binding resolution, trusted context
admission and the async-function adapter where those failures must occur before
`invoke()` can safely run.

This is fake-backed offline contract evidence: it is not production integration qualification.
Real model/provider qualification: not run. Real Tool Gateway,
PostgreSQL Artifact persistence, Temporal Activity and external integration
qualification remain with their owning specifications and require their own
operator-controlled prerequisites.

## Qualification outcome

- Structural handlers and explicitly adapted async functions satisfy the same
  typed request/response protocol; handler registration remains explicit and
  executable import configuration is rejected.
- Capability binding and Agent Version selection are deterministic, tenant- and
  environment-scoped, and pinned before local execution and retries.
- Typed validation, trusted execution scope, Artifact lineage, disclosure-safe
  diagnostics, cooperative cancellation, replay, partial recovery and bounded
  retries are executable offline contracts.
- The isolation evidence uses two Workspaces and two Environments for binding,
  invocation, cancellation and idempotency surfaces. Denials share safe errors
  and do not disclose protected Artifact content or existence.
- A synthetic `answer_question`-shaped handler proves generic typed Artifact and
  evidence flow without qualifying the real Q&A capability, retrieval, Cognee
  projection or a model provider. Legacy `/ask` and `/ingest` remain compatible.

## AC1–AC20 traceability

| Criterion | Executable evidence | Qualified behavior |
| --- | --- | --- |
| AC1 | `tests/agents/test_local_runtime.py::test_valid_artifact_handoff_persists_lineage_before_reporting_success` | A structural handler receives a typed request and complete pinned context through one `invoke()` boundary. |
| AC2 | `tests/agents/test_local_runtime.py::test_adapted_async_function_persists_typed_artifact_through_runtime`<br>`tests/agents/test_async_adapter.py::test_compatible_async_function_invokes_through_structural_handler` | An ordinary typed async function is explicitly adapted, admitted and produces the same persisted Artifact/result contract through `invoke()`. |
| AC3 | `tests/agents/test_handler_registry.py::test_registry_rejects_duplicate_capability_registration`<br>`tests/agents/test_handler_registry.py::test_registry_rejects_missing_or_invalid_factories_and_handlers`<br>`tests/agents/test_handler_registry.py::test_registry_rejects_executable_import_path_keys` | Duplicate, invalid, ambiguous and executable-path registrations fail before execution. |
| AC4 | `tests/agents/test_handler_registry.py::test_resolver_rejects_missing_unknown_or_disabled_selection`<br>`tests/agents/test_handler_registry.py::test_resolver_does_not_infer_binding_priority_winner`<br>`tests/agents/test_handler_registry.py::test_resolver_rejects_cross_scope_candidates_without_fallback` | Binding resolution is explicit and fails closed for missing, unknown, conflicting, unauthorized or cross-scope candidates. |
| AC5 | `tests/agents/test_local_runtime.py::test_explicitly_eligible_transient_failure_retries_with_pinned_identity`<br>`tests/agents/test_handler_registry.py::test_resolver_pins_resolution_against_later_catalog_mutation` | The resolved Agent Version remains pinned across local attempts and later catalog changes. |
| AC6 | `tests/agents/test_local_runtime.py::test_unusable_input_artifact_fails_without_handler_or_data_leak`<br>`tests/agents/test_local_runtime.py::test_incompatible_handler_output_is_not_persisted`<br>`tests/agents/test_runtime_contracts.py::test_schema_identity_must_match_capability_but_unknown_versions_remain_structural` | Malformed, missing, extra and incompatible input/output fields fail at their typed boundary without invalid persistence; invalid input never invokes the handler. |
| AC7 | `tests/agents/test_execution_context_construction.py::test_factory_rejects_scope_substitution_without_disclosure`<br>`tests/agents/test_execution_context_construction.py::test_factory_preserves_diagnostics_lineage_deadline_and_live_ports` | Trusted identity/scope is immutable, validated and preserves trace, deadline and live execution ports. |
| AC8 | `tests/agents/test_local_runtime.py::test_unusable_input_artifact_fails_without_handler_or_data_leak`<br>`tests/agents/test_local_runtime.py::test_inaccessible_artifact_uses_same_safe_failure_as_missing_artifact` | Missing, inaccessible, stale, tombstoned and cross-scope inputs fail without content or existence disclosure. |
| AC9 | `tests/agents/test_local_runtime.py::test_valid_artifact_handoff_persists_lineage_before_reporting_success` | Output Artifacts preserve tenant/work-item scope, schema, producer version, evidence and every upstream Artifact ID. |
| AC10 | `tests/agents/test_local_runtime.py::test_invalid_persistence_receipt_cannot_appear_as_success`<br>`tests/agents/test_local_runtime.py::test_unpersisted_proposed_action_reference_is_rejected` | `AgentResult` exposes only persisted references and validated safe metadata. |
| AC11 | `tests/agents/test_local_runtime.py::test_success_emits_separate_registered_diagnostics_and_audit_events`<br>`tests/agents/test_local_runtime.py::test_failure_diagnostics_reject_negative_disclosure_corpus` | Events retain stable scope/run/version/trace fields and reject protected content and raw failures. |
| AC12 | `tests/agents/test_local_runtime.py::test_success_emits_separate_registered_diagnostics_and_audit_events`<br>`tests/agents/test_local_runtime.py::test_required_audit_failure_blocks_execution_with_safe_error` | The runtime consumes shared Issue #4 Diagnostic Event, Structured Error and Audit Event contracts. |
| AC13 | `tests/agents/test_local_runtime.py::test_handler_cancellation_emits_cancelled_without_reporting_success`<br>`tests/agents/test_local_runtime.py::test_repeated_cancel_request_is_idempotent`<br>`tests/agents/test_local_runtime.py::test_cancel_request_rejects_every_cross_run_scope`<br>`tests/agents/test_local_runtime.py::test_cancellation_isolated_between_agent_runs` | Cancellation is cooperative, idempotent and isolated by Workspace, Environment, step and Agent Run. |
| AC14 | `tests/agents/test_local_runtime.py::test_equivalent_repeat_replays_one_stable_result_without_side_effects` | Equivalent at-least-once invocation replays one stable result without repeating handler or Artifact effects. |
| AC15 | `tests/agents/test_local_runtime.py::test_same_key_with_different_semantics_returns_safe_conflict`<br>`tests/agents/test_local_runtime.py::test_same_key_with_different_validated_input_returns_safe_conflict`<br>`tests/agents/test_local_runtime.py::test_same_key_with_different_runtime_identity_returns_safe_conflict`<br>`tests/agents/test_execution_context_construction.py::test_factory_rejects_scope_substitution_without_disclosure`<br>`tests/agents/test_local_runtime.py::test_same_raw_key_is_isolated_between_two_tenants` | Within one tenant scope, changed version/capability/run/input semantics conflict; scope substitution is rejected, while the same raw key in two independently admitted tenant scopes cannot replay, expose or overwrite the other result. |
| AC16 | `tests/agents/test_local_runtime.py::test_lost_checkpoint_ack_recovers_without_handler_repeat`<br>`tests/agents/test_local_runtime.py::test_artifact_failure_retry_creates_one_logical_output` | Checkpoint and persistence partial failures recover one logical output without a second handler effect. |
| AC17 | `tests/agents/test_local_runtime.py::test_incompatible_handler_output_is_not_persisted`<br>`tests/agents/test_local_runtime.py::test_cross_scope_invocation_fails_before_artifact_loading`<br>`tests/agents/test_local_runtime.py::test_handler_cancellation_emits_cancelled_without_reporting_success`<br>`tests/agents/test_local_runtime.py::test_explicit_handler_error_is_preserved_without_persistence`<br>`tests/agents/test_local_runtime.py::test_ambiguous_external_effect_never_retries`<br>`tests/agents/test_local_runtime.py::test_unlisted_retryable_failure_never_retries` | Validation, authorization, cancellation, terminal and ambiguous-effect failures do not retry; only explicitly eligible safe technical failures do. |
| AC18 | `tests/agents/test_local_runtime.py::test_retry_exhaustion_returns_one_sanitized_terminal_result`<br>`tests/agents/test_local_runtime.py::test_temporal_style_reexecution_recovers_without_duplicate_committed_effect` | Exhaustion returns one safe terminal result; a fake Temporal-style caller owns durable re-execution and recovery. |
| AC19 | `tests/agents/test_local_runtime.py::test_legacy_invoke_runs_synthetic_answer_question_with_typed_lineage`<br>`tests/api/test_diagnostics.py::test_legacy_ask_bodies_remain_compatible_with_trace_headers`<br>`tests/api/test_diagnostics.py::test_legacy_ingest_body_remains_compatible_with_trace_header` | Synthetic Q&A traverses the generic seam while legacy Q&A/ingestion behavior and framework independence remain intact. |
| AC20 | `tests/qualification/test_issue115_qualification.py::test_issue115_traceability_maps_exactly_ac1_to_ac20`<br>`tests/qualification/test_issue115_qualification.py::test_issue115_traceability_evidence_nodes_are_collectable`<br>`tests/qualification/test_issue115_qualification.py::test_issue115_qualification_declares_runtime_workflow_ownership_boundary` | The approved deterministic offline evidence is complete, collectable and explicitly separated from provider/production qualification. |

## Runtime and workflow ownership

The local runtime owns one stateless, bounded invocation: typed validation,
Artifact lineage, idempotency, safe diagnostics, cooperative cancellation and
bounded technical retries. It may schedule only short injected delays for an
explicit allowlist of technical failures when the invocation is safe to repeat.

Temporal owns durable retries, fallback, approvals, signal waiting, long waits,
workflow recovery and selection of a new binding. The runtime neither persists
workflow retry history nor creates approvals, fallback routes or a competing
durable lifecycle. A caller/Temporal re-execution reuses the same invocation
idempotency contract and can recover a committed checkpoint without repeating
the handler effect.

## AC15 tenant-scope interpretation

Issue #104 states both that changed scope must not reuse an existing result and
that identical raw keys are scoped by Workspace and Environment at the
canonical receipt boundary. These are compatible when “scope conflict” means
an attempted scope substitution of one admitted invocation, not global
uniqueness of an opaque caller key across unrelated tenants. The trusted context
factory rejects substitution before execution. Correctly admitted tenant A and
tenant B invocations may use the same raw key, but their scope participates in
the command identity and receipt address, so neither can replay, expose or
overwrite the other's result.

## Repository verification gates

The ticket is handed off only after these exact gates pass on the final branch:

- `uv sync --extra dev`
- `uv run pytest -q`
- `uv run python -m compileall -q src tests`

## Rollout and rollback

This ticket changes qualification evidence, not runtime deployment behavior or
stored data. Rollout merges the suite and document with the already delivered
runtime seam. Rollback reverts this qualification commit; it requires no data
migration and does not change the runtime contracts qualified by Issues
#105–#114.
