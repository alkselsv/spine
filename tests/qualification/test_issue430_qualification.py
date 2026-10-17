from __future__ import annotations

import ast
import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TRACEABILITY = PROJECT_ROOT / "docs/qualification/issue-430-durable-qa-lifecycle.md"
REPORT = PROJECT_ROOT / "docs/qualification/issue-430-durable-qa-lifecycle-report.json"


CRITERIA = {
    "AC1": (
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_ask_admission_replays_persisted_run_message_and_rejects_conflicts",
        "tests/application/persistence/test_ask_admission.py::test_ask_admission_rollback_removes_receipt_run_message_and_intent",
    ),
    "AC2": (
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_ask_admission_replays_persisted_run_message_and_rejects_conflicts",
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_ask_admission_replays_after_application_process_recreation",
        "tests/application/persistence/test_ask_admission.py::test_ask_admission_changed_payload_conflicts_without_aliasing",
        "tests/api/test_authorized_ask.py::test_authorized_ask_denies_other_workspace_without_calling_service",
    ),
    "AC3": (
        "tests/api/test_authorized_ask.py::test_legacy_ask_is_unchanged_and_not_routed_through_authorized_service",
        "tests/api/test_authorized_ask.py::test_ask_admission_returns_stable_ids_without_content_before_execution",
        "tests/api/test_authorized_ask.py::test_ask_admission_payload_conflict_is_safe_409",
        "tests/api/test_authorized_ask.py::test_legacy_cancellation_suppresses_answer_from_completion_race",
    ),
    "AC4": (
        "tests/application/workflows/test_ask_admission_dispatch.py::test_duplicate_admission_delivery_resolves_one_stable_workflow",
        "tests/application/workflows/test_ask_admission_dispatch.py::test_reconciliation_retries_committed_admission_without_second_start",
        "tests/integration/temporal/test_ask_admission_lifecycle.py::test_temporal_ask_workflow_cancellation_signal_returns_terminal_result",
        "tests/integration/temporal/test_ask_admission_lifecycle.py::test_temporal_ask_workflow_resumes_after_worker_restart",
        "tests/integration/temporal/test_ask_admission_lifecycle.py::test_temporal_ask_cancellation_resumes_after_worker_restart",
        "tests/integration/postgresql/test_postgresql_outbox_delivery.py::test_postgresql_expired_lease_rejects_stale_worker",
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_ask_admission_replays_after_application_process_recreation",
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_terminal_persistence_retries_after_commit_failure",
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_temporal_retry_after_terminal_commit_failure_persists_one_postgresql_winner",
    ),
    "AC5": (
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_answer_question_retry_cancellation_contract",
        "tests/application/workflows/test_ask_admission_dispatch.py::test_workflow_cancel_before_start_runs_only_cancellation_activity",
        "tests/api/test_authorized_ask.py::test_legacy_cancellation_suppresses_answer_from_completion_race",
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_ask_completion_cancellation_cas_keeps_first_terminal_winner",
    ),
    "AC6": (
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_ask_admission_replays_persisted_run_message_and_rejects_conflicts",
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_answer_question_retry_cancellation_contract",
        "tests/api/test_authorized_ask.py::test_authorized_ask_returns_only_accepted_answer_and_citation_references",
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_terminal_retry_keeps_run_message_and_accepted_disclosure_consistent",
    ),
    "AC7": (
        "tests/application/workflows/test_ask_admission_dispatch.py::test_scoped_command_loader_rejects_denied_or_mismatched_command",
        "tests/api/test_authorized_ask.py::test_authorized_ask_denies_other_workspace_without_calling_service",
        "tests/integration/postgresql/test_postgresql_outbox_delivery.py::test_runtime_delivery_reads_are_isolated_between_workspaces",
        "tests/api/test_authorized_ask.py::test_durable_ask_denial_isolated_by_workspace_environment_and_subject_without_leaks",
        "tests/api/test_authorized_ask.py::test_cancel_response_does_not_disclose_foreign_run_existence",
        "tests/application/persistence/test_ask_admission.py::test_cancellation_scope_hides_existing_foreign_run_like_unknown_run",
    ),
    "AC8": (
        "tests/api/test_authorized_ask.py::test_legacy_ask_is_unchanged_and_not_routed_through_authorized_service",
        "tests/api/test_authorized_ask.py::test_ask_admission_returns_stable_ids_without_content_before_execution",
        "tests/api/test_authorized_ask.py::test_durable_ask_routes_are_published_in_openapi",
        "tests/api/test_openapi_handoff.py::test_generation_export_matches_backend_openapi_document",
    ),
    "AC9": (
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_ask_admission_replays_persisted_run_message_and_rejects_conflicts",
        "tests/integration/temporal/test_ask_admission_lifecycle.py::test_temporal_ask_workflow_cancellation_signal_returns_terminal_result",
        "tests/integration/temporal/test_ask_admission_lifecycle.py::test_temporal_ask_workflow_resumes_after_worker_restart",
        "tests/integration/temporal/test_ask_admission_lifecycle.py::test_temporal_ask_cancellation_resumes_after_worker_restart",
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_terminal_persistence_retries_after_commit_failure",
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_temporal_retry_after_terminal_commit_failure_persists_one_postgresql_winner",
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_terminal_retry_keeps_run_message_and_accepted_disclosure_consistent",
    ),
}

QUALIFIED_STATUSES = {
    "AC1": "qualified_postgresql_and_offline",
    "AC2": "qualified_postgresql_and_offline_process_recreation",
    "AC3": "qualified_offline",
    "AC4": "qualified_postgresql_outbox_and_temporal_test_server",
    "AC5": "qualified_postgresql_and_offline_both_cas_winners",
    "AC6": "qualified_postgresql_and_offline",
    "AC7": "qualified_workspace_environment_subject_and_no_leak",
    "AC8": "qualified_openapi_and_generated_frontend_types",
    "AC9": "qualified_integrations_and_boundaries_reported",
}


def _node_name(value: str) -> str:
    return value.split("[", maxsplit=1)[0]


def test_issue430_matrix_references_existing_executable_evidence() -> None:
    for criterion, evidence_items in CRITERIA.items():
        assert evidence_items, criterion
        for evidence in evidence_items:
            path_text, node_text = evidence.split("::", maxsplit=1)
            tree = ast.parse((PROJECT_ROOT / path_text).read_text(encoding="utf-8"))
            assert any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == _node_name(node_text)
                for node in ast.walk(tree)
            ), evidence


def test_issue430_report_matches_traceability_and_is_explicit_about_boundaries() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))

    assert report["schema_version"] == "spine.qa.ask-admission-cancellation:v1"
    assert report["ticket"] == 430
    assert report["parent_ticket"] == 407
    assert [item["id"] for item in report["criteria"]] == list(CRITERIA)
    assert {item["id"]: tuple(item["evidence"]) for item in report["criteria"]} == CRITERIA
    assert TRACEABILITY.read_text(encoding="utf-8").startswith(
        "# Issue #430 durable Q&A lifecycle qualification\n"
    )
    assert report["exactly_once_physical_execution"] is False
    assert report["external_provider_qualification"] is False
    assert report["unverified_prerequisites"]
    criteria = {item["id"]: item for item in report["criteria"]}
    assert {key: item["status"] for key, item in criteria.items()} == QUALIFIED_STATUSES
    assert criteria["AC8"]["verified_commands"] == ["corepack pnpm web:check"]
    assert report["frontend_evidence"] == [
        "web/src/features/chat/durable-ask-api-contract.ts",
        "web/src/features/chat/durable-ask-api-contract.test.ts",
    ]
