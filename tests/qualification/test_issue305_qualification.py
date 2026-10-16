"""Executable traceability checks for the complete R1 control-plane API."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TRACEABILITY = PROJECT_ROOT / "docs/qualification/issue-305-control-plane-api.md"
REPORT = PROJECT_ROOT / "docs/qualification/issue-305-control-plane-api-report.json"


OFFLINE_TARGETS = (
    "tests/api",
    "tests/application/test_answer_outcomes.py",
    "tests/application/test_answer_question_candidate.py",
    "tests/application/test_answer_question_context.py",
    "tests/application/test_attempt_revalidation.py",
    "tests/application/test_authorized_context_broker.py",
    "tests/application/test_batch_ingestion.py",
    "tests/application/test_citation_inspector.py",
    "tests/application/test_disclosure_revalidation.py",
    "tests/application/test_document_ingestion.py",
    "tests/application/test_document_queries.py",
    "tests/application/test_feedback.py",
    "tests/application/test_ingestion_history.py",
    "tests/application/test_parsed_content_inspection.py",
    "tests/application/test_projection_control.py",
    "tests/application/test_qa_sessions.py",
    "tests/application/test_qa_streaming.py",
    "tests/application/qa_events/test_event_log.py",
    "tests/application/diagnostics/test_errors.py",
    "tests/application/persistence/test_answer_question_runs.py",
    "tests/application/persistence/test_idempotency_replay_authorization.py",
    "tests/application/persistence/test_ingestion.py",
    "tests/application/persistence/test_parsed_artifacts.py",
    "tests/application/persistence/test_projection_activation.py",
    "tests/contracts/memory",
    "tests/contracts/persistence/test_answer_question_run_repository_contract.py",
    "tests/contracts/persistence/test_idempotency_repository_contract.py",
    "tests/contracts/persistence/test_ingestion_repository_contract.py",
    "tests/evals/test_qa_r1_dataset.py",
    "tests/evals/test_regression_gate.py",
    "tests/infrastructure/persistence/test_answer_disclosure_schema.py",
    "tests/infrastructure/persistence/test_answer_question_run_schema.py",
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
        "api-surface-and-generated-contract",
        (
            "tests/api/test_openapi_handoff.py::test_generation_export_matches_backend_openapi_document",
            "tests/api/test_contracts.py::test_response_envelopes_are_versioned_scoped_closed_and_immutable",
            "tests/api/test_authorization.py::test_protected_route_openapi_declares_bearer_and_required_scope_headers",
            "tests/api/test_documents.py::test_authorized_document_collection_returns_only_safe_operational_metadata",
            "tests/api/test_ingestion.py::test_authorized_upload_returns_stable_status_reference_and_streams_chunks",
            "tests/api/test_projection_control.py::test_projection_status_is_scoped_and_recovery_returns_typed_command_result",
            "tests/api/test_qa_sessions.py::test_session_create_replay_and_message_read_use_stable_scoped_contracts",
            "tests/api/test_authorized_ask.py::test_authorized_ask_returns_only_accepted_answer_and_citation_references",
            "tests/api/test_qa_runs.py::test_run_detail_exposes_pinned_attempt_metadata_and_safe_timeline",
            "tests/api/test_feedback.py::test_feedback_route_records_exact_citation_feedback",
            "tests/api/test_health_summary.py::test_administrator_health_summary_is_scoped_and_discloses_only_safe_metadata",
        ),
    ),
    TraceabilityRow(
        2,
        "authorization-and-tenant-environment-isolation",
        (
            "tests/api/test_authorization.py::test_valid_bearer_resolves_trusted_actor_scope_and_trace",
            "tests/api/test_authorization.py::test_missing_or_foreign_scope_returns_safe_403_before_handler",
            "tests/api/test_documents.py::test_missing_and_foreign_documents_fail_without_enumeration",
            "tests/api/test_ingestion.py::test_cross_workspace_upload_is_denied_without_leaking_service_or_content",
            "tests/api/test_batch_ingestion.py::test_valid_second_workspace_cannot_observe_first_workspace_run",
            "tests/api/test_ingestion_history.py::test_history_denies_foreign_scope_without_enumerating_rows",
            "tests/api/test_projection_control.py::test_projection_control_rejects_foreign_scope_before_service_execution",
            "tests/api/test_qa_sessions.py::test_administrator_without_content_grant_receives_metadata_only_message",
            "tests/application/test_citation_inspector.py::test_citation_identity_isolated_between_workspaces_and_environments",
            "tests/api/test_feedback.py::test_feedback_denies_foreign_scope_and_unknown_target_without_enumeration",
            "tests/api/test_health_summary.py::test_health_summary_isolated_for_two_authorized_workspace_environments",
        ),
    ),
    TraceabilityRow(
        3,
        "pagination-idempotency-and-lifecycle",
        (
            "tests/api/test_contracts.py::test_cursor_is_bound_to_scope_operation_limit_and_filters",
            "tests/api/test_contracts.py::test_idempotency_key_is_bounded_and_rejects_ambiguous_values",
            "tests/api/test_documents.py::test_document_cursor_is_bound_to_exact_filters",
            "tests/api/test_documents.py::test_revision_history_is_cursor_paginated_and_cursor_tampering_fails_closed",
            "tests/api/test_ingestion.py::test_same_upload_replays_with_stable_identity",
            "tests/api/test_ingestion.py::test_same_upload_key_with_changed_content_returns_safe_conflict",
            "tests/api/test_batch_ingestion.py::test_same_batch_replays_with_stable_run_id",
            "tests/api/test_batch_ingestion.py::test_same_idempotency_key_with_changed_content_conflicts",
            "tests/api/test_ingestion_history.py::test_history_cursor_is_scope_bound_and_replay_safe",
            "tests/api/test_projection_control.py::test_projection_recovery_is_unavailable_when_worker_service_is_not_composed",
            "tests/api/test_qa_sessions.py::test_message_route_requires_idempotency_and_safe_not_found",
            "tests/api/test_authorized_ask.py::test_authorized_ask_same_key_replays_stable_result",
            "tests/api/test_feedback.py::test_feedback_route_records_safe_result_and_replays_idempotently",
            "tests/api/test_parsed_content.py::test_parsed_content_ready_response_has_typed_locator_and_scope_bound_cursor",
            "tests/api/test_documents.py::test_document_detail_represents_tombstone_processing_and_stale_degraded_publication",
        ),
    ),
    TraceabilityRow(
        4,
        "qa-outcomes-lineage-and-disclosure",
        (
            "tests/api/test_authorized_ask.py::test_authorized_ask_maps_each_terminal_outcome_without_renaming",
            "tests/api/test_qa_runs.py::test_run_detail_returns_metadata_only_and_safe_not_found",
            "tests/api/test_citation_inspector.py::test_citation_inspector_uses_safe_error_for_unavailable_content",
            "tests/application/test_answer_outcomes.py::test_application_resolver_maps_typed_validator_fake_without_diagnostic_detail",
            "tests/application/test_answer_question_context.py::test_revalidation_failure_blocks_pre_disclosure_and_never_returns_answered",
            "tests/application/test_disclosure_revalidation.py::test_current_requester_revocation_suppresses_history_without_content_reason",
            "tests/application/test_citation_inspector.py::test_fabricated_citation_identity_or_digest_fails_closed_without_excerpt",
            "tests/application/test_qa_sessions.py::test_message_content_is_hidden_when_current_disclosure_is_revoked",
        ),
    ),
    TraceabilityRow(
        5,
        "resumable-sse-and-terminal-safety",
        (
            "tests/api/test_qa_stream.py::test_sse_streams_accepted_ask_events_and_resumes_after_last_event_id",
            "tests/api/test_qa_stream.py::test_sse_revalidates_protected_events_and_emits_only_safe_denial",
            "tests/api/test_qa_stream.py::test_sse_rejects_a_cursor_from_another_message_without_event_leakage",
            "tests/application/test_qa_streaming.py::test_publishing_safe_ask_result_creates_resumable_terminal_stream",
            "tests/application/test_qa_streaming.py::test_publishing_same_ask_result_is_idempotent_and_does_not_duplicate_terminal",
            "tests/application/test_qa_streaming.py::test_retry_after_partial_event_persistence_has_one_logical_effect",
            "tests/application/test_qa_streaming.py::test_completion_wins_a_later_cancellation_without_a_second_terminal_event",
            "tests/application/qa_events/test_event_log.py::test_stream_rehydrates_terminal_event_after_disconnect",
            "tests/application/qa_events/test_event_log.py::test_heartbeat_retry_is_idempotent_after_reconnect",
            "tests/application/qa_events/test_event_log.py::test_authorization_change_replaces_protected_emission_with_safe_denial",
            "tests/application/qa_events/test_event_log.py::test_stream_rejects_candidate_output_before_the_disclosure_gate",
        ),
    ),
    TraceabilityRow(
        6,
        "diagnostics-and-legacy-compatibility",
        (
            "tests/api/test_diagnostics.py::test_unexpected_api_failure_is_generic_and_keeps_request_trace",
            "tests/api/test_diagnostics.py::test_invalid_cursor_uses_safe_structured_400_without_cursor_details",
            "tests/api/test_diagnostics.py::test_legacy_ask_bodies_remain_compatible_with_trace_headers",
            "tests/api/test_diagnostics.py::test_legacy_ingest_body_remains_compatible_with_trace_header",
            "tests/api/test_health_summary.py::test_public_readiness_timeout_returns_the_existing_safe_failure_shape",
            "tests/api/test_ingestion.py::test_status_exposes_acceptance_processing_and_receipt_without_content",
            "tests/api/test_batch_ingestion.py::test_batch_polling_returns_all_items",
        ),
    ),
    TraceabilityRow(
        7,
        "persistence-evaluation-and-architecture-gates",
        (
            "tests/application/persistence/test_idempotency_replay_authorization.py::test_replay_revalidates_current_authorization_and_stores_only_reference",
            "tests/application/persistence/test_answer_question_runs.py::test_context_bundle_lineage_belongs_to_exact_answer_question_attempt",
            "tests/contracts/persistence/test_idempotency_repository_contract.py::test_independent_workspaces_environments_and_keys_do_not_collide",
            "tests/contracts/persistence/test_ingestion_repository_contract.py::test_ingestion_run_item_contract",
            "tests/infrastructure/persistence/test_answer_question_run_schema.py::test_answer_question_run_mapping_keeps_scope_idempotency_and_safe_payload",
            "tests/infrastructure/persistence/test_answer_disclosure_schema.py::test_answer_disclosure_mapping_is_tenant_scoped_and_payload_backed",
            "tests/evals/test_qa_r1_dataset.py::test_r1_dataset_manifest_is_ordered_for_replay",
            "tests/evals/test_regression_gate.py::test_offline_regression_gate_passes_reproducible_r1_contract",
            "tests/test_architecture_scaffold.py::ArchitectureScaffoldTests::test_domain_layer_has_no_framework_imports",
        ),
    ),
    TraceabilityRow(
        8,
        "qualification-boundaries-and-reporting",
        (
            "tests/qualification/test_issue305_qualification.py::test_issue305_report_is_machine_readable_and_separates_readiness_claims",
            "tests/qualification/test_issue305_qualification.py::test_issue305_report_names_external_prerequisites_and_native_dependencies",
            "tests/qualification/test_issue305_qualification.py::test_issue305_parent_relationship_remains_open_and_blockers_are_closed",
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
            if "test_issue305_qualification.py" not in evidence
        )
    )


def _node_name(node_text: str) -> str:
    return node_text.split("[", maxsplit=1)[0].rsplit("::", maxsplit=1)[-1]


def test_issue305_traceability_matrix_covers_exactly_all_acceptance_criteria() -> None:
    allowed_levels = {
        "api-surface-and-generated-contract",
        "authorization-and-tenant-environment-isolation",
        "pagination-idempotency-and-lifecycle",
        "qa-outcomes-lineage-and-disclosure",
        "resumable-sse-and-terminal-safety",
        "diagnostics-and-legacy-compatibility",
        "persistence-evaluation-and-architecture-gates",
        "qualification-boundaries-and-reporting",
    }

    assert tuple(row.acceptance_criterion for row in TRACEABILITY_MATRIX) == tuple(
        range(1, 9)
    )
    assert all(row.evidence for row in TRACEABILITY_MATRIX)
    assert all(len(row.evidence) == len(set(row.evidence)) for row in TRACEABILITY_MATRIX)
    assert all(row.test_level in allowed_levels for row in TRACEABILITY_MATRIX)


def test_issue305_traceability_evidence_points_to_real_test_nodes() -> None:
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


def test_issue305_traceability_document_matches_executable_matrix() -> None:
    content = TRACEABILITY.read_text(encoding="utf-8")
    rows = {
        int(match.group(1)): tuple(
            re.findall(r"`(tests/[^`]+?::(?:test_|ArchitectureScaffoldTests::test_)[^`]+)`", match.group(2))
        )
        for match in re.finditer(r"^\| AC(\d+) \| .+? \| (.+?) \|$", content, re.MULTILINE)
    }

    assert tuple(rows) == tuple(range(1, 9))
    assert rows == {
        row.acceptance_criterion: row.evidence for row in TRACEABILITY_MATRIX
    }


def test_issue305_traceability_evidence_nodes_are_collectable() -> None:
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


def test_issue305_offline_evidence_suite_passes() -> None:
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


def test_issue305_report_is_machine_readable_and_separates_readiness_claims() -> None:
    report = _report()

    assert report["schema_version"] == "r1-control-plane-api-qualification:v1"
    assert report["ticket"] == 305
    assert report["parent_ticket"] == 26
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
        "postgresql": "passed_disposable_harness",
        "web_compatibility": "ci_required",
        "provider_qualification": "not_approved",
    }
    assert report["readiness"] == {
        "deterministic_offline": "qualified",
        "postgresql": "qualified_disposable_harness",
        "web_compatibility": "ci_required",
        "integration_prerequisites": "not_run",
        "native_dependencies": "verified_closed",
        "production": "not_approved",
    }
    assert report["production_approval"] is False
    assert report["verification"]["offline"]["status"] == "passed"
    assert report["verification"]["compile_import"]["status"] == "passed"
    assert report["verification"]["postgresql"]["status"] == "passed"
    assert report["verification"]["web_compatibility"]["status"] == "ci_required"
    assert report["verification"]["opt_in_integrations"]["status"] == "skipped"
    assert report["verification"]["failures"] == []


def test_issue305_report_names_external_prerequisites_and_native_dependencies() -> None:
    report = _report()

    prerequisites = report["external_prerequisites"]
    assert isinstance(prerequisites, list)
    assert "SPINE_TEST_DATABASE_URL" in report["postgresql_prerequisite"]["requirement"]
    assert any("PostgreSQL" in item for item in prerequisites)
    assert any("generated frontend client" in item for item in prerequisites)
    assert any("Cognee" in item for item in prerequisites)
    assert any("real provider" in item for item in prerequisites)
    assert report["native_dependencies"] == {
        "294": "closed",
        "295": "closed",
        "296": "closed",
        "298": "closed",
        "300": "closed",
        "302": "closed",
        "303": "closed",
        "304": "closed",
    }


def test_issue305_parent_relationship_remains_open_and_blockers_are_closed() -> None:
    report = _report()

    assert report["parent_relationship"] == {
        "ticket": 26,
        "state": "open",
        "native_dependencies_changed": False,
    }
    assert all(status == "closed" for status in report["native_dependencies"].values())
