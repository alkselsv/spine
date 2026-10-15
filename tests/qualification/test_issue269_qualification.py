"""Executable qualification matrix for the complete Context Broker (Issue #269)."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TRACEABILITY = PROJECT_ROOT / "docs/qualification/issue-269-context-broker.md"
REPORT = PROJECT_ROOT / "docs/qualification/issue-269-context-broker-report.json"


OFFLINE_TARGETS = (
    "tests/contracts/memory/test_context_broker_v1.py",
    "tests/contracts/memory/test_publication_pin.py",
    "tests/contracts/memory/test_retrieval_contract.py",
    "tests/application/test_authorized_context_broker.py",
    "tests/application/test_authorized_retrieval_scope.py",
    "tests/application/test_attempt_revalidation.py",
    "tests/application/persistence/test_context_bundles.py",
    "tests/memory/test_physical_read_plan.py",
    "tests/memory/test_structured_retrieval.py",
    "tests/test_architecture_scaffold.py",
)


@dataclass(frozen=True, slots=True)
class TraceabilityRow:
    acceptance_criterion: int
    test_level: str
    evidence: tuple[str, ...]


TRACEABILITY_MATRIX = (
    TraceabilityRow(
        1,
        "offline/application",
        (
            "tests/application/test_authorized_context_broker.py::test_allowed_request_persists_exact_bundle_and_replays_without_second_read",
            "tests/contracts/memory/test_context_broker_v1.py::test_fake_broker_issues_an_explicit_fake_bundle_for_allowed_retrieval",
            "tests/application/persistence/test_context_bundles.py::test_context_bundle_repository_replays_exact_immutable_attempt",
        ),
    ),
    TraceabilityRow(
        2,
        "contract/application",
        (
            "tests/contracts/memory/test_context_broker_v1.py::test_context_request_rejects_client_selected_projection_authority",
            "tests/contracts/memory/test_context_broker_v1.py::test_fake_broker_maps_controlled_retrieval_outcomes_safely",
            "tests/contracts/memory/test_context_broker_v1.py::test_context_response_rejects_bundle_on_denied_outcome",
            "tests/application/test_authorized_context_broker.py::test_unpublished_active_projection_is_safe_unavailable",
        ),
    ),
    TraceabilityRow(
        3,
        "authorization/isolation",
        (
            "tests/application/test_authorized_retrieval_scope.py::test_mixed_access_omits_unauthorized_source_without_denied_source_leakage",
            "tests/application/test_authorized_retrieval_scope.py::test_interactive_processing_requires_human_and_service_grants",
            "tests/application/test_authorized_retrieval_scope.py::test_scope_rejects_wrong_purpose_and_cross_environment_without_repository_call",
            "tests/application/test_authorized_context_broker.py::test_workspace_mismatch_denies_without_leaking_other_workspace_state",
            "tests/application/test_attempt_revalidation.py::test_same_idempotency_key_isolated_between_workspaces",
            "tests/contracts/memory/test_context_broker_v1.py::test_fake_broker_keeps_two_workspace_requests_isolated",
        ),
    ),
    TraceabilityRow(
        4,
        "provenance/retrieval",
        (
            "tests/application/test_authorized_retrieval_scope.py::test_superseded_revision_fails_closed_instead_of_using_old_publication_member",
            "tests/application/test_authorized_retrieval_scope.py::test_missing_retention_or_tombstoned_current_revision_fails_closed",
            "tests/memory/test_physical_read_plan.py::test_missing_receipt_lineage_fails_closed",
            "tests/memory/test_physical_read_plan.py::test_cross_workspace_reference_fails_closed_without_repairing_the_reference",
            "tests/memory/test_structured_retrieval.py::test_executor_keeps_incompatible_duplicate_provenance_as_separate_items",
        ),
    ),
    TraceabilityRow(
        5,
        "physical-boundary",
        (
            "tests/application/test_authorized_retrieval_scope.py::test_missing_provider_safety_proof_fails_closed",
            "tests/memory/test_physical_read_plan.py::test_isolation_failure_is_typed_and_never_produces_a_plan",
            "tests/memory/test_physical_read_plan.py::test_non_active_namespace_is_excluded_before_provider_boundary",
            "tests/memory/test_physical_read_plan.py::test_changed_publication_pin_cannot_mix_with_the_authorized_scope",
            "tests/memory/test_structured_retrieval.py::test_executor_rejects_unsafe_resolution_without_provider_call",
        ),
    ),
    TraceabilityRow(
        6,
        "replay/revalidation",
        (
            "tests/application/test_authorized_context_broker.py::test_replay_returns_immutable_bundle_without_reinvoking_profile_or_publication",
            "tests/application/persistence/test_context_bundles.py::test_context_bundle_repository_rejects_conflicting_replay",
            "tests/application/test_attempt_revalidation.py::test_exact_duplicate_is_idempotent",
            "tests/application/test_attempt_revalidation.py::test_concurrent_duplicate_submissions_share_one_logical_result",
            "tests/application/test_attempt_revalidation.py::test_policy_revocation_rejects_reuse_without_a_regeneration_plan",
            "tests/application/test_attempt_revalidation.py::test_tombstoned_source_fails_closed_even_when_the_old_evidence_is_present",
        ),
    ),
    TraceabilityRow(
        7,
        "partial/cancellation/scoring",
        (
            "tests/application/test_attempt_revalidation.py::test_cancelled_request_has_no_protected_bundle_result",
            "tests/application/test_attempt_revalidation.py::test_partial_bundle_replay_keeps_a_typed_partial_outcome_without_evidence",
            "tests/memory/test_structured_retrieval.py::test_executor_reports_partial_provider_failure_without_failed_lane_content",
            "tests/memory/test_structured_retrieval.py::test_executor_propagates_cancellation_before_starting_next_partition",
            "tests/contracts/memory/test_retrieval_contract.py::test_retrieval_score_is_optional_and_round_trips_when_provider_supplies_it",
        ),
    ),
    TraceabilityRow(
        8,
        "postgresql/architecture",
        (
            "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_context_bundle_replays_isolates_and_safe_reads",
            "tests/infrastructure/persistence/test_postgresql_publication_resolution.py::test_active_publication_resolution_uses_one_consistent_statement_snapshot",
            "tests/integration/postgresql/test_initial_tenancy_migration.py::test_workspace_rls_hides_and_rejects_another_workspace_rows",
            "tests/integration/postgresql/test_initial_tenancy_migration.py::test_environment_rls_isolates_two_environments_in_one_workspace",
            "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_concurrent_duplicate_claims_have_one_owner_and_stable_replay",
            "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_lost_commit_response_replays_one_combined_effect",
            "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_access_policy_activation_has_one_concurrent_cas_winner_and_stable_replay",
            "tests/integration/postgresql/test_authorization_directory.py::test_revoked_generation_stops_next_composed_request_before_handler",
            "tests/test_architecture_scaffold.py::ArchitectureScaffoldTests::test_domain_layer_has_no_framework_imports",
        ),
    ),
)


def _report() -> dict[str, object]:
    return json.loads(REPORT.read_text(encoding="utf-8"))


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


def test_issue269_traceability_matrix_covers_exactly_ac1_to_ac8() -> None:
    allowed_levels = {
        "offline/application",
        "contract/application",
        "authorization/isolation",
        "provenance/retrieval",
        "physical-boundary",
        "replay/revalidation",
        "partial/cancellation/scoring",
        "postgresql/architecture",
    }

    assert tuple(row.acceptance_criterion for row in TRACEABILITY_MATRIX) == tuple(
        range(1, 9)
    )
    assert all(row.evidence for row in TRACEABILITY_MATRIX)
    assert all(len(row.evidence) == len(set(row.evidence)) for row in TRACEABILITY_MATRIX)
    assert all(row.test_level in allowed_levels for row in TRACEABILITY_MATRIX)


def test_issue269_traceability_evidence_points_to_real_test_nodes() -> None:
    for evidence in _evidence_nodes():
        path_text, node_text = evidence.split("::", maxsplit=1)
        evidence_path = PROJECT_ROOT / path_text
        assert evidence_path.is_file(), evidence
        tree = ast.parse(evidence_path.read_text(encoding="utf-8"))
        node_name = _node_name(node_text)
        assert any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == node_name
            for node in ast.walk(tree)
        ), evidence


def test_issue269_traceability_document_matches_executable_matrix() -> None:
    content = TRACEABILITY.read_text(encoding="utf-8")
    rows = {
        int(match.group(1)): tuple(
            re.findall(r"`(tests/[^`]+?::(?:test_|ArchitectureScaffoldTests::test_)[^`]+)`", match.group(2))
        )
        for match in re.finditer(r"^\| AC(\d+) \| (.+?) \|$", content, re.MULTILINE)
    }

    assert tuple(rows) == tuple(range(1, 9))
    assert rows == {
        row.acceptance_criterion: row.evidence for row in TRACEABILITY_MATRIX
    }


def test_issue269_traceability_evidence_nodes_are_collectable() -> None:
    paths = tuple(
        dict.fromkeys(node.split("::", maxsplit=1)[0] for node in _evidence_nodes())
    )
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


def test_issue269_offline_evidence_suite_passes() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *OFFLINE_TARGETS],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert re.search(r"\d+ passed", output), output


def test_issue269_report_is_machine_readable_and_separates_readiness_claims() -> None:
    report = _report()

    assert report["schema_version"] == "context-broker-qualification:v1"
    assert report["ticket"] == 269
    assert report["parent_ticket"] == 27
    assert report["criteria"] == [
        {
            "id": f"AC{row.acceptance_criterion}",
            "status": "qualified_offline",
            "evidence": list(row.evidence),
        }
        for row in TRACEABILITY_MATRIX
    ]
    assert report["release_gate"] == {
        "status": "qualified_offline",
        "postgresql": "mandatory_external_gate",
        "provider_qualification": "not_approved",
    }
    readiness = report["readiness"]
    assert readiness == {
        "technical": "qualified_offline",
        "contract": "qualified_offline",
        "native_dependency": "not_run",
        "agent_claimability": "qualified_offline",
        "production": "not_approved",
    }
    postgres = report["postgresql_prerequisite"]
    assert isinstance(postgres, dict)
    assert postgres["status"] == "mandatory_external_gate"
    assert "SPINE_TEST_DATABASE_URL" in postgres["requirement"]
    assert "Docker" in postgres["requirement"]
    assert report["production_approval"] is False


def test_issue269_report_documents_all_approved_issue27_boundaries() -> None:
    report = _report()
    boundaries = report["boundaries"]
    assert boundaries == {
        "postgresql_publication_authority": "consumed_not_owned",
        "access_policy_authority": "consumed_not_owned",
        "cognee": "rebuildable_physical_projection_only",
        "answer_generation": "out_of_scope",
        "final_disclosure": "out_of_scope",
    }
