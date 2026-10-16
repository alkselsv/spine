"""Executable qualification matrix for Issue #364 projection recovery."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TRACEABILITY = PROJECT_ROOT / "docs/qualification/issue-364-projection-recovery.md"
REPORT = PROJECT_ROOT / "docs/qualification/issue-364-projection-recovery-report.json"


@dataclass(frozen=True, slots=True)
class TraceabilityRow:
    acceptance_criterion: int
    evidence: tuple[str, ...]


TRACEABILITY_MATRIX = (
    TraceabilityRow(
        1,
        (
            "tests/application/test_projection_recovery_execution.py::test_recovery_request_dispatch_rebuilds_once_and_records_canonical_result",
            "tests/integration/postgresql/test_postgresql_outbox_delivery.py::test_postgresql_recovery_request_replay_dispatches_one_tenant_scoped_intent",
            "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_projection_recovery_resolver_satisfies_contract",
            "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_projection_recovery_result_bridge_satisfies_contract",
        ),
    ),
    TraceabilityRow(
        2,
        (
            "tests/application/test_projection_recovery_execution.py::test_retryable_physical_rebuild_redelivery_has_one_canonical_result",
            "tests/contracts/memory/test_projection_rebuild_contract.py::test_rebuild_replay_is_stable_after_a_lost_response",
            "tests/contracts/persistence/test_projection_contract.py::test_projection_recovery_resolver_replays_one_shadow_rebuild_plan",
            "tests/contracts/persistence/test_projection_contract.py::test_projection_recovery_result_bridge_persists_and_replays_canonical_evidence",
        ),
    ),
    TraceabilityRow(
        3,
        (
            "tests/application/test_projection_publication.py::test_publish_from_adapter_result_maps_and_validates_before_activation",
            "tests/application/test_projection_publication.py::test_publish_runs_physical_gate_before_postgresql_activation",
            "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_projection_publication_satisfies_cas_contract",
        ),
    ),
    TraceabilityRow(
        4,
        (
            "tests/application/test_projection_request_execution.py::test_non_retryable_physical_rejection_returns_safe_terminal_code",
            "tests/application/test_physical_projection_validation.py::test_failed_physical_evidence_becomes_failed_validation_and_degraded_reconciliation",
            "tests/contracts/memory/test_projection_rebuild_contract.py::test_shadow_rebuild_artifacts_require_explicit_namespace_selection",
        ),
    ),
    TraceabilityRow(
        5,
        (
            "tests/application/test_projection_publication.py::test_rollback_command_targets_only_a_validated_successor_snapshot",
            "tests/application/test_projection_publication.py::test_rollback_command_rejects_historical_snapshot_without_successor_lineage",
            "tests/application/test_projection_publication.py::test_rollback_delegates_successor_cas_claim_to_publication_repository",
            "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_projection_publication_satisfies_cas_contract",
        ),
    ),
    TraceabilityRow(
        6,
        (
            "tests/application/test_projection_control.py::test_projection_status_marks_stale_degraded_publication_retryable",
            "tests/application/test_projection_control.py::test_projection_status_exposes_failed_reconciliation_as_bounded_failure",
            "tests/integration/postgresql/test_postgresql_outbox_delivery.py::test_postgresql_retry_obeys_due_boundary",
            "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_projection_recovery_lifecycle_satisfies_contract",
        ),
    ),
    TraceabilityRow(
        7,
        (
            "tests/application/test_projection_recovery_execution.py::test_foreign_or_changed_recovery_plan_is_rejected_before_physical_work",
            "tests/integration/postgresql/test_postgresql_outbox_delivery.py::test_postgresql_claim_preserves_tenant_and_trace_scope",
            "tests/application/test_physical_projection_validation.py::test_physical_evidence_rejects_a_fabricated_scope_before_persistence",
            "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_projection_recovery_resolver_satisfies_contract",
            "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_projection_recovery_result_bridge_satisfies_contract",
        ),
    ),
    TraceabilityRow(
        8,
        (
            "tests/application/test_projection_recovery_execution.py::test_projection_delivery_routes_include_operator_recovery_requests",
            "tests/application/test_projection_control.py::test_projection_recovery_request_replays_and_emits_one_typed_outbox_intent",
        ),
    ),
)


def _report() -> dict[str, object]:
    return json.loads(REPORT.read_text(encoding="utf-8"))


def _node_name(value: str) -> str:
    return value.split("[", maxsplit=1)[0]


def test_issue364_matrix_covers_every_acceptance_criterion_with_real_tests() -> None:
    assert tuple(row.acceptance_criterion for row in TRACEABILITY_MATRIX) == tuple(
        range(1, 9)
    )
    for row in TRACEABILITY_MATRIX:
        assert row.evidence
        for evidence in row.evidence:
            path_text, node_text = evidence.split("::", maxsplit=1)
            source = PROJECT_ROOT / path_text
            tree = ast.parse(source.read_text(encoding="utf-8"))
            assert any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == _node_name(node_text)
                for node in ast.walk(tree)
            ), evidence


def test_issue364_report_matches_versioned_traceability_contract() -> None:
    report = _report()

    assert report["schema_version"] == "projection-recovery-qualification:v1"
    assert report["ticket"] == 364
    assert report["parent_ticket"] == 18
    assert report["criteria"] == [
        {
            "id": f"AC{row.acceptance_criterion}",
            "status": "qualified_postgresql"
            if row.acceptance_criterion in {1, 2, 3, 5, 6, 7}
            else "qualified_offline",
            "evidence": list(row.evidence),
        }
        for row in TRACEABILITY_MATRIX
    ]
    assert report["production_approval"] is False
    assert report["readiness"] == {
        "deterministic_physical_adapter": "qualified",
        "postgresql": "qualified_disposable_harness",
        "configured_cognee": "not_run",
        "production": "not_approved",
    }


def test_issue364_report_preserves_existing_authority_boundaries() -> None:
    report = _report()

    assert report["boundaries"] == {
        "recovery_api": "issue_296_consumed_not_replaced",
        "physical_projection": "context_projection_port",
        "canonical_state": "postgresql_projection_repository",
        "publication": "existing_validation_seal_and_cas",
        "rollback": "new_successor_never_pointer_rewind",
        "telemetry": "operational_observation_not_audit_authority",
    }
    assert report["unverified_prerequisites"] == [
        "Configured Cognee/Neo4j/PGVector provider profile was not selected or exercised.",
        "Production identities, authorization bindings, and deployment monitoring remain deployment-owned prerequisites.",
    ]
    assert TRACEABILITY.read_text(encoding="utf-8").startswith(
        "# Issue #364 projection recovery qualification\n"
    )
