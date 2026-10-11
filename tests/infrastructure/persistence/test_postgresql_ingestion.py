from uuid import UUID

from spine.infrastructure.persistence.postgresql import _PostgreSQLIngestionRepository


def test_transition_row_mapping_removes_storage_only_safe_metrics_schema() -> None:
    row = {
        "ingestion_transition_id": UUID("10000000-0000-0000-0000-000000000001"),
        "workspace_id": UUID("10000000-0000-0000-0000-000000000002"),
        "environment_id": UUID("10000000-0000-0000-0000-000000000003"),
        "target_kind": "run",
        "ingestion_run_id": UUID("10000000-0000-0000-0000-000000000004"),
        "ingestion_run_source_id": None,
        "predecessor_transition_id": None,
        "state": "requested",
        "attempt": 1,
        "safe_metrics_schema": "ingestion-safe-detail:v1",
        "safe_metrics": {"attempt": 1},
        "actor_or_service": "ingestion-ledger",
        "trace_id": UUID("10000000-0000-0000-0000-000000000005"),
        "occurred_at": "2026-10-11T12:00:00+00:00",
    }

    transition = _PostgreSQLIngestionRepository._transition_from_row(row)

    assert transition.safe_metrics is not None
    assert transition.safe_metrics.attempt == 1
