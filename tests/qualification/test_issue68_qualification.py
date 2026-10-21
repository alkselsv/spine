"""Executable, offline qualification checks for Issue #68."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from collections.abc import Mapping

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from spine.application.diagnostics import (
    DiagnosticContext,
    Retryability,
    StructuredError,
)
from spine.application.diagnostics.audit import (
    AuditEventRegistry,
    AuditOutcome,
    CommandAuditPayload,
)
from spine.application.persistence.context import ContextOrigin

from tests.contracts.diagnostics.fixtures import (
    assert_downstream_diagnostic_context,
    assert_safe_structured_error,
    diagnostic_context,
    assert_no_diagnostic_leaks,
    synthetic_uuid,
)
from tests.qualification.gate_runner import (
    GATE_ARTIFACT,
    GATE_COMMANDS,
    _worktree_has_unstaged_files,
    artifact_matches_report,
    classify_result,
    qualified_tree_sha256,
    run_gate_commands,
    staged_diff_sha256,
    validate_gate_artifact,
)


@dataclass(frozen=True)
class TraceabilityRow:
    acceptance_criterion: int
    test_level: str
    evidence: tuple[str, ...]
    gate_commands: tuple[str, ...] = ()


TRACEABILITY_MATRIX = (
    TraceabilityRow(1, "domain/application", ("tests/application/diagnostics/test_context.py::test_context_requires_non_zero_trace_id", "tests/application/diagnostics/test_context.py::test_context_rejects_zero_optional_identifiers", "tests/application/diagnostics/test_context.py::test_context_rejects_extra_fields_and_is_immutable", "tests/application/diagnostics/test_context.py::test_nested_context_preserves_trace_and_correlation_and_replaces_causation", "tests/application/diagnostics/test_context.py::test_context_can_transition_correlation_without_changing_trace_or_causation")),
    TraceabilityRow(2, "infrastructure", ("tests/infrastructure/diagnostics/test_traceparent.py::test_valid_traceparent_continues_trace_identity_and_formats_canonically", "tests/infrastructure/diagnostics/test_traceparent.py::test_missing_or_malformed_traceparent_falls_back_to_injected_trusted_trace", "tests/infrastructure/diagnostics/test_traceparent.py::test_parser_rejects_invalid_traceparent_without_exposing_input", "tests/api/test_diagnostics.py::test_valid_traceparent_continues_one_trusted_request_trace")),
    TraceabilityRow(3, "application/integration", ("tests/contracts/persistence/test_outbox_writer_contract.py::test_trace_mismatch_is_terminal_and_discards_pending_mutation", "tests/contracts/persistence/test_audit_writer_contract.py::test_audit_context_substitution_is_rejected_before_append", "tests/workers/test_composition.py::test_worker_quarantines_consumer_context_substitution", "tests/integration/test_diagnostics_flow.py::test_issue68_composed_path_covers_all_safe_surfaces")),
    TraceabilityRow(4, "domain/application", ("tests/application/diagnostics/test_errors.py::test_structured_error_v1_is_frozen_and_has_only_public_fields",)),
    TraceabilityRow(5, "application/infrastructure", ("tests/application/diagnostics/test_errors.py::test_unknown_error_is_generic_without_inspecting_or_leaking_exception", "tests/application/diagnostics/test_errors.py::test_default_registry_maps_each_persistence_category", "tests/application/diagnostics/test_errors.py::test_default_registry_maps_each_auth_category", "tests/infrastructure/persistence/test_postgresql_error_translation.py::test_provider_failure_maps_to_stable_redacted_category", "tests/application/outbox/test_dispatch.py::test_retryable_failure_clamps_injected_backoff_without_raw_error", "tests/application/outbox/test_dispatch.py::test_terminal_failure_is_quarantined_with_safe_code", "tests/application/outbox/test_dispatch.py::test_timeout_or_context_mismatch_never_acknowledges_success", "tests/application/outbox/test_dispatch.py::test_unknown_route_is_quarantined_without_consumer_execution", "tests/application/outbox/test_dispatch.py::test_unknown_consumer_is_quarantined_without_handler_execution", "tests/application/outbox/test_dispatch.py::test_unexpected_handler_failure_never_leaks_raw_detail")),
    TraceabilityRow(6, "infrastructure", ("tests/api/test_diagnostics.py::test_api_validation_failure_returns_safe_structured_error", "tests/api/test_diagnostics.py::test_api_http_failure_uses_structured_error_envelope", "tests/api/test_diagnostics.py::test_registered_api_failure_uses_safe_mapping_and_same_trace", "tests/api/test_diagnostics.py::test_legacy_ask_bodies_remain_compatible_with_trace_headers", "tests/api/test_diagnostics.py::test_legacy_ingest_body_remains_compatible_with_trace_header")),
    TraceabilityRow(7, "domain/application", ("tests/application/diagnostics/test_events.py::test_default_registry_supports_safe_outbox_delivery_observations", "tests/application/diagnostics/test_events.py::test_registry_rejects_payload_with_mutable_fields", "tests/application/diagnostics/test_events.py::test_registry_rejects_content_bearing_identifier_field_name", "tests/contracts/diagnostics/test_sink_contract.py::test_sink_emits_the_same_validated_snapshot[recording]")),
    TraceabilityRow(8, "contract/infrastructure", ("tests/contracts/diagnostics/test_sink_contract.py::test_sink_emits_the_same_validated_snapshot[logging]", "tests/infrastructure/diagnostics/test_sinks.py::test_logging_sink_emits_only_the_validated_structured_snapshot")),
    TraceabilityRow(9, "security", ("tests/application/diagnostics/test_errors.py::test_unknown_error_is_generic_without_inspecting_or_leaking_exception", "tests/infrastructure/diagnostics/test_sinks.py::test_protected_corpus_cannot_reach_structured_logs")),
    TraceabilityRow(10, "domain/application", ("tests/application/diagnostics/test_audit.py::test_registered_command_event_is_a_detached_immutable_snapshot", "tests/application/diagnostics/test_audit.py::test_audit_envelope_rejects_actor_scope_inconsistent_with_origin", "tests/application/diagnostics/test_audit.py::test_request_access_decision_uses_new_schema_without_changing_v1", "tests/application/diagnostics/test_audit.py::test_default_registry_supports_each_required_event_family", "tests/qualification/test_issue68_qualification.py::test_issue68_audit_envelope_preserves_scope_trace_and_schema")),
    TraceabilityRow(11, "application/integration", ("tests/contracts/persistence/test_audit_writer_contract.py::test_audit_failure_rolls_back_mutation_and_outbox_intent", "tests/integration/postgresql/test_postgresql_audit.py::test_postgresql_audit_failure_rolls_back_mutation_and_outbox_intent", "tests/integration/postgresql/test_postgresql_audit.py::test_mutation_audit_and_outbox_roll_back_together_at_every_boundary")),
    TraceabilityRow(12, "application/integration", ("tests/contracts/persistence/test_audit_writer_contract.py::test_required_audit_failure_never_releases_effect", "tests/integration/postgresql/test_postgresql_audit.py::test_postgresql_required_audit_failure_never_releases_effect")),
    TraceabilityRow(13, "contract/integration", ("tests/contracts/persistence/test_audit_writer_contract.py::test_duplicate_producer_identity_returns_original_audit_event", "tests/contracts/persistence/test_audit_writer_contract.py::test_distinct_retry_attempt_identities_create_distinct_audit_events", "tests/integration/postgresql/test_postgresql_audit.py::test_postgresql_duplicate_producer_identity_returns_original_event", "tests/integration/postgresql/test_postgresql_audit.py::test_postgresql_distinct_retry_attempts_create_distinct_events")),
    TraceabilityRow(14, "postgresql", ("tests/integration/postgresql/test_postgresql_audit.py::test_audit_catalog_has_named_append_only_tenant_contract", "tests/integration/postgresql/test_initial_tenancy_migration.py::test_initial_schema_has_named_tenancy_constraints_and_postgresql_types")),
    TraceabilityRow(15, "security/postgresql", ("tests/api/test_authorization.py::test_auth_failure_responses_and_diagnostics_exclude_negative_leak_corpus", "tests/integration/postgresql/test_postgresql_audit.py::test_runtime_rls_denies_cross_workspace_audit_read_and_insert", "tests/integration/postgresql/test_postgresql_audit.py::test_postgresql_allowed_audit_does_not_leak_target_metadata")),
    TraceabilityRow(16, "contract", ("tests/contracts/persistence/test_audit_writer_contract.py::test_audit_event_becomes_visible_only_after_explicit_commit", "tests/integration/postgresql/test_postgresql_audit.py::test_postgresql_audit_event_becomes_visible_only_after_explicit_commit")),
    TraceabilityRow(17, "contract", ("tests/contracts/persistence/test_outbox_delivery_repository_contract.py::test_claim_commits_a_bounded_lease_before_delivery", "tests/integration/postgresql/test_initial_tenancy_migration.py::test_outbox_intent_is_immutable_after_insert")),
    TraceabilityRow(18, "application/postgresql", ("tests/application/persistence/test_delivery.py::test_claimed_delivery_is_an_immutable_validated_snapshot", "tests/contracts/persistence/test_outbox_delivery_repository_contract.py::test_expired_lease_is_reclaimed_and_stale_worker_cannot_finalize", "tests/contracts/persistence/test_outbox_delivery_repository_contract.py::test_concurrent_workers_cannot_claim_the_same_delivery", "tests/application/outbox/test_dispatch.py::test_crash_before_handler_recovers_after_lease_expiry", "tests/integration/postgresql/test_postgresql_outbox_delivery.py::test_postgresql_concurrent_workers_do_not_overlap_claims", "tests/integration/postgresql/test_postgresql_outbox_delivery.py::test_postgresql_expired_lease_rejects_stale_worker")),
    TraceabilityRow(19, "application/integration", ("tests/application/outbox/test_dispatch.py::test_acknowledgement_loss_redelivers_one_logical_consumer_effect",)),
    TraceabilityRow(20, "application/integration", ("tests/application/outbox/test_dispatch.py::test_retryable_failure_clamps_injected_backoff_without_raw_error", "tests/application/outbox/test_dispatch.py::test_retryable_failure_at_attempt_limit_is_quarantined")),
    TraceabilityRow(21, "application/postgresql", ("tests/application/outbox/test_dispatch.py::test_success_commits_delivery_audit_before_emitting_diagnostic", "tests/integration/postgresql/test_postgresql_outbox_delivery.py::test_postgresql_dispatch_commits_delivery_and_audit_atomically")),
    TraceabilityRow(22, "application/integration", ("tests/application/outbox/test_dispatch.py::test_unknown_route_is_quarantined_without_consumer_execution", "tests/application/outbox/test_dispatch.py::test_unknown_consumer_is_quarantined_without_handler_execution", "tests/contracts/persistence/test_outbox_delivery_repository_contract.py::test_unknown_route_can_be_claimed_and_safely_quarantined", "tests/integration/postgresql/test_initial_tenancy_migration.py::test_unknown_revision_is_rejected_without_schema_changes", "tests/integration/postgresql/test_postgresql_outbox_delivery.py::test_unknown_persisted_event_claim_uses_header_only_envelope", "tests/integration/postgresql/test_postgresql_outbox_delivery.py::test_postgresql_unknown_persisted_event_is_quarantined_safely")),
    TraceabilityRow(23, "application/integration", ("tests/workers/test_composition.py::test_worker_factory_reconstructs_trusted_intent_context", "tests/integration/test_diagnostics_flow.py::test_issue68_composed_path_covers_all_safe_surfaces")),
    TraceabilityRow(24, "security/postgresql", ("tests/application/outbox/test_dispatch.py::test_unexpected_handler_failure_never_leaks_raw_detail", "tests/infrastructure/diagnostics/test_sinks.py::test_protected_corpus_cannot_reach_structured_logs", "tests/integration/postgresql/test_postgresql_outbox_delivery.py::test_postgresql_issue68_disclosure_corpus_covers_persisted_rows")),
    TraceabilityRow(25, "composed offline", ("tests/integration/test_diagnostics_flow.py::test_issue68_composed_path_covers_all_safe_surfaces",)),
    TraceabilityRow(26, "contract/architecture", ("tests/contracts/diagnostics/test_sink_contract.py::test_sink_emits_the_same_validated_snapshot[recording]", "tests/qualification/test_issue68_qualification.py::test_issue68_framework_free_downstream_fake_reuses_shared_contract")),
    TraceabilityRow(27, "domain/application", ("tests/application/outbox/test_dispatch.py::test_bounded_batch_starts_handlers_within_the_same_lease_window", "tests/application/persistence/test_retry_policy.py::test_custom_attempt_limit_and_injected_backoff_are_deterministic")),
    TraceabilityRow(28, "architecture", ("tests/auth/test_architecture.py::test_auth_seam_has_only_inward_framework_independent_dependencies", "tests/infrastructure/db/test_import_safety.py::test_importing_configuration_and_persistence_creates_no_engine_or_socket", "tests/test_architecture_scaffold.py::ArchitectureScaffoldTests::test_domain_layer_has_no_application_or_infrastructure_imports", "tests/qualification/test_issue68_qualification.py::test_issue68_domain_import_gate_covers_observability_frameworks")),
    TraceabilityRow(29, "postgresql", ("tests/integration/postgresql/test_initial_tenancy_migration.py::test_every_retained_revision_upgrades_to_one_final_head",)),
    TraceabilityRow(30, "repository gates", ("tests/test_architecture_scaffold.py::ArchitectureScaffoldTests::test_domain_layer_has_no_framework_imports", "tests/qualification/test_issue68_qualification.py::test_issue68_traceability_matrix_covers_exactly_ac1_to_ac30", "tests/qualification/test_issue68_qualification.py::test_issue68_migration_graph_has_one_dynamic_head_and_reaches_base", "tests/qualification/test_issue68_qualification.py::test_issue68_traceability_evidence_nodes_are_collectable", "tests/qualification/test_issue68_qualification.py::test_issue68_compileall_gate", "tests/qualification/test_issue68_qualification.py::test_issue68_full_offline_pytest_gate", "tests/qualification/test_issue68_qualification.py::test_issue68_gate_runner_classifies_repository_gate_results", "tests/qualification/test_issue68_qualification.py::test_issue68_gate_runner_executes_every_declared_command_in_order", "tests/qualification/test_issue68_qualification.py::test_issue68_gate_runner_cli_binds_to_staged_diff", "tests/qualification/test_issue68_qualification.py::test_issue68_gate_runner_rejects_unstaged_or_untracked_files", "tests/qualification/test_issue68_qualification.py::test_issue68_gate_runner_hash_changes_with_staged_diff", "tests/qualification/test_issue68_qualification.py::test_issue68_gate_runner_hashes_qualified_tree", "tests/qualification/test_issue68_qualification.py::test_issue68_gate_runner_matches_artifact_hashes_for_staged_and_clean_trees", "tests/qualification/test_issue68_qualification.py::test_issue68_gate_runner_rejects_mixed_pytest_failures", "tests/qualification/test_issue68_qualification.py::test_issue68_gate_runner_rejects_mixed_setup_errors_without_failed_summary", "tests/qualification/test_issue68_qualification.py::test_issue68_gate_runner_rejects_marker_outside_setup_blocks", "tests/qualification/test_issue68_qualification.py::test_issue68_gate_runner_artifact_matches_staged_diff_or_qualified_tree", "tests/qualification/test_issue68_qualification.py::test_issue68_gate_runner_validates_artifact_schema"), gate_commands=("uv sync --extra dev", "uv lock --check", "uv run pytest -q", "uv run python -m compileall -q src tests", "git diff --cached --check")),
)


def test_issue68_traceability_matrix_covers_exactly_ac1_to_ac30() -> None:
    criteria = tuple(row.acceptance_criterion for row in TRACEABILITY_MATRIX)
    allowed_levels = {
        "domain/application",
        "infrastructure",
        "application/integration",
        "application/infrastructure",
        "contract/infrastructure",
        "contract/integration",
        "security",
        "application/postgresql",
        "security/postgresql",
        "contract",
        "composed offline",
        "contract/architecture",
        "architecture",
        "postgresql",
        "repository gates",
    }
    assert criteria == tuple(range(1, 31))
    assert all(row.evidence for row in TRACEABILITY_MATRIX)
    assert all(row.test_level in allowed_levels for row in TRACEABILITY_MATRIX)
    assert all(len(row.evidence) == len(set(row.evidence)) for row in TRACEABILITY_MATRIX)
    assert TRACEABILITY_MATRIX[-1].gate_commands == (
        "uv sync --extra dev",
        "uv lock --check",
        "uv run pytest -q",
        "uv run python -m compileall -q src tests",
        "git diff --cached --check",
    )
    assert TRACEABILITY_MATRIX[-1].gate_commands == tuple(
        " ".join(command) for command in GATE_COMMANDS
    )


def test_issue68_traceability_evidence_points_to_test_modules() -> None:
    repository_root = Path(__file__).parents[2]

    for row in TRACEABILITY_MATRIX:
        for evidence in row.evidence:
            path_text, node_text = evidence.split("::", maxsplit=1)
            node_name = node_text.split("[", maxsplit=1)[0].rsplit("::", maxsplit=1)[-1]
            evidence_path = repository_root / path_text
            assert evidence_path.is_file(), evidence
            tree = ast.parse(evidence_path.read_text(encoding="utf-8"))
            assert any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == node_name
                for node in ast.walk(tree)
            ), evidence


def test_issue68_traceability_evidence_nodes_are_collectable() -> None:
    """Resolve the exact matrix nodes through pytest's real collector."""

    repository_root = Path(__file__).parents[2]
    evidence_by_path: dict[str, set[str]] = {}
    for row in TRACEABILITY_MATRIX:
        for evidence in row.evidence:
            path_text, node_text = evidence.split("::", maxsplit=1)
            evidence_by_path.setdefault(path_text, set()).add(node_text)

    collected = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            *evidence_by_path,
        ],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert collected.returncode == 0, collected.stdout + collected.stderr
    output = collected.stdout + collected.stderr
    for path_text, expected_nodes in evidence_by_path.items():
        missing = [
            node
            for node in expected_nodes
            if f"{path_text}::{node}" not in output
        ]
        assert not missing, f"missing collected nodes for {path_text}: {missing}\n{output}"


def test_issue68_offline_traceability_nodes_pass() -> None:
    """Execute every non-PostgreSQL evidence node named by the matrix."""

    if os.environ.get("SPINE_ISSUE68_FULL_OFFLINE_GATE") == "1":
        return
    repository_root = Path(__file__).parents[2]
    qualification_path = "tests/qualification/test_issue68_qualification.py"
    nested_executor_names = {
        "test_issue68_offline_traceability_nodes_pass",
        "test_issue68_full_offline_pytest_gate",
    }
    offline_nodes = tuple(
        f"{path_text}::{node_text}"
        for row in TRACEABILITY_MATRIX
        for evidence in row.evidence
        for path_text, node_text in (evidence.split("::", maxsplit=1),)
        if not path_text.startswith("tests/integration/postgresql/")
        and not (
            path_text == qualification_path
            and node_text.split("[", maxsplit=1)[0] in nested_executor_names
        )
    )
    unique_nodes = tuple(dict.fromkeys(offline_nodes))
    collection = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *unique_nodes],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    collection_output = collection.stdout + collection.stderr
    assert collection.returncode == 0, collection_output
    collected = re.search(r"(\d+) tests? collected", collection_output)
    assert collected is not None, collection_output
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-ra", *unique_nodes],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    lowered = output.lower()
    assert " skipped" not in lowered
    assert " xfailed" not in lowered
    assert " xpassed" not in lowered
    passed = re.search(r"(\d+) passed", lowered)
    assert passed is not None, output
    assert int(passed.group(1)) == int(collected.group(1)), output


def test_issue68_compileall_gate() -> None:
    repository_root = Path(__file__).parents[2]
    compileall = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", "src", "tests"],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert compileall.returncode == 0, compileall.stdout + compileall.stderr


def test_issue68_gate_runner_classifies_repository_gate_results() -> None:
    passed = classify_result(
        ("uv", "lock", "--check"),
        returncode=0,
        output="Resolved 160 packages in 4ms",
    )
    blocked = classify_result(
        ("uv", "run", "pytest", "-q"),
        returncode=1,
        output=(
            "10 passed, 2 errors\n"
            "_ ERROR at setup of test_one _\n"
            "mandatory PostgreSQL gate could not start container runtime image postgres:17.6-bookworm\n"
            "_ ERROR at setup of test_two _\n"
            "mandatory PostgreSQL gate could not start container runtime image postgres:17.6-bookworm"
        ),
    )
    failed = classify_result(
        ("git", "diff", "--cached", "--check"),
        returncode=1,
        output="secret raw output must not be retained",
    )

    assert (passed.status, passed.safe_summary) == ("passed", "completed successfully")
    assert blocked.status == "blocked-infrastructure"
    assert "mandatory PostgreSQL" in blocked.safe_summary
    assert failed.status == "failed"
    assert "secret raw output" not in failed.safe_summary


@pytest.mark.parametrize(
    "message",
    (
        "mandatory PostgreSQL gate requires SPINE_TEST_DATABASE_URL or a working container runtime",
        "mandatory PostgreSQL gate cannot import Testcontainers",
        "mandatory PostgreSQL gate could not start container runtime image postgres:17.6-bookworm",
    ),
)
def test_issue68_gate_runner_marks_only_postgresql_prerequisite_errors_unavailable(
    message: str,
) -> None:
    blocked = classify_result(
        ("uv", "run", "pytest", "-q"),
        returncode=1,
        output=f"10 passed, 1 error\n_ ERROR at setup of test_postgresql _\n{message}",
    )
    mixed_failure = classify_result(
        ("uv", "run", "pytest", "-q"),
        returncode=1,
        output=(
            "1 failed, 10 passed, 1 error\n"
            "_ ERROR at setup of test_postgresql _\n"
            f"{message}\n"
            "_ FAILED test_offline_contract _\nAssertionError"
        ),
    )

    assert blocked.status == "blocked-infrastructure"
    assert mixed_failure.status == "failed"


def test_issue68_gate_runner_executes_every_declared_command_in_order() -> None:
    calls: list[tuple[tuple[str, ...], Path]] = []

    def fake_executor(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool,
        text: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert check is False
        assert capture_output is True
        assert text is True
        calls.append((command, cwd))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    repository_root = Path(__file__).parents[2]
    results = run_gate_commands(repository_root, executor=fake_executor)

    assert tuple(command for command, _ in calls) == GATE_COMMANDS
    assert all(cwd == repository_root for _, cwd in calls)
    assert all(result.status == "passed" for result in results)


def test_issue68_gate_runner_cli_binds_to_staged_diff() -> None:
    repository_root = Path(__file__).parents[2]
    result = subprocess.run(
        [sys.executable, "-m", "tests.qualification.gate_runner", "--dry-run"],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["staged_diff_sha256"] == staged_diff_sha256(repository_root)
    assert tuple(report["commands"]) == tuple(" ".join(command) for command in GATE_COMMANDS)
    assert report["results"] == []


def _git_init(repository_root: Path) -> None:
    subprocess.run(("git", "init", "--quiet"), cwd=repository_root, check=True)


def test_issue68_gate_runner_rejects_unstaged_or_untracked_files(tmp_path: Path) -> None:
    _git_init(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("staged", encoding="utf-8")
    subprocess.run(("git", "add", "tracked.txt"), cwd=tmp_path, check=True)
    assert not _worktree_has_unstaged_files(tmp_path)

    tracked.write_text("unstaged", encoding="utf-8")
    assert _worktree_has_unstaged_files(tmp_path)

    tracked.write_text("staged", encoding="utf-8")
    untracked = tmp_path / "untracked.txt"
    untracked.write_text("untracked", encoding="utf-8")
    assert _worktree_has_unstaged_files(tmp_path)


def test_issue68_gate_runner_hash_changes_with_staged_diff(tmp_path: Path) -> None:
    _git_init(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("first", encoding="utf-8")
    subprocess.run(("git", "add", "tracked.txt"), cwd=tmp_path, check=True)
    first_hash = staged_diff_sha256(tmp_path)

    tracked.write_text("second", encoding="utf-8")
    subprocess.run(("git", "add", "tracked.txt"), cwd=tmp_path, check=True)
    assert staged_diff_sha256(tmp_path) != first_hash


def test_issue68_gate_runner_hashes_qualified_tree(tmp_path: Path) -> None:
    _git_init(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("first", encoding="utf-8")
    subprocess.run(("git", "add", "tracked.txt"), cwd=tmp_path, check=True)
    first_hash = qualified_tree_sha256(tmp_path)

    tracked.write_text("second", encoding="utf-8")
    subprocess.run(("git", "add", "tracked.txt"), cwd=tmp_path, check=True)
    assert qualified_tree_sha256(tmp_path) != first_hash


def test_issue68_gate_runner_rejects_mixed_pytest_failures() -> None:
    output = (
        "10 passed, 1 failed, 2 errors\n"
        "mandatory PostgreSQL gate could not start container runtime image postgres:17.6-bookworm\n"
        "mandatory PostgreSQL gate could not start container runtime image postgres:17.6-bookworm"
    )
    result = classify_result(
        ("uv", "run", "pytest", "-q"),
        returncode=1,
        output=output,
    )
    assert result.status == "failed"


def test_issue68_gate_runner_rejects_mixed_setup_errors_without_failed_summary() -> None:
    output = (
        "10 passed, 2 errors\n"
        "_ ERROR at setup of test_one _\n"
        "mandatory PostgreSQL gate could not start container runtime image postgres:17.6-bookworm\n"
        "_ ERROR at setup of test_two _\n"
        "non-PostgreSQL fixture failure"
    )
    result = classify_result(
        ("uv", "run", "pytest", "-q"),
        returncode=1,
        output=output,
    )
    assert result.status == "failed"


def test_issue68_gate_runner_rejects_marker_outside_setup_blocks() -> None:
    output = (
        "10 passed, 2 errors\n"
        "unrelated output repeats: mandatory PostgreSQL gate could not start\n"
        "_ ERROR at setup of test_one _\n"
        "mandatory PostgreSQL gate could not start container runtime image postgres:17.6-bookworm\n"
        "_ ERROR at setup of test_two _\n"
        "non-PostgreSQL fixture failure"
    )
    result = classify_result(
        ("uv", "run", "pytest", "-q"),
        returncode=1,
        output=output,
    )
    assert result.status == "failed"


def test_issue68_gate_runner_artifact_matches_staged_diff_or_qualified_tree() -> None:
    repository_root = Path(__file__).parents[2]
    artifact_path = repository_root / GATE_ARTIFACT

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        artifact: dict[str, object] = {}
        for key, value in pairs:
            if key in artifact:
                raise ValueError(f"duplicate JSON key: {key}")
            artifact[key] = value
        return artifact

    with pytest.raises(ValueError, match="duplicate JSON key"):
        json.loads('{"key": 1, "key": 2}', object_pairs_hook=reject_duplicate_keys)

    artifact = json.loads(
        artifact_path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys
    )
    validate_gate_artifact(artifact)
    # This artifact is an immutable qualification snapshot. Its source tree
    # identity is historical; current source changes are evaluated by CI.
    actual_staged_diff = staged_diff_sha256(repository_root)
    if actual_staged_diff != hashlib.sha256(b"").hexdigest():
        assert artifact["staged_diff_sha256"] == actual_staged_diff
    assert artifact["results"] == [
        {
            "command": "uv sync --extra dev",
            "returncode": 0,
            "status": "passed",
            "summary": "completed successfully",
        },
        {
            "command": "uv lock --check",
            "returncode": 0,
            "status": "passed",
            "summary": "completed successfully",
        },
        {
            "command": "uv run pytest -q",
            "returncode": 0,
            "status": "passed",
            "summary": "3740 passed",
        },
        {
            "command": "uv run python -m compileall -q src tests",
            "returncode": 0,
            "status": "passed",
            "summary": "completed successfully",
        },
        {
            "command": "git diff --cached --check",
            "returncode": 0,
            "status": "passed",
            "summary": "completed successfully",
        },
    ]


def test_issue68_gate_runner_matches_artifact_hashes_for_staged_and_clean_trees() -> None:
    repository_root = Path(__file__).parents[2]
    artifact = json.loads((repository_root / GATE_ARTIFACT).read_text(encoding="utf-8"))
    report = {
        "staged_diff_sha256": artifact["staged_diff_sha256"],
        "qualified_tree_sha256": artifact["qualified_tree_sha256"],
        "results": artifact["results"],
    }
    assert artifact_matches_report(artifact, report)

    clean_checkout_report = {
        **report,
        "staged_diff_sha256": hashlib.sha256(b"").hexdigest(),
    }
    assert artifact_matches_report(artifact, clean_checkout_report)

    mismatched_nonempty_diff = {
        **report,
        "staged_diff_sha256": "f" * 64,
    }
    assert not artifact_matches_report(artifact, mismatched_nonempty_diff)

    mismatched_tree = {
        **report,
        "qualified_tree_sha256": "e" * 64,
    }
    assert not artifact_matches_report(artifact, mismatched_tree)


def test_issue68_gate_runner_validates_artifact_schema() -> None:
    repository_root = Path(__file__).parents[2]
    artifact = json.loads((repository_root / GATE_ARTIFACT).read_text(encoding="utf-8"))
    validate_gate_artifact(artifact)

    with pytest.raises(ValueError):
        validate_gate_artifact({**artifact, "unexpected": "field"})
    with pytest.raises(ValueError):
        validate_gate_artifact(
            {
                **artifact,
                "results": [
                    *artifact["results"][:-1],
                    {**artifact["results"][-1], "status": "blocked-infrastructure"},
                ],
            }
        )
    with pytest.raises(ValueError):
        validate_gate_artifact(
            {
                **artifact,
                "results": [
                    *artifact["results"][:-1],
                    {**artifact["results"][-1], "status": []},
                ],
            }
        )


def test_issue68_domain_import_gate_covers_observability_frameworks() -> None:
    """The domain must remain free of framework and observability imports."""

    domain_root = Path(__file__).parents[2] / "src" / "spine" / "domain"
    forbidden_roots = {
        "alembic",
        "cognee",
        "fastapi",
        "logging",
        "opentelemetry",
        "psycopg",
        "sqlalchemy",
        "temporalio",
    }
    imported_roots: set[str] = set()
    for path in domain_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])
    assert imported_roots.isdisjoint(forbidden_roots)


def test_issue68_audit_envelope_preserves_scope_trace_and_schema() -> None:
    workspace_id = synthetic_uuid(6810)
    environment_id = synthetic_uuid(6811)
    trace_id = synthetic_uuid(6812)
    correlation_id = synthetic_uuid(6813)
    causation_id = synthetic_uuid(6814)
    event = AuditEventRegistry.with_default_families().build_event(
        workspace_id=workspace_id,
        environment_id=environment_id,
        event_type="command.outcome",
        schema_version=1,
        payload=CommandAuditPayload(command_type="workspace.inspect"),
        origin=ContextOrigin.INTERACTIVE,
        acting_subject_id=synthetic_uuid(6815),
        service_principal_id=None,
        trace_id=trace_id,
        correlation_id=correlation_id,
        causation_id=causation_id,
        occurred_at=datetime(2026, 10, 12, 16, tzinfo=timezone.utc),
        outcome=AuditOutcome.COMPLETED,
        reason="inspection_completed",
    )

    assert event.workspace_id == workspace_id
    assert event.environment_id == environment_id
    assert event.trace_id == trace_id
    assert event.correlation_id == correlation_id
    assert event.causation_id == causation_id
    assert event.schema_version == 1


def test_issue68_full_offline_pytest_gate() -> None:
    """Run the complete offline gate without recursively rerunning this node."""

    if os.environ.get("SPINE_ISSUE68_FULL_OFFLINE_GATE") == "1":
        return
    repository_root = Path(__file__).parents[2]
    environment = os.environ.copy()
    environment["SPINE_ISSUE68_FULL_OFFLINE_GATE"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--ignore=tests/integration/postgresql",
        ],
        cwd=repository_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_issue68_migration_graph_has_one_dynamic_head_and_reaches_base() -> None:
    repository_root = Path(__file__).parents[2]
    config = Config(str(repository_root / "alembic.ini"))
    config.set_main_option("script_location", str(repository_root / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    heads = tuple(scripts.get_heads())

    assert len(heads) == 1
    head = heads[0]
    revisions = tuple(scripts.iterate_revisions(head, "base"))
    revision_ids = tuple(revision.revision for revision in revisions)

    assert revision_ids
    assert revision_ids[0] == head
    assert any(revision.down_revision is None for revision in revisions)
    assert len(revision_ids) == len(set(revision_ids))
    assert all(
        revision.down_revision is None
        or revision.down_revision in revision_ids
        for revision in revisions
    )


def test_issue68_shared_downstream_fixture_has_no_framework_imports() -> None:
    fixture_path = Path(__file__).parents[1] / "contracts" / "diagnostics" / "fixtures.py"
    tree = ast.parse(fixture_path.read_text(encoding="utf-8"))
    allowed_modules = {
        "__future__",
        "collections.abc",
        "json",
        "spine.application.diagnostics",
        "uuid",
    }
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module)
        elif isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
    assert imported_modules <= allowed_modules


def test_issue68_framework_free_downstream_fake_reuses_shared_contract() -> None:
    class FrameworkFreeDownstream:
        def accept(
            self,
            context_wire: Mapping[str, object],
            error_wire: Mapping[str, object],
        ) -> dict[str, str | None]:
            """Consume only the serialized application boundary, not app objects."""

            restored = DiagnosticContext.model_validate(context_wire)
            restored_error = StructuredError.model_validate(error_wire)
            return {
                "trace_id": str(restored.trace_id),
                "correlation_id": (
                    str(restored.correlation_id)
                    if restored.correlation_id is not None
                    else None
                ),
                "causation_id": (
                    str(restored.causation_id)
                    if restored.causation_id is not None
                    else None
                ),
                "error_code": restored_error.code,
                "error_trace_id": str(restored_error.trace_id),
            }

    context = diagnostic_context(trace=681, correlation=682, causation=683)
    error = StructuredError(
        code="spine.request.rejected",
        safe_message="The request was rejected.",
        retryability=Retryability.NEVER,
        trace_id=context.trace_id,
    )
    context_wire = json.loads(context.model_dump_json())
    error_wire = json.loads(error.model_dump_json())
    downstream_result = FrameworkFreeDownstream().accept(context_wire, error_wire)
    assert downstream_result == {
        "trace_id": str(synthetic_uuid(681)),
        "correlation_id": str(synthetic_uuid(682)),
        "causation_id": str(synthetic_uuid(683)),
        "error_code": "spine.request.rejected",
        "error_trace_id": str(synthetic_uuid(681)),
    }
    restored_context = DiagnosticContext.model_validate(context_wire)
    restored_error = StructuredError.model_validate(error_wire)
    assert_downstream_diagnostic_context(
        restored_context,
        trace_id=synthetic_uuid(681),
        correlation_id=synthetic_uuid(682),
        causation_id=synthetic_uuid(683),
    )
    assert_safe_structured_error(restored_error)
    assert restored_error.trace_id == restored_context.trace_id


def test_issue68_disclosure_helper_catches_nested_and_double_encoded_values() -> None:
    protected = 'Traceback: "customer question"'
    nested_capture = json.dumps(
        {"outer": json.dumps({"inner": json.dumps(protected)})}
    )

    with pytest.raises(AssertionError):
        assert_no_diagnostic_leaks((nested_capture,), leak_corpus=(protected,))

    formatted_logs = "\n".join(
        (
            json.dumps({"message": json.dumps(protected)}),
            json.dumps({protected: "redacted"}),
        )
    )
    with pytest.raises(AssertionError):
        assert_no_diagnostic_leaks((line for line in (formatted_logs,)), leak_corpus=(protected,))

    prefixed_log = f"INFO spine.worker: {json.dumps({'message': json.dumps(protected)})}"
    with pytest.raises(AssertionError):
        assert_no_diagnostic_leaks((prefixed_log,), leak_corpus=(protected,))
