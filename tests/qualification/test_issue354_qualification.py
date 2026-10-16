"""Executable offline qualification for Context Broker adapter parity (#354)."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TRACEABILITY = PROJECT_ROOT / "docs/qualification/issue-354-context-adapter-parity.md"
REPORT = PROJECT_ROOT / "docs/qualification/issue-354-context-adapter-parity-report.json"
OFFLINE_TARGETS = ("tests/contracts/memory/test_adapter_parity.py",)


@dataclass(frozen=True, slots=True)
class TraceabilityRow:
    acceptance_criterion: int
    evidence: tuple[str, ...]


TRACEABILITY_MATRIX = (
    TraceabilityRow(
        1,
        (
            "tests/contracts/memory/test_adapter_parity.py::test_active_context_brokers_have_equivalent_normalized_outcomes",
            "tests/contracts/memory/test_adapter_parity.py::test_active_legacy_broker_adapter_preserves_normalized_outcomes",
            "tests/contracts/memory/test_adapter_parity.py::test_active_projection_adapters_normalize_retrieval_failures",
        ),
    ),
    TraceabilityRow(
        2,
        (
            "tests/contracts/memory/test_adapter_parity.py::test_active_context_brokers_have_equivalent_normalized_outcomes",
            "tests/contracts/memory/test_adapter_parity.py::test_broker_parity_keeps_partial_distinct_from_complete",
        ),
    ),
    TraceabilityRow(
        3,
        (
            "tests/contracts/memory/test_adapter_parity.py::test_authorized_broker_does_not_call_physical_read_after_denial",
            "tests/contracts/memory/test_acl_leakage_contract.py::test_denied_scope_has_no_content_or_sensitive_diagnostic",
        ),
    ),
    TraceabilityRow(
        4,
        (
            "tests/application/test_authorized_context_broker.py::test_allowed_request_persists_exact_bundle_and_replays_without_second_read",
            "tests/contracts/memory/test_publication_pin.py::test_broker_resolves_one_immutable_pin_and_passes_same_identity_downstream",
            "tests/application/test_attempt_revalidation.py::test_pin_mismatch_rejects_even_when_the_provider_reports_a_healthy_state",
            "tests/application/test_authorized_retrieval_scope.py::test_stale_revision_fails_closed_and_provider_narrowing_is_required_when_declared",
            "tests/contracts/memory/test_adapter_parity.py::test_active_projection_adapters_preserve_score_presence_and_provenance",
        ),
    ),
    TraceabilityRow(
        5,
        (
            "tests/contracts/memory/test_adapter_parity.py::test_active_context_brokers_have_equivalent_normalized_outcomes",
            "tests/contracts/memory/test_conformance.py::test_unsafe_evidence_never_leaks_projected_content",
        ),
    ),
    TraceabilityRow(
        6,
        (
            "tests/contracts/memory/test_adapter_parity.py::test_broker_adapters_map_provider_failures_without_diagnostics",
            "tests/contracts/memory/test_conformance.py::test_unsafe_evidence_never_leaks_projected_content",
        ),
    ),
    TraceabilityRow(
        7,
        (
            "tests/contracts/memory/test_adapter_parity.py::test_active_projection_adapters_preserve_score_presence_and_provenance",
            "tests/contracts/memory/test_retrieval_contract.py::test_retrieval_score_is_optional_and_round_trips_when_provider_supplies_it",
        ),
    ),
    TraceabilityRow(
        8,
        (
            "tests/qualification/test_issue354_qualification.py::test_issue354_offline_parity_suite_passes",
            "tests/qualification/test_issue354_qualification.py::test_issue354_report_is_machine_readable",
        ),
    ),
)


def _report() -> dict[str, object]:
    return json.loads(REPORT.read_text(encoding="utf-8"))


def _evidence_nodes() -> tuple[str, ...]:
    return tuple(dict.fromkeys(node for row in TRACEABILITY_MATRIX for node in row.evidence))


def _node_name(node_text: str) -> str:
    return node_text.split("[", maxsplit=1)[0].rsplit("::", maxsplit=1)[-1]


def test_issue354_traceability_matrix_covers_exactly_ac1_to_ac8() -> None:
    assert tuple(row.acceptance_criterion for row in TRACEABILITY_MATRIX) == tuple(range(1, 9))
    assert all(row.evidence for row in TRACEABILITY_MATRIX)
    assert all(len(row.evidence) == len(set(row.evidence)) for row in TRACEABILITY_MATRIX)


def test_issue354_traceability_document_matches_executable_matrix() -> None:
    content = TRACEABILITY.read_text(encoding="utf-8")
    rows = {
        int(match.group(1)): tuple(re.findall(r"`(tests/[^`]+?::test_[^`]+)`", match.group(2)))
        for match in re.finditer(r"^\| AC(\d+) \| (.+?) \|$", content, re.MULTILINE)
    }
    assert tuple(rows) == tuple(range(1, 9))
    assert rows == {row.acceptance_criterion: row.evidence for row in TRACEABILITY_MATRIX}


def test_issue354_traceability_evidence_nodes_are_real_and_collectable() -> None:
    nodes = _evidence_nodes()
    for evidence in nodes:
        path_text, node_text = evidence.split("::", maxsplit=1)
        path = PROJECT_ROOT / path_text
        assert path.is_file(), evidence
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == _node_name(node_text)
            for node in ast.walk(tree)
        ), evidence

    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *nodes],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    output = collected.stdout + collected.stderr
    assert collected.returncode == 0, output
    assert all(node in output for node in nodes), output


def test_issue354_report_is_machine_readable() -> None:
    report = _report()
    assert report["schema_version"] == "context-adapter-parity-qualification:v1"
    assert report["ticket"] == 354
    assert report["mode"] == "offline_deterministic_fake"
    assert report["release_gate"] == {
        "status": "passed",
        "provider_qualification": "not_approved",
    }
    assert report["criteria"] == [
        {
            "id": f"AC{row.acceptance_criterion}",
            "status": "passed",
            "evidence": list(row.evidence),
        }
        for row in TRACEABILITY_MATRIX
    ]
    assert report["production_approval"] is False
    assert any("Real Cognee" in item for item in report["limitations"])
    assert all(
        set(entry) >= {"adapter", "contract_version", "outcomes", "safety_status"}
        for entry in report["parity_results"]
    )
    assert {entry["adapter"] for entry in report["parity_results"]} == {
        "context-broker-v1",
        "authorized-context-broker",
        "legacy-context-broker",
        "deterministic-context-projection",
        "cognee-context-projection-composition",
    }


def test_issue354_offline_parity_suite_passes() -> None:
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
