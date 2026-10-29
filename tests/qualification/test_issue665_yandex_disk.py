"""Executable offline acceptance evidence for issue #665."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

SCENARIOS: dict[str, tuple[str, ...]] = {
    "refs_only_admission_and_two_workspace_denial": (
        "tests/connectors/test_yandex_disk.py::test_wrong_scope_and_workspace_are_denied_before_provider_content_read",
        "tests/connectors/test_yandex_disk.py::test_two_workspaces_keep_equivalent_files_in_distinct_source_identities",
        "tests/contracts/memory/test_context_broker_v1.py::test_fake_broker_keeps_two_workspace_requests_isolated",
    ),
    "official_metadata_exact_bytes_and_identity_assurance": (
        "tests/connectors/test_yandex_disk.py::test_binary_revision_evidence_must_match_exact_original_bytes",
        "tests/connectors/test_yandex_disk.py::test_reader_preserves_official_metadata_exact_bytes_and_path_scoped_identity",
        "tests/connectors/test_yandex_disk.py::test_capabilities_do_not_emulate_stable_identity_or_incremental_changes",
    ),
    "pagination_duplicate_throttle_revocation_and_partial_read": (
        "tests/connectors/test_yandex_disk.py::test_throttled_listing_retries_without_duplicate_observation",
        "tests/connectors/test_yandex_disk.py::test_listing_limits_bound_provider_controlled_pagination",
        "tests/connectors/test_yandex_disk.py::test_transient_outage_uses_bounded_backoff_and_jitter",
        "tests/connectors/test_yandex_disk.py::test_partial_download_and_concurrent_revision_never_emit_observation",
        "tests/connectors/test_yandex_disk.py::test_size_limit_and_provider_revocation_fail_before_observation",
        "tests/connectors/test_yandex_disk.py::test_wrong_scope_and_workspace_are_denied_before_provider_content_read",
    ),
    "canonical_revision_replay_projection_and_no_business_authority": (
        "tests/application/test_connector_ingestion.py::test_yandex_file_replay_keeps_one_immutable_revision_and_projection_effect",
    ),
    "authorized_context_broker_exact_revision_lineage": (
        "tests/application/test_authorized_context_broker.py::test_allowed_request_persists_exact_bundle_and_replays_without_second_read",
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


def test_issue665_traceability_points_to_executable_public_seams() -> None:
    for node_id in (node for scenario in SCENARIOS.values() for node in scenario):
        relative_path, test_name = node_id.split("::", 1)
        assert test_name in _test_names(ROOT / relative_path), node_id


def test_yandex_adapter_does_not_import_proposal_or_memory_implementations() -> None:
    source = (ROOT / "src/spine/connectors/yandex_disk.py").read_text(encoding="utf-8")

    assert "PricingBasis" not in source
    assert "proposal" not in source.lower()
    assert "cognee" not in source.lower()
    assert "spine.memory" not in source
