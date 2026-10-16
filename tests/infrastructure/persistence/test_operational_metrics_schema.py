from __future__ import annotations

from spine.infrastructure.persistence.postgresql_mappings import operational_metric_events


def test_operational_metric_event_mapping_preserves_tenant_and_deduplication_keys() -> None:
    assert {column.name for column in operational_metric_events.primary_key.columns} == {
        "workspace_id",
        "environment_id",
        "event_id",
        "measurement",
    }
    assert {column.name for column in operational_metric_events.c} >= {
        "source_identity",
        "trace_id",
        "correlation_id",
        "measurement",
        "event_at",
        "deduplication_key",
        "logical_operation_id",
        "attempt_id",
        "attempt_number",
        "source_watermark_at",
        "projection_watermark_at",
        "value",
        "currency",
        "answer_outcome",
        "accepted_evidence",
        "feedback_id",
        "feedback_outcome",
    }
    assert any(
        constraint.name == "uq_operational_metric_events_deduplication"
        for constraint in operational_metric_events.constraints
    )
