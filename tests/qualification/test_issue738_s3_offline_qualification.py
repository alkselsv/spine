"""Executable traceability for issue #738 S3 offline qualification."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "docs/qualification/issue-738-s3-offline-qualification.json"
RUNBOOK_PATH = ROOT / "docs/runbooks/r2-s3-offline-qualification.md"


SCENARIO_TESTS: dict[str, tuple[str, ...]] = {
    "lifecycle_classification_and_current_authority": (
        "tests/domain/sources/test_lifecycle.py::test_lifecycle_observation_maps_to_safe_non_authorizing_effect",
        "tests/domain/sources/test_lifecycle.py::test_lifecycle_decision_cannot_convert_deletion_evidence_to_tombstone",
        "tests/integration/postgresql/test_postgresql_lifecycle_authority.py::test_postgresql_lifecycle_authority_persists_current_scoped_decision",
    ),
    "withdrawal_read_revision_and_commit_races": (
        "tests/application/test_connector_ingestion.py::test_scope_withdrawal_blocks_connector_before_provider_read",
        "tests/integration/postgresql/test_postgresql_lifecycle_authority.py::test_postgresql_lifecycle_fence_orders_replacement_and_commit",
        "tests/integration/postgresql/test_postgresql_lifecycle_authority.py::test_postgresql_withdrawal_serializes_after_inflight_canonical_acceptance",
    ),
    "disclosure_review_hold_create_and_release": (
        "tests/application/test_disclosure_holds.py::test_authorized_hold_create_replay_and_current_reader",
        "tests/application/test_disclosure_holds.py::test_separate_release_clears_only_exact_hold_without_granting_disclosure",
        "tests/integration/postgresql/test_postgresql_disclosure_holds.py::test_postgresql_hold_create_release_replay_and_workspace_isolation",
        "tests/integration/postgresql/test_postgresql_disclosure_holds.py::test_postgresql_hold_release_cas_allows_one_immutable_winner",
    ),
    "retention_lineage_mixed_artifacts_and_currentness": (
        "tests/application/test_retention_applicability.py::test_mixed_artifact_reports_ineligible_source_for_suppression_or_rebuild",
        "tests/application/test_retention_applicability.py::test_cross_tenant_context_fails_closed_without_resolving_sources",
        "tests/integration/postgresql/test_postgresql_retention_applicability.py::test_postgresql_retention_reader_enforces_revision_lineage_and_tenant_rls",
        "tests/integration/postgresql/test_postgresql_retention_applicability.py::test_postgresql_retention_fence_orders_replacement_and_commit",
    ),
    "qualified_evidence_issuer_and_revocation": (
        "tests/application/test_tombstone_authorization.py::test_stale_missing_or_unsupported_current_guarantee_fails_closed",
        "tests/application/test_tombstone_authorization.py::test_unknown_commit_reconciles_by_original_command_identity",
        "tests/integration/postgresql/test_postgresql_tombstone_authorization.py::test_postgresql_authorization_replays_one_logical_effect",
        "tests/integration/postgresql/test_postgresql_tombstone_authorization.py::test_postgresql_revocation_makes_issued_authorization_non_current",
        "tests/integration/postgresql/test_postgresql_tombstone_authorization.py::test_postgresql_authorization_is_not_visible_across_workspaces",
    ),
    "canonical_v3_selector_writer_retry_and_cas": (
        "tests/domain/sources/test_canonical_tombstone_v3.py::test_v3_acceptance_requires_exact_tombstone_authorization_link",
        "tests/integration/postgresql/test_postgresql_canonical_tombstone_v3.py::test_tombstone_selector_publication_is_immutable_and_cas_serialized",
        "tests/application/persistence/test_canonical_acceptance_retry.py::test_v3_tombstone_authority_is_revalidated_inside_every_retry_uow",
        "tests/application/persistence/test_canonical_acceptance_retry.py::test_serialization_retry_exhaustion_fails_closed_and_is_audited",
        "tests/application/persistence/test_canonical_acceptance_retry.py::test_unknown_commit_reconciles_with_same_command_identity",
        "tests/integration/postgresql/test_initial_tenancy_migration.py::test_initial_schema_has_named_tenancy_constraints_and_postgresql_types",
    ),
    "projection_retrieval_citation_and_final_disclosure": (
        "tests/application/test_projection_request_resolution.py::test_production_resolver_fences_tombstoned_current_revision_before_physical_io",
        "tests/infrastructure/test_source_usage_guard.py::test_transaction_guard_suppresses_whole_mixed_artifact_on_one_denial",
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_source_usage_guard_fences_authority_changes_until_rollback",
        "tests/application/test_authorized_retrieval_scope.py::test_missing_retention_or_tombstoned_current_revision_fails_closed",
        "tests/application/test_citation_validation.py::test_current_source_lifecycle_fails_closed",
        "tests/application/test_disclosure_revalidation.py::test_current_ineligible_dependency_suppresses_whole_answer",
        "tests/application/test_answer_disclosure.py::test_gate_suppresses_replay_after_current_authority_revocation",
    ),
    "governed_recovery_reappearance_and_no_policy_transfer": (
        "tests/application/test_source_lifecycle.py::test_reappearance_requires_existing_tombstone_and_fresh_canonical_evidence",
        "tests/application/test_source_lifecycle.py::test_reappearance_cannot_transfer_a_pre_tombstone_access_policy",
        "tests/application/test_connector_ingestion.py::test_connector_reappearance_without_governed_authority_fails_closed",
        "tests/application/test_connector_ingestion.py::test_google_drive_reappearance_replay_keeps_the_accepted_lineage",
    ),
    "reader_first_mixed_version_rollout_and_rollback": (
        "tests/domain/sources/test_canonical_tombstone_v3.py::test_historical_versions_cannot_fabricate_tombstone_authorization_link",
        "tests/domain/sources/test_canonical_tombstone_v3.py::test_legacy_selector_is_time_bounded_and_successor_is_immutable_history",
        "tests/application/persistence/test_canonical_acceptance_retry.py::test_legacy_tombstone_without_selector_authority_fails_closed",
        "tests/infrastructure/persistence/test_canonical_tombstone_v3_schema.py::test_v3_reader_first_migration_retains_schema_on_writer_rollback",
        "tests/integration/postgresql/test_postgresql_canonical_tombstone_v3.py::test_v3_reader_first_migration_rollback_retains_read_schema_and_disables_writer",
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_selector_migration_rollback_preserves_existing_v1_acceptance",
        "tests/integration/postgresql/test_initial_tenancy_migration.py::test_every_retained_revision_upgrades_to_one_final_head",
    ),
    "provider_compatibility_and_strict_absence": (
        "tests/application/test_connector_ingestion.py::test_google_v1_and_provider_v2_produce_equivalent_canonical_outcomes",
        "tests/application/test_connector_ingestion.py::test_yandex_file_replay_keeps_one_immutable_revision_and_projection_effect",
        "tests/application/test_connector_ingestion.py::test_mounted_filesystem_observation_reaches_canonical_path_with_stable_replay",
        "tests/application/test_connector_ingestion.py::test_google_sheets_reader_reaches_shared_canonical_and_projection_handoff",
        "tests/application/test_yandex_disk_reconciliation.py::test_provider_reordering_cannot_certify_complete_coverage_or_tombstone",
        "tests/application/test_google_sheets_lifecycle.py::test_incomplete_scan_cannot_create_candidate_or_tombstone",
    ),
}

POSTGRESQL_SCENARIOS = {
    "lifecycle_classification_and_current_authority",
    "withdrawal_read_revision_and_commit_races",
    "disclosure_review_hold_create_and_release",
    "retention_lineage_mixed_artifacts_and_currentness",
    "qualified_evidence_issuer_and_revocation",
    "canonical_v3_selector_writer_retry_and_cas",
    "projection_retrieval_citation_and_final_disclosure",
    "reader_first_mixed_version_rollout_and_rollback",
}


def _test_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def test_issue738_report_traces_every_required_scenario_to_executable_evidence() -> (
    None
):
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

    assert report["schema_version"] == "issue-738-s3-offline-qualification:v1"
    assert report["ticket"] == 738
    assert set(report["required_scenarios"]) == set(SCENARIO_TESTS)
    assert report["traceability"] == {
        scenario: list(node_ids) for scenario, node_ids in SCENARIO_TESTS.items()
    }
    for scenario in POSTGRESQL_SCENARIOS:
        assert any(
            node_id.startswith("tests/integration/postgresql/")
            for node_id in SCENARIO_TESTS[scenario]
        ), scenario
    for node_id in (
        node_id for node_ids in SCENARIO_TESTS.values() for node_id in node_ids
    ):
        relative_path, test_name = node_id.split("::", 1)
        assert test_name in _test_names(ROOT / relative_path), node_id


def test_issue738_runbook_executes_traceability_and_repository_gates() -> None:
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    commands = re.findall(r"```bash\n(.*?)\n```", runbook, flags=re.DOTALL)
    command_text = "\n".join(commands)

    evidence_paths = {
        node_id.split("::", 1)[0]
        for node_ids in SCENARIO_TESTS.values()
        for node_id in node_ids
    }
    for path in evidence_paths:
        assert path in command_text, path
    assert "uv sync --extra dev" in command_text
    assert "uv run pytest -q" in command_text
    assert "uv run python -m compileall -q src tests" in command_text
    assert "SPINE_POSTGRESQL_TEST_URL" in runbook
    assert "production" in runbook.lower()
    assert "#679" in runbook


def test_issue738_report_records_exact_offline_limits_and_verification() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

    assert report["status"] == "offline_pass"
    assert re.fullmatch(r"[0-9a-f]{40}", report["qualified_base_commit"])
    assert report["qualification_commit"] == "WORKTREE"
    for gate in ("focused", "postgresql", "repository"):
        result = report["verification"][gate]
        assert result["status"] == "passed"
        assert result["failed"] == 0
        assert result["passed"] > 0
        assert "<" not in result["command"]
    assert report["verification"]["compileall"] == "passed"
    assert report["reader_before_writer"] == "verified_offline"
    assert report["rollback_preserves_v3_history"] == "verified_offline"
    assert report["live_provider_status"] == "not_exercised"
    assert report["production_activation_status"] == "not_approved"
    assert report["physical_cleanup_status"] == "not_exercised"
    assert report["issue_679_status"] == "not_approved_for_execution"
    assert report["unverified_prerequisites"]


def test_issue738_report_contains_only_synthetic_content_safe_evidence() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    serialized = REPORT_PATH.read_text(encoding="utf-8").lower()

    assert report["privacy"] == {
        "synthetic_fixtures_only": True,
        "credentials_recorded": False,
        "raw_provider_payloads_recorded": False,
        "customer_content_recorded": False,
        "production_data_recorded": False,
    }
    for forbidden in (
        "access_token",
        "refresh_token",
        "authorization: bearer",
        "signed_url",
        "client_secret",
    ):
        assert forbidden not in serialized
