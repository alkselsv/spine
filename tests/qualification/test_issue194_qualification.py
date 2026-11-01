"""Executable traceability checks for the complete Issue #194 ingestion gate."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TRACEABILITY = PROJECT_ROOT / "docs/qualification/issue-194-document-ingestion.md"
REPORT = PROJECT_ROOT / "docs/qualification/issue-194-document-ingestion-report.json"


@dataclass(frozen=True, slots=True)
class TraceabilityRow:
    acceptance_criterion: int
    evidence: tuple[str, ...]


TRACEABILITY_MATRIX = (
    TraceabilityRow(
        1,
        (
            "tests/application/test_document_ingestion.py::test_document_ingestion_completes_each_r1_format_with_parser_metadata",
            "tests/ingestion/test_pdf_parser.py::test_pdf_locator_round_trip_reopens_the_exact_extracted_page_range",
            "tests/ingestion/test_docx_parser.py::test_docx_nested_tables_and_duplicate_text_have_exact_structural_paths",
            "tests/domain/artifacts/test_xlsx_parser.py::test_xlsx_parser_preserves_physical_coordinates_and_resolves_exact_cells",
        ),
    ),
    TraceabilityRow(
        2,
        (
            "tests/application/test_batch_ingestion.py::test_batch_keeps_one_run_and_exposes_independent_partial_outcomes",
            "tests/api/test_batch_ingestion.py::test_batch_upload_streams_files_and_exposes_independent_outcomes",
            "tests/api/test_batch_ingestion.py::test_batch_polling_returns_all_items",
        ),
    ),
    TraceabilityRow(
        3,
        (
            "tests/application/test_document_ingestion.py::test_document_ingestion_completes_text_path_and_replays_stably",
            "tests/application/test_document_ingestion.py::test_projection_request_is_not_visible_when_its_transaction_fails",
            "tests/application/test_document_ingestion.py::test_stale_revision_is_rejected_before_parser_and_projection_request",
            "tests/application/outbox/test_dispatch.py::test_projection_request_redelivery_has_one_logical_consumer_effect",
            "tests/application/outbox/test_dispatch.py::test_concurrent_projection_request_dispatch_has_one_logical_consumer_effect",
            "tests/contracts/persistence/test_idempotency_repository_contract.py::test_independent_workspaces_environments_and_keys_do_not_collide",
            "tests/integration/object_storage/test_filesystem.py::test_concurrent_finalizers_publish_one_complete_immutable_object",
        ),
    ),
    TraceabilityRow(
        4,
        (
            "tests/application/test_source_lifecycle.py::test_deletion_records_observation_and_delegates_canonical_transition_atomically",
            "tests/application/test_source_lifecycle.py::test_reappearance_requires_existing_tombstone_and_fresh_canonical_evidence",
            "tests/application/test_source_lifecycle.py::test_repeating_same_lifecycle_command_returns_delegated_replay_without_new_effect",
            "tests/application/persistence/test_canonical_acceptance.py::test_canonical_acceptance_advances_head_and_replays_without_second_effect",
            "tests/contracts/persistence/test_canonical_acceptance_contract.py::test_canonical_rejects_stale_expected_current_without_success_effect",
        ),
    ),
    TraceabilityRow(
        5,
        (
            "tests/application/test_document_ingestion.py::test_projection_request_payload_rejects_content_bearing_fields",
            "tests/application/test_document_ingestion.py::test_projection_request_is_not_visible_when_its_transaction_fails",
            "tests/application/outbox/test_dispatch.py::test_projection_request_redelivery_has_one_logical_consumer_effect",
            "tests/application/outbox/test_dispatch.py::test_concurrent_projection_request_dispatch_has_one_logical_consumer_effect",
            "tests/api/test_batch_ingestion.py::test_batch_polling_returns_all_items",
        ),
    ),
    TraceabilityRow(
        6,
        (
            "tests/api/test_batch_ingestion.py::test_cross_workspace_upload_is_denied_before_parsing_or_service",
            "tests/api/test_batch_ingestion.py::test_cross_workspace_status_is_denied_before_service",
            "tests/api/test_batch_ingestion.py::test_valid_second_workspace_cannot_observe_first_workspace_run",
            "tests/application/test_content_authorization.py::test_revoked_current_policy_is_denied_on_next_decision_without_cache",
            "tests/application/test_content_authorization.py::test_changed_authority_generation_requires_reauthorization",
            "tests/integration/object_storage/test_filesystem.py::test_corrupt_and_missing_finalized_bytes_fail_closed",
            "tests/api/test_diagnostics.py::test_unexpected_api_failure_is_generic_and_keeps_request_trace",
        ),
    ),
    TraceabilityRow(
        7,
        (
            "tests/api/test_diagnostics.py::test_legacy_ingest_body_remains_compatible_with_trace_header",
            "tests/domain/artifacts/test_xlsx_parser.py::test_parse_document_selects_xlsx_adapter_without_using_legacy_loader",
        ),
    ),
    TraceabilityRow(
        8,
        (
            "tests/qualification/test_issue194_qualification.py::test_issue194_postgresql_evidence_is_mandatory_and_real",
            "tests/integration/postgresql/test_postgresql_outbox_delivery.py::test_postgresql_concurrent_workers_do_not_overlap_claims",
            "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_adapter_satisfies_ingestion_repository_contract",
            "tests/integration/object_storage/test_filesystem.py::test_interrupted_upload_is_resumable_after_adapter_restart",
            "tests/api/test_batch_ingestion.py::test_batch_openapi_declares_bounded_multipart_and_polling_contract",
            "tests/test_architecture_scaffold.py::ArchitectureScaffoldTests::test_domain_layer_has_no_framework_imports",
        ),
    ),
    TraceabilityRow(
        9,
        (
            "tests/qualification/test_issue194_qualification.py::test_issue194_report_names_unexercised_external_integrations",
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
            if "test_issue194_qualification.py" not in evidence
        )
    )


def _offline_evidence_nodes() -> tuple[str, ...]:
    return tuple(
        node for node in _evidence_nodes() if "tests/integration/postgresql/" not in node
    )


def test_issue194_traceability_matrix_covers_exactly_all_acceptance_criteria() -> None:
    assert tuple(row.acceptance_criterion for row in TRACEABILITY_MATRIX) == tuple(
        range(1, 10)
    )
    assert all(row.evidence for row in TRACEABILITY_MATRIX)
    assert all(len(row.evidence) == len(set(row.evidence)) for row in TRACEABILITY_MATRIX)


def test_issue194_traceability_document_matches_the_executable_matrix() -> None:
    content = TRACEABILITY.read_text(encoding="utf-8")
    rows = {
        int(match.group(1)): tuple(
            re.findall(r"`(tests/[^`]+?::test_[^`]+)`", match.group(2))
        )
        for match in re.finditer(r"^\| AC(\d+) \| (.+?) \|$", content, re.MULTILINE)
    }

    assert tuple(rows) == tuple(range(1, 10))
    assert rows == {
        row.acceptance_criterion: row.evidence
        for row in TRACEABILITY_MATRIX
    }


def test_issue194_report_is_machine_readable_and_scope_bounded() -> None:
    report = _report()

    assert report["schema_version"] == "document-ingestion-qualification:v1"
    assert report["ticket"] == 194
    assert report["mode"] == "supported_local_production_profile"
    assert report["release_gate"] == {"status": "passed", "r1_release": "not_approved"}
    assert report["criteria"] == [
        {
            "id": f"AC{row.acceptance_criterion}",
            "status": "passed",
            "evidence": list(row.evidence),
        }
        for row in TRACEABILITY_MATRIX
    ]
    assert report["production_approval"] is False
    assert report["out_of_scope"] == [
        "ContextProjection and Cognee provider qualification",
        "Q&A and Knowledge UI",
        "complete R1 release approval",
    ]


def test_issue194_evidence_nodes_are_collectable() -> None:
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


def test_issue194_offline_evidence_suite_passes() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *_offline_evidence_nodes()],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert re.search(r"\d+ passed", output), output


def test_issue194_postgresql_evidence_is_mandatory_and_real() -> None:
    for relative_path in (
        "tests/integration/postgresql/test_postgresql_outbox_delivery.py",
        "tests/integration/postgresql/test_postgresql_unit_of_work.py",
    ):
        content = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        assert "pytest.mark.postgresql" in content
        assert "testcontainers" in content or "postgresql_provision" in content


def test_issue194_report_names_unexercised_external_integrations() -> None:
    report = _report()

    limitations = report["limitations"]
    assert isinstance(limitations, list)
    assert any("Cognee" in item for item in limitations)
    assert any("external" in item.lower() for item in limitations)
