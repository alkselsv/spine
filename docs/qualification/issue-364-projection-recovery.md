# Issue #364 projection recovery qualification

Status: qualified with deterministic physical adapter and disposable PostgreSQL;
configured Cognee/provider execution remains unverified.

This qualification exercises the existing #296 command, durable outbox,
`ContextProjection`, #208 result mapping, physical validation, candidate sealing,
PostgreSQL publication CAS, and successor rollback boundaries. It does not add a
second recovery API, projection lifecycle, active-pointer authority, or audit
ledger.

## Traceability

| Criterion | Evidence |
|---|---|
| AC1 | `tests/application/test_projection_recovery_execution.py::test_recovery_request_dispatch_rebuilds_once_and_records_canonical_result`; `tests/integration/postgresql/test_postgresql_outbox_delivery.py::test_postgresql_recovery_request_replay_dispatches_one_tenant_scoped_intent`; `tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_projection_recovery_resolver_satisfies_contract`; `tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_projection_recovery_result_bridge_satisfies_contract` |
| AC2 | `tests/application/test_projection_recovery_execution.py::test_retryable_physical_rebuild_redelivery_has_one_canonical_result`; `tests/contracts/memory/test_projection_rebuild_contract.py::test_rebuild_replay_is_stable_after_a_lost_response`; `tests/contracts/persistence/test_projection_contract.py::test_projection_recovery_resolver_replays_one_shadow_rebuild_plan`; `tests/contracts/persistence/test_projection_contract.py::test_projection_recovery_result_bridge_persists_and_replays_canonical_evidence` |
| AC3 | `tests/application/test_projection_publication.py::test_publish_from_adapter_result_maps_and_validates_before_activation`; `tests/application/test_projection_publication.py::test_publish_runs_physical_gate_before_postgresql_activation`; `tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_projection_publication_satisfies_cas_contract` |
| AC4 | `tests/application/test_projection_request_execution.py::test_non_retryable_physical_rejection_returns_safe_terminal_code`; `tests/application/test_physical_projection_validation.py::test_failed_physical_evidence_becomes_failed_validation_and_degraded_reconciliation`; `tests/contracts/memory/test_projection_rebuild_contract.py::test_shadow_rebuild_artifacts_require_explicit_namespace_selection` |
| AC5 | `tests/application/test_projection_publication.py::test_rollback_command_targets_only_a_validated_successor_snapshot`; `tests/application/test_projection_publication.py::test_rollback_command_rejects_historical_snapshot_without_successor_lineage`; `tests/application/test_projection_publication.py::test_rollback_delegates_successor_cas_claim_to_publication_repository`; `tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_projection_publication_satisfies_cas_contract` |
| AC6 | `tests/application/test_projection_control.py::test_projection_status_marks_stale_degraded_publication_retryable`; `tests/application/test_projection_control.py::test_projection_status_exposes_failed_reconciliation_as_bounded_failure`; `tests/integration/postgresql/test_postgresql_outbox_delivery.py::test_postgresql_retry_obeys_due_boundary`; `tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_projection_recovery_lifecycle_satisfies_contract` |
| AC7 | `tests/application/test_projection_recovery_execution.py::test_foreign_or_changed_recovery_plan_is_rejected_before_physical_work`; `tests/integration/postgresql/test_postgresql_outbox_delivery.py::test_postgresql_claim_preserves_tenant_and_trace_scope`; `tests/application/test_physical_projection_validation.py::test_physical_evidence_rejects_a_fabricated_scope_before_persistence`; `tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_projection_recovery_resolver_satisfies_contract`; `tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_projection_recovery_result_bridge_satisfies_contract` |
| AC8 | `tests/application/test_projection_recovery_execution.py::test_projection_delivery_routes_include_operator_recovery_requests`; `tests/application/test_projection_control.py::test_projection_recovery_request_replays_and_emits_one_typed_outbox_intent` |

## Qualified flow

The administrator command persists one content-free
`projection.recovery_requested:v1` intent. The registered
`projection.recovery_requested` event is delivered through the existing
`projection_request` route, which reconstructs and validates its
scope, command identity, action, and digest. A canonical resolver supplies an
immutable `RebuildCommand`; the worker invokes the physical projection only
after the canonical read transaction has closed. Result mapping reuses the
existing canonical receipt/reference bridge.

At-least-once delivery is safe: a retry resumes the same physical command, and
the canonical result bridge returns a recorded handoff instead of producing a
second logical result. Foreign scope, changed digest, malformed lineage,
stale/tombstoned input, active-namespace use, failed physical validation, and
CAS conflict fail closed with bounded codes.

Publication remains gated by physical evidence, sealing, and the existing
PostgreSQL activation CAS. Rollback is a finalized successor snapshot pointing
to the current active snapshot; historical pointer rewind is rejected.

## Remaining prerequisites

- Configured Cognee/Neo4j/PGVector provider profile was not selected or exercised.
- Production identities, authorization bindings, and deployment monitoring remain deployment-owned prerequisites.

No production approval, provider qualification, RPO/RTO claim, or direct
canonical mutation is made by this report.
