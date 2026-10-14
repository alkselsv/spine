"""Executable offline qualification checks for ContextProjection Issue #211."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TRACEABILITY = PROJECT_ROOT / "docs/qualification/issue-211-context-projection.md"
REPORT = PROJECT_ROOT / "docs/qualification/issue-211-context-projection-report.json"

OFFLINE_TARGETS = (
    "tests/contracts/memory",
    "tests/memory/cognee",
    "tests/application/test_projection_result_mapping.py",
)


@dataclass(frozen=True, slots=True)
class TraceabilityRow:
    acceptance_criterion: int
    evidence: tuple[str, ...]


TRACEABILITY_MATRIX = (
    TraceabilityRow(
        1,
        (
            "tests/contracts/memory/test_conformance.py::test_context_projection_contract_exposes_all_operations",
            "tests/memory/cognee/test_indexing.py::test_indexing_preserves_revision_locator_and_namespace_provenance",
            "tests/memory/cognee/test_graph.py::test_graph_writer_preserves_approved_artifacts_and_evidence_lineage",
        ),
    ),
    TraceabilityRow(
        2,
        (
            "tests/contracts/memory/test_conformance.py::test_retrieval_round_trips_chunks_entities_relations_and_provenance",
            "tests/memory/cognee/test_retrieval.py::test_retrieval_maps_public_structured_search_to_provider_neutral_result",
            "tests/application/test_projection_result_mapping.py::test_valid_adapter_result_maps_to_issue7_receipt_and_source_reference",
        ),
    ),
    TraceabilityRow(
        3,
        (
            "tests/contracts/memory/test_conformance.py::test_missing_evidence_abstains_without_returning_content",
            "tests/memory/cognee/test_validation.py::test_duplicate_missing_and_incomplete_sets_are_deterministic",
            "tests/memory/cognee/test_retrieval.py::test_retrieval_rejects_reference_without_physical_provenance_mapping",
            "tests/memory/cognee/test_retrieval.py::test_retrieval_returns_typed_incomplete_result_for_missing_locator",
        ),
    ),
    TraceabilityRow(
        4,
        (
            "tests/contracts/memory/test_conformance.py::test_shadow_namespace_is_not_visible_to_ordinary_retrieval",
            "tests/memory/cognee/test_retrieval.py::test_retrieval_rejects_shadow_namespace_without_physical_call",
            "tests/memory/cognee/test_rebuild.py::test_abandoned_shadow_rebuild_is_recoverable_without_becoming_readable",
            "tests/memory/cognee/test_graph.py::test_graph_writer_failed_shadow_rebuild_does_not_change_active_namespace",
        ),
    ),
    TraceabilityRow(
        5,
        (
            "tests/contracts/memory/test_conformance.py::test_same_command_replays_without_a_second_logical_effect",
            "tests/contracts/memory/test_conformance.py::test_non_current_revision_is_fenced_without_an_artifact",
            "tests/contracts/memory/test_conformance.py::test_partial_failure_and_lost_response_are_safe_to_retry",
            "tests/memory/cognee/test_indexing.py::test_remove_tombstones_source_and_repeated_remove_does_not_readd_content",
            "tests/memory/cognee/test_retrieval.py::test_retrieval_does_not_return_content_when_evidence_is_stale",
            "tests/memory/cognee/test_retrieval.py::test_retrieval_does_not_return_content_for_unsafe_evidence",
            "tests/memory/cognee/test_recovery.py::test_repair_fences_tombstoned_source_without_reintroducing_artifacts",
        ),
    ),
    TraceabilityRow(
        6,
        (
            "tests/contracts/memory/test_acl_leakage_contract.py::test_denied_scope_has_no_content_or_sensitive_diagnostic",
            "tests/memory/cognee/test_retrieval.py::test_retrieval_fails_closed_for_cross_scope_physical_metadata",
            "tests/memory/cognee/test_indexing.py::test_foreign_workspace_effect_is_denied_before_physical_write",
            "tests/application/test_projection_result_mapping.py::test_foreign_workspace_effect_envelope_is_rejected",
        ),
    ),
    TraceabilityRow(
        7,
        (
            "tests/contracts/memory/test_backend_contract.py::test_backend_implements_every_context_projection_operation",
            "tests/memory/cognee/test_indexing.py::test_adapter_rejects_an_unqualified_cognee_version",
            "tests/memory/cognee/test_retrieval.py::test_public_sdk_bridge_uses_only_public_search_and_search_type",
        ),
    ),
    TraceabilityRow(
        8,
        (
            "tests/qualification/test_issue211_qualification.py::test_issue211_report_is_machine_readable_and_disclaims_production_approval",
            "tests/qualification/test_issue211_qualification.py::test_issue211_traceability_evidence_nodes_are_collectable",
            "tests/qualification/test_issue211_qualification.py::test_issue211_offline_adapter_suite_passes",
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


def test_issue211_traceability_matrix_covers_exactly_ac1_to_ac8() -> None:
    assert tuple(row.acceptance_criterion for row in TRACEABILITY_MATRIX) == tuple(
        range(1, 9)
    )
    assert all(row.evidence for row in TRACEABILITY_MATRIX)
    assert all(len(row.evidence) == len(set(row.evidence)) for row in TRACEABILITY_MATRIX)


def test_issue211_traceability_document_matches_the_executable_matrix() -> None:
    content = TRACEABILITY.read_text(encoding="utf-8")
    rows = {
        int(match.group(1)): tuple(
            re.findall(r"`(tests/[^`]+?::test_[^`]+)`", match.group(2))
        )
        for match in re.finditer(r"^\| AC(\d+) \| (.+?) \|$", content, re.MULTILINE)
    }

    assert tuple(rows) == tuple(range(1, 9))
    assert rows == {
        row.acceptance_criterion: row.evidence for row in TRACEABILITY_MATRIX
    }


def test_issue211_report_is_machine_readable_and_disclaims_production_approval() -> None:
    report = _report()

    assert report["schema_version"] == "context-projection-qualification:v1"
    assert report["ticket"] == 211
    assert report["mode"] == "offline_deterministic_fake"
    assert report["release_gate"] == {"status": "passed", "provider_qualification": "not_approved"}
    assert report["criteria"] == [
        {
            "id": f"AC{row.acceptance_criterion}",
            "status": "passed",
            "evidence": list(row.evidence),
        }
        for row in TRACEABILITY_MATRIX
    ]
    assert report["production_approval"] is False
    limitations = report["limitations"]
    assert isinstance(limitations, list)
    assert any("fake-backed" in item for item in limitations)
    assert any("Cognee" in item for item in limitations)


def test_issue211_traceability_evidence_nodes_are_collectable() -> None:
    nodes = _evidence_nodes()
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *nodes],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    output = collected.stdout + collected.stderr
    assert collected.returncode == 0, output
    assert all(node in output for node in nodes)


def test_issue211_offline_adapter_suite_passes() -> None:
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
