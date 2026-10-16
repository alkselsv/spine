# Issue #354 Context adapter parity qualification

Status: **qualified offline against deterministic fakes; production approval is
not granted**.

The parity suite runs the same normalized request and fault matrix against the
active `ContextBrokerV1`, the authorized application broker and the two active
deterministic Context Projection compositions. It compares typed outcomes and
safety properties, not provider-specific payload bytes or ordering.

The machine-readable result is
`docs/qualification/issue-354-context-adapter-parity-report.json`. The
executable qualification checks are in
`tests/qualification/test_issue354_qualification.py`.

## Qualification boundary

The offline gate proves:

- adapter and contract version are reported for every parity observation;
- allowed, denied, stale, tombstoned, partial, degraded, unavailable and
  cancelled cases map to the same normalized typed outcomes;
- workspace/environment authorization is fail-closed and denied requests do
  not reach physical reads;
- partial results remain distinct from complete allowed results;
- source revision, stable locator and optional score behavior remains typed;
- provider diagnostics and protected text are absent from normalized failures.

Tombstoned projection evidence is normalized at the Context Broker boundary to
`insufficient_evidence`; the public `ContextOutcome` contract intentionally has
no provider-specific tombstone member.

## AC1–AC8 traceability

| Criterion | Executable evidence | Qualified behavior |
| --- | --- | --- |
| AC1 | `tests/contracts/memory/test_adapter_parity.py::test_active_context_brokers_have_equivalent_normalized_outcomes`<br>`tests/contracts/memory/test_adapter_parity.py::test_active_legacy_broker_adapter_preserves_normalized_outcomes`<br>`tests/contracts/memory/test_adapter_parity.py::test_active_projection_adapters_normalize_retrieval_failures` | The same deterministic matrix is run against each active adapter composition, including the supported legacy compatibility surface. |
| AC2 | `tests/contracts/memory/test_adapter_parity.py::test_active_context_brokers_have_equivalent_normalized_outcomes`<br>`tests/contracts/memory/test_adapter_parity.py::test_broker_parity_keeps_partial_distinct_from_complete` | Closed outcomes and the partial/complete distinction are mapped consistently. |
| AC3 | `tests/contracts/memory/test_adapter_parity.py::test_authorized_broker_does_not_call_physical_read_after_denial`<br>`tests/contracts/memory/test_acl_leakage_contract.py::test_denied_scope_has_no_content_or_sensitive_diagnostic` | Access policy, workspace and environment isolation fail closed. |
| AC4 | `tests/application/test_authorized_context_broker.py::test_allowed_request_persists_exact_bundle_and_replays_without_second_read`<br>`tests/contracts/memory/test_publication_pin.py::test_broker_resolves_one_immutable_pin_and_passes_same_identity_downstream`<br>`tests/application/test_attempt_revalidation.py::test_pin_mismatch_rejects_even_when_the_provider_reports_a_healthy_state`<br>`tests/application/test_authorized_retrieval_scope.py::test_stale_revision_fails_closed_and_provider_narrowing_is_required_when_declared`<br>`tests/contracts/memory/test_adapter_parity.py::test_active_projection_adapters_preserve_score_presence_and_provenance` | ContextBundle identity, publication pins, stale revisions, source revisions and locators remain consistent or fail closed. |
| AC5 | `tests/contracts/memory/test_adapter_parity.py::test_active_context_brokers_have_equivalent_normalized_outcomes`<br>`tests/contracts/memory/test_conformance.py::test_unsafe_evidence_never_leaks_projected_content` | Bundles and projected content are absent from unsafe outcomes. |
| AC6 | `tests/contracts/memory/test_adapter_parity.py::test_broker_adapters_map_provider_failures_without_diagnostics`<br>`tests/contracts/memory/test_conformance.py::test_unsafe_evidence_never_leaks_projected_content` | Provider-specific failure details do not cross the normalized seam. |
| AC7 | `tests/contracts/memory/test_adapter_parity.py::test_active_projection_adapters_preserve_score_presence_and_provenance`<br>`tests/contracts/memory/test_retrieval_contract.py::test_retrieval_score_is_optional_and_round_trips_when_provider_supplies_it` | Score-present and score-absent retrieval remain contract-compatible. |
| AC8 | `tests/qualification/test_issue354_qualification.py::test_issue354_offline_parity_suite_passes`<br>`tests/qualification/test_issue354_qualification.py::test_issue354_report_is_machine_readable` | The result and traceability matrix are executable and machine-readable. |

## Verification

Focused qualification:

```text
uv run pytest -q tests/qualification/test_issue354_qualification.py
```

Repository handoff additionally requires:

```text
uv sync --extra dev
uv run pytest -q
uv run python -m compileall -q src tests
```

Real Cognee, PostgreSQL and production provider qualification remain outside
this deterministic offline gate.
