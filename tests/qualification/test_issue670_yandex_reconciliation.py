"""Executable offline acceptance evidence for issue #670."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

SCENARIOS: dict[str, tuple[str, ...]] = {
    "opaque_checkpoint_commit_and_interrupted_recovery": (
        "tests/application/test_yandex_disk_reconciliation.py::test_pages_commit_opaque_checkpoint_only_after_canonical_effects_are_accepted",
        "tests/application/test_yandex_disk_reconciliation.py::test_interrupted_page_replays_with_stable_item_identity_and_keeps_prior_checkpoint",
        "tests/application/test_yandex_disk_reconciliation.py::test_interrupted_tombstone_resumes_without_rescanning_or_committing_terminal_checkpoint",
        "tests/application/test_connector_ingestion.py::test_yandex_reconciliation_recovers_through_canonical_ingestion_and_outbox_seam",
    ),
    "qualified_bounded_full_scan": (
        "tests/application/test_yandex_disk_reconciliation.py::test_scan_admission_rejects_unqualified_full_reconciliation_before_provider_call",
        "tests/connectors/test_yandex_disk.py::test_full_scan_uses_opaque_durable_checkpoints_and_complete_official_pages",
        "tests/connectors/test_yandex_disk.py::test_capabilities_do_not_emulate_stable_identity_or_incremental_changes",
        "tests/application/test_yandex_disk_reconciliation.py::test_provider_reordering_cannot_certify_complete_coverage_or_tombstone",
        "tests/connectors/test_yandex_disk.py::test_full_scan_rejects_same_count_unique_substitution_without_stable_generation",
    ),
    "new_change_absence_rename_and_reappearance": (
        "tests/application/test_connector_ingestion.py::test_yandex_file_replay_keeps_one_immutable_revision_and_projection_effect",
        "tests/application/test_connector_ingestion.py::test_yandex_reconciliation_recovers_through_canonical_ingestion_and_outbox_seam",
        "tests/application/test_yandex_disk_reconciliation.py::test_complete_scope_rename_records_new_path_and_tombstones_old_path_without_continuity",
        "tests/application/test_yandex_disk_reconciliation.py::test_changed_revision_records_history_without_lifecycle_deletion",
        "tests/application/test_yandex_disk_reconciliation.py::test_complete_absence_without_deletion_policy_remains_ambiguous",
        "tests/application/test_yandex_disk_reconciliation.py::test_path_scoped_reappearance_creates_new_source_and_records_non_equivalence",
        "tests/connectors/test_yandex_disk.py::test_path_scoped_reappearance_occurrence_never_claims_identity_continuity",
        "tests/application/test_source_lifecycle.py::test_reappearance_requires_existing_tombstone_and_fresh_canonical_evidence",
    ),
    "failure_states_and_duplicate_pages": (
        "tests/application/test_yandex_disk_reconciliation.py::test_incomplete_and_provider_failures_record_distinct_state_without_tombstone",
        "tests/application/test_yandex_disk_reconciliation.py::test_duplicate_page_item_reuses_accepted_effect_and_emits_one_lifecycle_record",
        "tests/application/test_yandex_disk_reconciliation.py::test_repeated_identical_failure_is_idempotent_in_lifecycle_history",
        "tests/application/test_yandex_disk_reconciliation.py::test_removal_policy_admission_rejects_caller_mutation_before_scan",
    ),
    "durable_tenant_scoped_ledger": (
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_yandex_reconciliation_repository_persists_checkpoint_and_tenant_scope",
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


def test_issue670_traceability_points_to_executable_public_seams() -> None:
    for node_id in (node for scenario in SCENARIOS.values() for node in scenario):
        relative_path, test_name = node_id.split("::", 1)
        assert test_name in _test_names(ROOT / relative_path), node_id


def test_reconciliation_module_does_not_import_memory_or_proposal_implementations() -> None:
    source = (
        ROOT / "src/spine/application/yandex_disk_reconciliation.py"
    ).read_text(encoding="utf-8")

    assert "PricingBasis" not in source
    assert "proposal" not in source.lower()
    assert "cognee" not in source.lower()
    assert "spine.memory" not in source
