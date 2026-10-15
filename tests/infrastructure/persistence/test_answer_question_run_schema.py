from __future__ import annotations

import importlib

from spine.infrastructure.persistence.postgresql_mappings import (
    answer_question_attempts,
    answer_question_runs,
)


def test_answer_question_run_mapping_keeps_scope_idempotency_and_safe_payload() -> None:
    assert [column.name for column in answer_question_runs.primary_key.columns] == [
        "workspace_id",
        "environment_id",
        "run_id",
    ]
    assert {
        "idempotency_key",
        "command_digest",
        "state",
        "outcome",
        "payload",
        "created_at",
        "completed_at",
    } <= set(column.name for column in answer_question_runs.columns)


def test_answer_question_attempt_mapping_keeps_run_and_context_bundle_lineage() -> None:
    assert [column.name for column in answer_question_attempts.primary_key.columns] == [
        "workspace_id",
        "environment_id",
        "attempt_id",
    ]
    assert {
        "run_id",
        "attempt_number",
        "state",
        "outcome",
        "context_bundle_id",
        "context_bundle_version",
        "context_bundle_digest",
        "payload",
        "started_at",
        "completed_at",
    } <= set(column.name for column in answer_question_attempts.columns)


def test_answer_question_run_migration_follows_context_bundle_head() -> None:
    migration = importlib.import_module(
        "migrations.versions.20261026_30_answer_question_runs"
    )

    assert migration.revision == "20261026_30"
    assert migration.down_revision == "20261025_29"
