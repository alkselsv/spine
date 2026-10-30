"""Executable offline acceptance evidence for issue #682."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

SCENARIOS: dict[str, tuple[str, ...]] = {
    "discriminated_document_or_transcript_input": (
        "tests/domain/capabilities/test_extract_requirements.py::test_extract_requirements_input_accepts_discriminated_document_and_transcript_evidence",
        "tests/domain/capabilities/test_extract_requirements.py::test_extract_requirements_input_rejects_discriminator_reference_mismatch",
    ),
    "candidate_categories_and_exact_time_ranges": (
        "tests/application/test_extract_requirements.py::test_transcript_fixture_extracts_all_candidate_categories_with_exact_time_ranges",
    ),
    "uncertainty_clarification_and_untrusted_instructions": (
        "tests/application/test_extract_requirements.py::test_weak_unknown_and_contradictory_transcript_requests_clarification",
        "tests/application/test_extract_requirements.py::test_transcript_instructions_cannot_change_policy_invoke_tools_or_create_pricing",
    ),
    "revocation_stale_fabricated_and_workspace_isolation": (
        "tests/application/test_extract_requirements.py::test_revoked_transcript_abstains_without_disclosing_cached_text",
        "tests/application/test_extract_requirements.py::test_stale_or_fabricated_transcript_locator_abstains",
        "tests/application/test_extract_requirements.py::test_handoff_rejects_fabricated_transcript_reference",
        "tests/api/test_requirements_artifacts.py::test_artifact_api_hides_cross_workspace_records_as_not_found",
        "tests/api/test_requirements_artifacts.py::test_evidence_inspection_hides_cross_workspace_artifact",
    ),
}


def _test_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def test_issue682_traceability_points_to_executable_public_seams() -> None:
    for node_id in (node for scenario in SCENARIOS.values() for node in scenario):
        relative_path, test_name = node_id.split("::", 1)
        assert test_name in _test_names(ROOT / relative_path), node_id


def test_issue682_uses_the_existing_run_artifact_and_transcript_inspector_ui() -> None:
    source = (
        ROOT / "web/src/features/workflows/RequirementsArtifactCard.test.tsx"
    ).read_text(encoding="utf-8")

    assert "opens_exact_transcript_time_evidence_from_the_existing_run_card" in source
    assert "'/api/v1/runs/{run_id}/requirements'" in source
    assert "'/api/v1/transcript-evidence/inspect'" in source

