"""Executable traceability checks for Issue #338."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TRACEABILITY = PROJECT_ROOT / "docs/qualification/issue-338-knowledge-control-plane.md"
REPORT = (
    PROJECT_ROOT
    / "docs/qualification/issue-338-knowledge-control-plane-report.json"
)


@dataclass(frozen=True, slots=True)
class TraceabilityRow:
    acceptance_criterion: int
    test_level: str
    evidence: tuple[str, ...]


TRACEABILITY_MATRIX = (
    TraceabilityRow(
        1,
        "controlled-browser-journey",
        (
            "web/e2e/app.spec.ts::test_documents_discovery_filters_paginates_and_navigates_without_content_identity",
            "web/e2e/app.spec.ts::test_documents_upload_reviews_batch_and_polls_processing",
            "web/e2e/app.spec.ts::test_ingestion_run_displays_partial_outcomes_retryability_and_safe_diagnostics",
            "web/e2e/app.spec.ts::test_document_retry_polls_durable_operation_and_reloads_its_run",
            "web/e2e/app.spec.ts::test_document_detail_deep_link_reloads_and_preserves_scope_and_subview_navigation",
            "web/e2e/app.spec.ts::test_document_revision_history_direct_url_reloads_exact_selected_revision",
            "web/e2e/app.spec.ts::test_projection_status_scope_switch_reload_and_safe_degradation",
            "web/e2e/app.spec.ts::test_projection_recovery_receipt_polls_status_until_server_reports_completion",
            "web/e2e/app.spec.ts::test_documents_loading_state_is_named_while_the_controlled_response_is_pending",
            "web/e2e/app.spec.ts::test_documents_empty_state_offers_a_safe_retry_action",
            "web/e2e/app.spec.ts::test_documents_validation_failure_hides_protected_diagnostics",
            "web/e2e/app.spec.ts::test_documents_partial_state_distinguishes_stale_degraded_and_unpublished",
            "web/e2e/app.spec.ts::test_documents_failed_state_hides_provider_diagnostics",
        ),
    ),
    TraceabilityRow(
        2,
        "protected-content-disclosure",
        (
            "web/e2e/app.spec.ts::test_document_content_inspection_renders_exact_revision_and_locator",
            "web/e2e/app.spec.ts::test_revoked_content_authorization_removes_cached_excerpt_for_administrator",
            "web/src/features/knowledge/ParsedContentInspector.test.tsx::test_denied_inspection_hides_payload_and_exposes_permission_state",
            "tests/application/test_parsed_content_inspection.py::test_administrator_without_current_read_content_receives_no_segment",
        ),
    ),
    TraceabilityRow(
        3,
        "lifecycle-idempotency-and-lineage",
        (
            "web/e2e/app.spec.ts::test_document_lifecycle_actions_are_scope_owned_and_preserve_tombstone_lineage",
            "web/e2e/app.spec.ts::test_document_reprocess_uses_current_revision_and_conflict_requires_refresh",
            "web/e2e/app.spec.ts::test_document_retry_replay_failed_cancelled_and_denied_states_are_safe",
            "web/e2e/app.spec.ts::test_document_retry_reuses_idempotency_key_and_returns_stable_replay_receipt",
            "tests/api/test_document_lifecycle_commands.py::test_reused_key_with_changed_lifecycle_data_returns_safe_conflict",
            "tests/application/test_document_lifecycle_commands.py::test_tombstone_same_key_replays_without_rechecking_the_old_current_head",
        ),
    ),
    TraceabilityRow(
        4,
        "contracts-accessibility-and-build",
        (
            "tests/api/test_openapi_handoff.py::test_generation_export_matches_backend_openapi_document",
            "web/src/app/states.test.tsx::test_%s_state_has_a_named_safe_panel",
            "web/src/features/knowledge/DocumentLifecycleActions.test.tsx::test_action_forms_expose_accessible_labels_without_physical_delete_control",
            "web/src/features/knowledge/ProcessingCommandAction.test.tsx::test_retry_posts_server_marked_failure_polls_status_and_preserves_scope_in_run_link",
        ),
    ),
    TraceabilityRow(
        5,
        "scope-authentication-and-url-isolation",
        (
            "web/e2e/app.spec.ts::test_control_plane_navigation_preserves_scope_and_generated_client_context",
            "web/e2e/app.spec.ts::test_documents_discovery_filters_paginates_and_navigates_without_content_identity",
            "web/e2e/app.spec.ts::test_ingestion_run_displays_partial_outcomes_retryability_and_safe_diagnostics",
            "web/e2e/app.spec.ts::test_document_detail_deep_link_reloads_and_preserves_scope_and_subview_navigation",
            "web/e2e/app.spec.ts::test_document_lifecycle_actions_are_scope_owned_and_preserve_tombstone_lineage",
            "web/e2e/app.spec.ts::test_projection_status_scope_switch_reload_and_safe_degradation",
            "web/e2e/app.spec.ts::test_authentication_denial_hides_administrator_shell_without_api_request",
            "tests/api/test_documents.py::test_missing_and_foreign_documents_fail_without_enumeration",
            "tests/api/test_batch_ingestion.py::test_valid_second_workspace_cannot_observe_first_workspace_run",
            "tests/api/test_projection_control.py::test_projection_control_rejects_foreign_scope_before_service_execution",
        ),
    ),
    TraceabilityRow(
        6,
        "qualification-boundaries",
        (
            "tests/qualification/test_issue338_qualification.py::test_issue338_report_separates_all_readiness_levels",
            "tests/qualification/test_issue338_qualification.py::test_issue338_report_names_unexercised_external_prerequisites",
        ),
    ),
    TraceabilityRow(
        7,
        "parent-and-dependency-guardrails",
        (
            "tests/qualification/test_issue338_qualification.py::test_issue338_parent_and_related_issues_remain_unchanged",
            "tests/qualification/test_issue338_qualification.py::test_issue338_direct_blockers_and_api_prerequisites_are_closed",
        ),
    ),
)


def _report() -> dict[str, object]:
    return json.loads(REPORT.read_text(encoding="utf-8"))


def _external_evidence() -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            evidence
            for row in TRACEABILITY_MATRIX
            for evidence in row.evidence
            if "test_issue338_qualification.py" not in evidence
        )
    )


def test_issue338_traceability_matrix_covers_exactly_all_acceptance_criteria() -> None:
    assert tuple(row.acceptance_criterion for row in TRACEABILITY_MATRIX) == tuple(
        range(1, 8)
    )
    assert all(row.evidence for row in TRACEABILITY_MATRIX)
    assert all(len(row.evidence) == len(set(row.evidence)) for row in TRACEABILITY_MATRIX)


def test_issue338_traceability_evidence_points_to_real_test_nodes() -> None:
    for evidence in _external_evidence():
        path_text, node_name = evidence.split("::", maxsplit=1)
        source = (PROJECT_ROOT / path_text).read_text(encoding="utf-8")
        escaped = re.escape(node_name).replace(r"%s", ".+")
        if path_text.endswith(".py"):
            pattern = rf"(?:async )?def {escaped}\("
        else:
            pattern = rf"(?:test|it)(?:\.each\([^;]+?\))?\(\s*['\"]{escaped}['\"]"
        assert re.search(pattern, source, re.DOTALL), evidence


def test_issue338_traceability_document_matches_executable_matrix() -> None:
    content = TRACEABILITY.read_text(encoding="utf-8")
    rows = {
        int(match.group(1)): tuple(re.findall(r"`((?:tests|web)/[^`]+?::[^`]+)`", match.group(2)))
        for match in re.finditer(
            r"^\| AC(\d+) \| .+? \| (.+?) \|$", content, re.MULTILINE
        )
    }
    assert tuple(rows) == tuple(range(1, 8))
    assert rows == {
        row.acceptance_criterion: row.evidence for row in TRACEABILITY_MATRIX
    }


def test_issue338_report_records_required_gates() -> None:
    report = _report()
    assert report["verification"] == {
        "generated_contract": {
            "command": "corepack pnpm web:api:check",
            "status": "passed",
        },
        "typecheck": {
            "command": "corepack pnpm web:typecheck",
            "status": "passed",
        },
        "production_build": {
            "command": "corepack pnpm web:build",
            "status": "passed",
        },
        "vitest_rtl_accessibility": {
            "command": "corepack pnpm web:test",
            "status": "passed",
        },
        "controlled_playwright": {
            "command": "corepack pnpm web:test:e2e",
            "status": "passed",
        },
        "python_api_contracts": {
            "command": "uv run pytest -q tests/api tests/application/test_parsed_content_inspection.py tests/qualification/test_issue338_qualification.py",
            "status": "passed",
        },
        "repository_suite": {
            "command": "uv run pytest -q",
            "status": "passed",
        },
        "compile_import": {
            "command": "uv run python -m compileall -q src tests",
            "status": "passed",
        },
    }


def test_issue338_report_separates_all_readiness_levels() -> None:
    report = _report()
    assert report["schema_version"] == "knowledge-control-plane-qualification:v1"
    assert report["ticket"] == 338
    assert report["production_approval"] is False
    assert report["readiness"] == {
        "contract_test": "qualified",
        "fake_backed_ui": "qualified",
        "api_integration": "qualified_offline_asgi",
        "production": "not_qualified",
    }
    assert report["criteria"] == [
        {
            "id": f"AC{row.acceptance_criterion}",
            "status": "qualified_offline",
            "evidence": list(row.evidence),
        }
        for row in TRACEABILITY_MATRIX
    ]


def test_issue338_report_names_unexercised_external_prerequisites() -> None:
    prerequisites = _report()["unexercised_prerequisites"]
    assert isinstance(prerequisites, dict)
    assert prerequisites == {
        "postgresql": "not_run_against_configured_production",
        "parser": "not_run_with_untrusted_real_documents",
        "projection": "not_run_with_configured_cognee_neo4j_pgvector",
        "provider": "not_run_with_real_model_provider",
        "production_api": "not_run_against_deployed_api",
    }


def test_issue338_parent_and_related_issues_remain_unchanged() -> None:
    assert _report()["protected_issue_states"] == {
        "16": "open",
        "22": "open",
        "23": "open",
        "26": "closed",
    }


def test_issue338_direct_blockers_and_api_prerequisites_are_closed() -> None:
    report = _report()
    assert report["direct_ui_dependencies"] == {
        str(ticket): "closed"
        for ticket in (326, 328, 329, 331, 333, 334, 335, 336, 337)
    }
    assert report["api_prerequisites"] == {
        str(ticket): "closed" for ticket in range(339, 345)
    }
