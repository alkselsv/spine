from __future__ import annotations

from spine.infrastructure.persistence.postgresql_mappings import (
    operational_correlation_records,
)


def test_operational_correlation_mapping_is_tenant_scoped_and_deduplicated() -> None:
    assert {column.name for column in operational_correlation_records.primary_key.columns} == {
        "workspace_id",
        "environment_id",
        "source_kind",
        "source_record_id",
        "deduplication_key",
    }
    assert {column.name for column in operational_correlation_records.c} >= {
        "source_identity",
        "source_schema_version",
        "trace_id",
        "correlation_id",
        "run_id",
        "delivery_event_id",
        "metric_measurement",
        "metric_coverage",
        "delivery_last_error_code",
        "run_outcome",
        "feedback_outcome",
    }
    assert any(
        constraint.name == "uq_operational_correlation_records_deduplication"
        for constraint in operational_correlation_records.constraints
    )
