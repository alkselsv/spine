"""Executable qualification matrix for the complete answer_question capability."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TRACEABILITY = PROJECT_ROOT / "docs/qualification/issue-285-answer-question.md"
REPORT = PROJECT_ROOT / "docs/qualification/issue-285-answer-question-report.json"


OFFLINE_TARGETS = (
    "tests/agents/test_answer_question_candidate_runtime.py",
    "tests/agents/test_local_runtime.py",
    "tests/application/diagnostics/test_errors.py",
    "tests/application/test_answer_disclosure.py",
    "tests/application/test_answer_outcomes.py",
    "tests/application/test_answer_question_candidate.py",
    "tests/application/test_answer_question_context.py",
    "tests/application/test_answer_question_execution.py",
    "tests/application/test_authorized_context_broker.py",
    "tests/application/test_citation_validation.py",
    "tests/application/test_disclosure_revalidation.py",
    "tests/application/test_attempt_revalidation.py",
    "tests/application/test_semantic_support.py",
    "tests/application/test_evidence_lineage_verification.py",
    "tests/contracts/memory/test_context_broker_v1.py",
    "tests/contracts/persistence/test_answer_question_run_repository_contract.py",
    "tests/domain/capabilities/test_answer_disclosure.py",
    "tests/domain/capabilities/test_answer_evaluation.py",
    "tests/domain/capabilities/test_answer_outcomes.py",
    "tests/domain/capabilities/test_answer_question_candidate.py",
    "tests/domain/capabilities/test_answer_question_contracts.py",
    "tests/domain/capabilities/test_answer_question_runs.py",
    "tests/domain/disclosure/test_disclosure_models.py",
    "tests/evals/test_answer_question_contract_evaluation.py",
    "tests/evals/test_qa_r1_dataset.py",
    "tests/evals/test_retrieval_replay.py",
    "tests/evals/test_regression_gate.py",
    "tests/infrastructure/persistence/test_answer_disclosure_schema.py",
    "tests/infrastructure/persistence/test_answer_question_run_schema.py",
    "tests/api/test_qa_sessions.py",
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
        "offline/provider-fake",
        (
            "tests/domain/capabilities/test_answer_question_contracts.py::test_fake_handler_covers_every_terminal_outcome_without_a_provider",
            "tests/application/test_answer_question_context.py::test_allowed_context_consumes_exact_bundle_and_pin_without_caller_selectors",
            "tests/application/test_answer_question_candidate.py::test_candidate_generation_delegates_to_pinned_runtime_as_repeatable_call",
            "tests/application/test_answer_question_candidate.py::test_candidate_generation_returns_a_persisted_result_not_a_token_stream",
            "tests/agents/test_answer_question_candidate_runtime.py::test_local_runtime_persists_a_valid_typed_candidate_once",
            "tests/evals/test_answer_question_contract_evaluation.py::test_answer_question_contract_evaluation_fixture_covers_closed_outcome_sets",
        ),
    ),
    TraceabilityRow(
        2,
        "postgresql/persistence",
        (
            "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_answer_question_run_create_and_replay_contract",
            "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_answer_question_run_conflicting_key_contract",
            "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_answer_question_attempt_immutability_contract",
            "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_answer_question_context_bundle_reference_contract",
            "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_answer_question_concurrent_completion_contract",
            "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_answer_question_run_isolation_contract",
            "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_answer_question_retry_cancellation_contract",
            "tests/integration/postgresql/test_initial_tenancy_migration.py::test_initial_schema_has_named_tenancy_constraints_and_postgresql_types",
            "tests/integration/postgresql/test_initial_tenancy_migration.py::test_workspace_rls_hides_and_rejects_another_workspace_rows",
            "tests/integration/postgresql/test_initial_tenancy_migration.py::test_environment_rls_isolates_two_environments_in_one_workspace",
        ),
    ),
    TraceabilityRow(
        3,
        "evidence/validation",
        (
            "tests/application/test_citation_validation.py::test_fabricated_or_wrong_generation_citation_fails_closed_without_source_details",
            "tests/application/test_evidence_lineage_verification.py::test_fabricated_evidence_digest_is_rejected",
            "tests/application/test_evidence_lineage_verification.py::test_mixed_access_multi_source_entity_is_withheld_as_a_whole",
            "tests/application/test_evidence_lineage_verification.py::test_all_authorized_multi_source_entity_is_verified_without_dropping_contributors",
            "tests/evals/test_retrieval_replay.py::test_stale_projection_abstains_until_rebuild_selects_current_revision",
            "tests/evals/test_retrieval_replay.py::test_unsafe_fixture_case_returns_no_protected_evidence[tombstone]",
            "tests/evals/test_retrieval_replay.py::test_cross_workspace_query_returns_empty_without_leaking_other_workspace",
            "tests/application/test_semantic_support.py::test_citation_presence_does_not_substitute_for_semantic_support",
            "tests/application/test_semantic_support.py::test_validator_failure_is_fail_closed",
            "tests/domain/capabilities/test_answer_evaluation.py::test_proven_exhaustive_coverage_produces_complete_answer",
            "tests/domain/capabilities/test_answer_evaluation.py::test_incomplete_coverage_with_current_fallback_produces_partial_answer",
            "tests/domain/capabilities/test_answer_evaluation.py::test_current_grounded_conflict_is_presented_without_selecting_a_winner",
            "tests/domain/capabilities/test_answer_evaluation.py::test_explicit_coverage_gap_without_fallback_rejects_answer",
        ),
    ),
    TraceabilityRow(
        4,
        "disclosure/lifecycle",
        (
            "tests/application/test_disclosure_revalidation.py::test_current_ineligible_dependency_suppresses_whole_answer",
            "tests/application/test_disclosure_revalidation.py::test_revision_replacement_requires_revalidation_without_disclosing_old_answer",
            "tests/application/test_disclosure_revalidation.py::test_material_later_publication_requires_revalidation",
            "tests/application/test_disclosure_revalidation.py::test_authority_generation_change_requires_revalidation",
            "tests/application/test_disclosure_revalidation.py::test_current_requester_revocation_suppresses_history_without_content_reason",
            "tests/application/test_disclosure_revalidation.py::test_superseded_dependency_suppresses_whole_answer",
            "tests/application/test_disclosure_revalidation.py::test_cancelled_revalidation_fails_closed_without_stored_answer",
            "tests/application/test_disclosure_revalidation.py::test_interrupted_revalidation_fails_closed_without_stored_answer",
            "tests/application/test_disclosure_revalidation.py::test_missing_dependency_fails_closed_and_does_not_reuse_stored_answer",
            "tests/application/test_attempt_revalidation.py::test_activation_change_alone_replays_the_pinned_bundle",
            "tests/application/test_attempt_revalidation.py::test_tombstoned_source_fails_closed_even_when_the_old_evidence_is_present",
            "tests/application/test_attempt_revalidation.py::test_retryable_observation_is_not_cached_and_later_retry_can_replay",
            "tests/application/test_answer_disclosure.py::test_gate_suppresses_replay_after_current_authority_revocation",
            "tests/application/test_answer_question_context.py::test_revalidation_failure_blocks_pre_disclosure_and_never_returns_answered",
        ),
    ),
    TraceabilityRow(
        5,
        "safety/cancellation/diagnostics",
        (
            "tests/agents/test_answer_question_candidate_runtime.py::test_local_runtime_rejects_candidate_before_output_checkpoint",
            "tests/agents/test_answer_question_candidate_runtime.py::test_local_runtime_rejects_untyped_or_source_free_candidate_output",
            "tests/domain/capabilities/test_answer_disclosure.py::test_cancellation_never_reaches_accepted_output",
            "tests/application/test_answer_outcomes.py::test_application_resolver_maps_typed_validator_fake_without_diagnostic_detail",
            "tests/application/diagnostics/test_errors.py::test_unknown_error_is_generic_without_inspecting_or_leaking_exception",
            "tests/contracts/memory/test_context_broker_v1.py::test_port_errors_map_to_safe_typed_responses[validator_error]",
            "tests/application/test_answer_question_context.py::test_denied_context_returns_safe_denial_without_loading_bundle",
            "tests/agents/test_local_runtime.py::test_failure_diagnostics_reject_negative_disclosure_corpus",
            "tests/application/test_semantic_support.py::test_citation_presence_does_not_substitute_for_semantic_support",
            "tests/application/test_semantic_support.py::test_validator_unavailable_is_a_mandatory_qna_failure",
            "tests/application/test_semantic_support.py::test_validator_failure_is_fail_closed",
        ),
    ),
    TraceabilityRow(
        6,
        "downstream-contract-compatibility",
        (
            "tests/domain/capabilities/test_answer_question_runs.py::test_answer_question_attempt_completion_pins_context_versions_usage_and_safe_events",
            "tests/application/test_answer_question_context.py::test_context_outcome_mapping_keeps_broker_and_answer_outcomes_distinct",
            "tests/evals/test_answer_question_contract_evaluation.py::test_answer_question_contract_evaluation_fixture_covers_closed_outcome_sets",
            "tests/api/test_qa_sessions.py::test_session_create_replay_and_message_read_use_stable_scoped_contracts",
            "tests/qualification/test_issue285_qualification.py::test_issue285_downstream_web_boundary_is_explicitly_compatibility_only",
        ),
    ),
    TraceabilityRow(
        7,
        "evaluation-inputs",
        (
            "tests/evals/test_qa_r1_dataset.py::test_r1_dataset_manifest_is_ordered_for_replay",
            "tests/evals/test_qa_r1_dataset.py::test_r1_dataset_is_synthetic_and_does_not_define_release_thresholds",
            "tests/evals/test_answer_question_contract_evaluation.py::test_answer_question_contract_evaluation_fixture_covers_closed_outcome_sets",
            "tests/evals/test_regression_gate.py::test_offline_regression_gate_passes_reproducible_r1_contract",
            "tests/qualification/test_issue285_qualification.py::test_issue285_regression_gate_input_contract_is_versioned",
        ),
    ),
    TraceabilityRow(
        8,
        "qualification/reporting",
        (
            "tests/qualification/test_issue285_qualification.py::test_issue285_report_is_machine_readable_and_separates_readiness_claims",
            "tests/qualification/test_issue285_qualification.py::test_issue285_report_names_unverified_external_prerequisites",
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


def _node_name(node_text: str) -> str:
    return node_text.split("[", maxsplit=1)[0].rsplit("::", maxsplit=1)[-1]


def test_issue285_traceability_matrix_covers_exactly_ac1_to_ac8() -> None:
    allowed_levels = {
        "offline/provider-fake",
        "postgresql/persistence",
        "evidence/validation",
        "disclosure/lifecycle",
        "safety/cancellation/diagnostics",
        "downstream-contract-compatibility",
        "evaluation-inputs",
        "qualification/reporting",
    }

    assert tuple(row.acceptance_criterion for row in TRACEABILITY_MATRIX) == tuple(
        range(1, 9)
    )
    assert all(row.evidence for row in TRACEABILITY_MATRIX)
    assert all(len(row.evidence) == len(set(row.evidence)) for row in TRACEABILITY_MATRIX)
    assert all(row.test_level in allowed_levels for row in TRACEABILITY_MATRIX)


def test_issue285_traceability_evidence_points_to_real_test_nodes() -> None:
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


def test_issue285_traceability_document_matches_executable_matrix() -> None:
    content = TRACEABILITY.read_text(encoding="utf-8")
    rows = {
        int(match.group(1)): tuple(
            re.findall(r"`(tests/[^`]+?::(?:test_|ArchitectureScaffoldTests::test_)[^`]+)`", match.group(2))
        )
        for match in re.finditer(r"^\| AC(\d+) \| (.+?) \|$", content, re.MULTILINE)
    }

    assert tuple(rows) == tuple(range(1, 9))
    assert rows == {
        row.acceptance_criterion: row.evidence for row in TRACEABILITY_MATRIX
    }


def test_issue285_traceability_evidence_nodes_are_collectable() -> None:
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


def test_issue285_offline_evidence_suite_passes() -> None:
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


def test_issue285_postgresql_evidence_is_an_explicit_external_gate() -> None:
    postgres_evidence = TRACEABILITY_MATRIX[1].evidence

    assert postgres_evidence
    assert all("tests/integration/postgresql/" in node for node in postgres_evidence)
    assert all(
        node.split("::", maxsplit=1)[0] not in OFFLINE_TARGETS
        for node in postgres_evidence
    )


def test_issue285_report_is_machine_readable_and_separates_readiness_claims() -> None:
    report = _report()

    assert report["schema_version"] == "answer-question-qualification:v1"
    assert report["ticket"] == 285
    assert report["parent_ticket"] == 25
    assert report["criteria"] == [
        {
            "id": f"AC{row.acceptance_criterion}",
            "status": "qualified_offline"
            if row.acceptance_criterion != 2
            else "qualified_postgresql",
            "evidence": list(row.evidence),
        }
        for row in TRACEABILITY_MATRIX
    ]
    assert report["release_gate"] == {
        "status": "qualified_offline",
        "postgresql": "passed_disposable_harness",
        "provider_qualification": "not_approved",
    }
    assert report["readiness"] == {
        "deterministic_offline": "qualified",
        "postgresql": "qualified_disposable_harness",
        "integration_prerequisites": "not_run",
        "native_dependency": "not_run",
        "production": "not_approved",
    }
    assert report["production_approval"] is False
    assert report["verification"]["offline"]["status"] == "passed"
    assert report["verification"]["compile_import"]["status"] == "passed"
    assert report["verification"]["postgresql"]["status"] == "passed"
    assert report["verification"]["web_compatibility"]["status"] == "ci_required"
    assert report["verification"]["opt_in_integrations"]["status"] == "skipped"
    assert report["verification"]["failures"] == []


def test_issue285_report_names_unverified_external_prerequisites() -> None:
    report = _report()
    prerequisites = report["external_prerequisites"]

    assert isinstance(prerequisites, list)
    assert "SPINE_TEST_DATABASE_URL" in report["postgresql_prerequisite"]["requirement"]
    assert any("Context Broker" in item for item in prerequisites)
    assert any("Cognee" in item for item in prerequisites)
    assert any("real provider" in item for item in prerequisites)
    assert report["boundaries"] == {
        "context_broker": "consumed_not_owned",
        "api_sse_issue_26": "compatibility_only",
        "chat_citation_inspector_issue_22": "compatibility_only",
        "regression_gate_issue_19": "inputs_only",
        "quality_thresholds_issue_17": "out_of_scope",
    }


def test_issue285_regression_gate_input_contract_is_versioned() -> None:
    from spine.evals.regression_gate import run_offline_regression_gate

    gate = run_offline_regression_gate()

    assert gate.passed is True
    assert gate.report is not None
    assert gate.report.qualification_scope == "offline-contract-only"
    assert gate.report.dataset is not None
    assert gate.report.dataset.dataset_id == "qa-r1-synthetic"
    assert gate.report.dataset.version == "1.0.0"
    assert gate.report.configuration.answer_configuration_version
    assert gate.report.configuration.citation_evaluator_version
    assert gate.report.evaluator_catalog


def test_issue285_downstream_web_boundary_is_explicitly_compatibility_only() -> None:
    transport = (PROJECT_ROOT / "web/src/features/chat/transport.ts").read_text(
        encoding="utf-8"
    )
    chat_readme = (PROJECT_ROOT / "web/src/features/chat/README.md").read_text(
        encoding="utf-8"
    )
    e2e = (PROJECT_ROOT / "web/e2e/app.spec.ts").read_text(encoding="utf-8")

    assert "future Issue #26 API and SSE endpoints" in transport
    assert "never treats streamed deltas as the saved message source" in chat_readme
    assert "test_chat_fixture_reconnects_and_rehydrates_canonical_message" in e2e
    assert "test_chat_fixture_cancels_and_retries_same_command_without_duplicate_message" in e2e
    protocol_tests = (PROJECT_ROOT / "web/src/features/chat/protocol.test.ts").read_text(
        encoding="utf-8"
    )
    transport_tests = (PROJECT_ROOT / "web/src/features/chat/transport.test.ts").read_text(
        encoding="utf-8"
    )
    assert "test_protocol_rejects_candidate_output_and_provider_details" in protocol_tests
    assert "test_transport_rechecks_disclosure_and_denies_before_protected_event" in transport_tests
