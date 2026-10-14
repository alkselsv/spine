"""Executable cross-slice qualification for Issue #90."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys

from alembic.config import Config
from alembic.script import ScriptDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TRACEABILITY = PROJECT_ROOT / "docs/qualification/issue-90-source-projection-lifecycle.md"
REPORT = PROJECT_ROOT / "docs/qualification/issue-90-source-projection-lifecycle-report.json"
REPOSITORY_GATES = (
    "uv sync --extra dev",
    "uv run pytest -q",
    "uv run python -m compileall -q src tests",
)


@dataclass(frozen=True, slots=True)
class TraceabilityRow:
    acceptance_criterion: int
    test_level: str
    evidence: tuple[str, ...]


TRACEABILITY_MATRIX = (
    TraceabilityRow(
        1,
        "postgresql/domain",
        (
            "tests/integration/postgresql/test_initial_tenancy_migration.py::test_every_retained_revision_upgrades_to_one_final_head",
            "tests/integration/postgresql/test_initial_tenancy_migration.py::test_initial_schema_has_named_tenancy_constraints_and_postgresql_types",
            "tests/contracts/persistence/test_source_observation_contract.py::test_source_observation_replay_is_stable",
            "tests/contracts/persistence/test_projection_contract.py::test_projection_candidate_seal_freezes_evidence_and_replays",
        ),
    ),
    TraceabilityRow(
        2,
        "contract/application/integration",
        (
            "tests/contracts/persistence/test_unit_of_work_contract.py::test_explicit_commit_makes_workspace_visible",
            "tests/contracts/persistence/test_canonical_acceptance_contract.py::test_canonical_happy_path_and_exact_replay",
            "tests/contracts/persistence/test_outbox_writer_contract.py::test_canonical_mutation_and_intent_become_visible_only_after_commit",
            "tests/contracts/persistence/test_audit_writer_contract.py::test_audit_failure_rolls_back_mutation_and_outbox_intent",
            "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_adapter_satisfies_shared_contract",
            "tests/integration/postgresql/test_postgresql_audit.py::test_mutation_audit_and_outbox_roll_back_together_at_every_boundary",
        ),
    ),
    TraceabilityRow(
        3,
        "security/postgresql",
        (
            "tests/contracts/persistence/test_unit_of_work_contract.py::test_worker_and_interactive_contexts_have_same_workspace_isolation",
            "tests/contracts/persistence/test_audit_writer_contract.py::test_audit_reader_does_not_reveal_event_to_another_workspace",
            "tests/application/persistence/test_ingestion.py::test_ingestion_scope_mismatch_fails_without_disclosing_ledger_rows",
            "tests/application/test_content_authorization.py::test_revoked_current_policy_is_denied_on_next_decision_without_cache",
            "tests/application/test_content_authorization.py::test_changed_authority_generation_requires_reauthorization",
            "tests/application/test_access_policy_activation.py::test_stale_baseline_has_no_activation_audit_or_outbox_effect",
            "tests/memory/cognee/test_retrieval.py::test_retrieval_rejects_shadow_namespace_without_physical_call",
            "tests/integration/postgresql/test_initial_tenancy_migration.py::test_workspace_rls_hides_and_rejects_another_workspace_rows",
            "tests/integration/postgresql/test_initial_tenancy_migration.py::test_environment_rls_isolates_two_environments_in_one_workspace",
            "tests/integration/postgresql/test_postgresql_audit.py::test_runtime_rls_denies_cross_workspace_audit_read_and_insert",
            "tests/integration/postgresql/test_authorization_directory.py::test_revoked_generation_stops_next_composed_request_before_handler",
            "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_policy_change_wins_before_acceptance_and_stale_evidence_fails_closed",
        ),
    ),
    TraceabilityRow(
        4,
        "contract/application/postgresql",
        (
            "tests/contracts/persistence/test_unit_of_work_contract.py::test_concurrent_duplicate_commit_has_one_logical_effect",
            "tests/contracts/persistence/test_ingestion_repository_contract.py::test_ingestion_binding_race_contract",
            "tests/contracts/persistence/test_outbox_delivery_repository_contract.py::test_concurrent_workers_cannot_claim_the_same_delivery",
            "tests/application/persistence/test_transaction_retry.py::test_retry_restarts_complete_unit_of_work_and_commits_one_effect",
            "tests/application/persistence/test_failure_paths.py::test_commit_copy_failure_is_atomic_and_terminal",
            "tests/application/test_projection_result_mapping.py::test_partial_failure_retry_replays_stable_receipt_without_becoming_validation",
            "tests/application/storage/test_reconciliation.py::test_reconciliation_reports_safe_orphan_and_preserves_restart_cursor",
            "tests/domain/projection/test_publication_models.py::test_drift_requires_a_specific_canonical_acceptance_and_detection_source",
            "tests/domain/projection/test_publication_models.py::test_unpublished_resolution_does_not_fabricate_freshness_or_health",
            "tests/memory/cognee/test_rebuild.py::test_abandoned_shadow_rebuild_is_recoverable_without_becoming_readable",
            "tests/memory/cognee/test_graph.py::test_graph_writer_failed_shadow_rebuild_does_not_change_active_namespace",
            "tests/memory/cognee/test_recovery.py::test_repair_fences_stale_source_without_reintroducing_artifacts",
            "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_concurrent_duplicate_claims_have_one_owner_and_stable_replay",
            "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_lost_commit_response_replays_one_combined_effect",
            "tests/integration/postgresql/test_postgresql_outbox_delivery.py::test_postgresql_concurrent_workers_do_not_overlap_claims",
            "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_access_policy_activation_has_one_concurrent_cas_winner_and_stable_replay",
            "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_boundary_capture_fences_acceptance_at_counter_lock",
            "tests/integration/postgresql/test_initial_tenancy_migration.py::test_k0_deferred_validator_rollback_removes_child_and_retry_succeeds",
        ),
    ),
    TraceabilityRow(
        5,
        "architecture/repository gates",
        (
            "tests/test_architecture_scaffold.py::ArchitectureScaffoldTests::test_domain_layer_has_no_framework_imports",
            "tests/application/test_source_lifecycle.py::test_deletion_records_observation_and_delegates_canonical_transition_atomically",
            "tests/application/test_source_lifecycle.py::test_reappearance_requires_existing_tombstone_and_fresh_canonical_evidence",
            "tests/application/test_projection_result_persistence.py::test_duplicate_delivery_replays_without_second_effect",
            "tests/application/test_access_policy_activation.py::test_concurrent_successors_have_one_cas_winner",
            "tests/qualification/test_issue154_qualification.py::test_issue154_qualification_record_covers_the_five_r1_formats",
            "tests/qualification/test_issue211_qualification.py::test_issue211_report_is_machine_readable_and_disclaims_production_approval",
            "tests/contracts/diagnostics/test_sink_contract.py::test_sink_emits_the_same_validated_snapshot[recording]",
            "tests/application/diagnostics/test_errors.py::test_unknown_error_is_generic_without_inspecting_or_leaking_exception",
            "tests/qualification/test_issue90_qualification.py::test_issue90_migration_graph_has_one_dynamic_head",
            "tests/qualification/test_issue90_qualification.py::test_issue90_repository_gates_are_documented",
        ),
    ),
)


def _evidence_nodes() -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            evidence
            for row in TRACEABILITY_MATRIX
            for evidence in row.evidence
        )
    )


def _node_name(node_text: str) -> str:
    return node_text.split("[", maxsplit=1)[0].rsplit("::", maxsplit=1)[-1]


def _report() -> dict[str, object]:
    return json.loads(REPORT.read_text(encoding="utf-8"))


def test_issue90_traceability_matrix_covers_exactly_ac1_to_ac5() -> None:
    allowed_levels = {
        "postgresql/domain",
        "contract/application/integration",
        "security/postgresql",
        "contract/application/postgresql",
        "architecture/repository gates",
    }
    assert tuple(row.acceptance_criterion for row in TRACEABILITY_MATRIX) == (1, 2, 3, 4, 5)
    assert all(row.evidence for row in TRACEABILITY_MATRIX)
    assert all(len(row.evidence) == len(set(row.evidence)) for row in TRACEABILITY_MATRIX)
    assert all(row.test_level in allowed_levels for row in TRACEABILITY_MATRIX)


def test_issue90_traceability_evidence_points_to_real_test_nodes() -> None:
    for evidence in _evidence_nodes():
        path_text, node_text = evidence.split("::", maxsplit=1)
        evidence_path = PROJECT_ROOT / path_text
        assert evidence_path.is_file(), evidence
        tree = ast.parse(evidence_path.read_text(encoding="utf-8"))
        assert any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == _node_name(node_text)
            for node in ast.walk(tree)
        ), evidence


def test_issue90_traceability_document_matches_executable_matrix() -> None:
    content = TRACEABILITY.read_text(encoding="utf-8")
    rows = {
        int(match.group(1)): tuple(
            re.findall(r"`(tests/[^`]+?::(?:test_|ArchitectureScaffoldTests::test_)[^`]+)`", match.group(2))
        )
        for match in re.finditer(r"^\| AC(\d+) \| (.+?) \|$", content, re.MULTILINE)
    }
    assert tuple(rows) == (1, 2, 3, 4, 5)
    assert rows == {
        row.acceptance_criterion: row.evidence for row in TRACEABILITY_MATRIX
    }


def test_issue90_migration_graph_has_one_dynamic_head() -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    script = ScriptDirectory.from_config(config)

    assert len(script.get_heads()) == 1
    assert script.get_bases()


def test_issue90_repository_gates_are_documented() -> None:
    content = TRACEABILITY.read_text(encoding="utf-8")
    assert all(f"{command}" in content for command in REPOSITORY_GATES)


def test_issue90_traceability_evidence_nodes_are_collectable() -> None:
    paths = tuple(dict.fromkeys(node.split("::", maxsplit=1)[0] for node in _evidence_nodes()))
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *paths],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    output = collected.stdout + collected.stderr
    assert collected.returncode == 0, output
    assert all(node in output for node in _evidence_nodes()), output


def test_issue90_offline_evidence_suite_passes() -> None:
    offline_paths = tuple(
        dict.fromkeys(
            node.split("::", maxsplit=1)[0]
            for node in _evidence_nodes()
            if not node.startswith("tests/integration/postgresql/")
            and not node.startswith("tests/qualification/")
        )
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *offline_paths],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert re.search(r"\d+ passed", output), output


def test_issue90_report_is_machine_readable_and_keeps_provider_boundary_explicit() -> None:
    report = _report()

    assert report["schema_version"] == "source-projection-lifecycle-qualification:v1"
    assert report["ticket"] == 90
    assert report["criteria"] == [
        {
            "id": f"AC{row.acceptance_criterion}",
            "status": "qualified",
            "evidence": list(row.evidence),
        }
        for row in TRACEABILITY_MATRIX
    ]
    assert report["production_approval"] is False
    limitations = report["limitations"]
    assert isinstance(limitations, list)
    assert any("PostgreSQL" in item for item in limitations)
    assert any("external" in item.lower() for item in limitations)
