from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.persistence.outbox import OutboxEventRegistry
from spine.application.storage import (
    StorageOutcome,
    StorageOutcomeKind,
    StorageOutcomeStatus,
    build_storage_outcome_audit_payload,
    build_storage_outcome_diagnostic_payload,
    audit_outcome_for_storage,
    build_storage_outcome_intent,
    register_storage_outcome_outbox_event,
)


WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000099")
TRACE_ID = UUID("20000000-0000-0000-0000-000000000099")
SUBJECT_ID = UUID("30000000-0000-0000-0000-000000000099")


def outcome(**updates: object) -> StorageOutcome:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "trace_id": TRACE_ID,
        "operation": StorageOutcomeKind.INTEGRITY,
        "status": StorageOutcomeStatus.INDETERMINATE,
        "subject_id": SUBJECT_ID,
        "error_code": "object_corrupt",
        "producer_deduplication_id": "storage:integrity:subject:corrupt",
    }
    values.update(updates)
    return StorageOutcome.model_validate(values)


def test_storage_outcome_rejects_unbounded_error_text() -> None:
    with pytest.raises(ValidationError):
        outcome(error_code="provider failure leaked here")


def test_storage_outcome_builds_idempotent_typed_outbox_intent() -> None:
    registry = OutboxEventRegistry()
    register_storage_outcome_outbox_event(registry)

    intent = build_storage_outcome_intent(registry, outcome())

    assert intent.workspace_id == WORKSPACE_ID
    assert intent.trace_id == TRACE_ID
    assert intent.aggregate is not None
    assert intent.aggregate.object_id == SUBJECT_ID
    assert intent.payload_json() == {
        "attempt_number": 1,
        "error_code": "object_corrupt",
        "operation": "integrity",
        "producer_deduplication_id": "storage:integrity:subject:corrupt",
        "status": "indeterminate",
        "subject_id": str(SUBJECT_ID),
    }


def test_storage_outcome_projects_to_distinct_audit_and_diagnostic_payloads() -> None:
    audit_payload = build_storage_outcome_audit_payload(outcome())
    diagnostic_payload = build_storage_outcome_diagnostic_payload(outcome())

    assert audit_payload.operation == diagnostic_payload.operation
    assert audit_payload.status == diagnostic_payload.status
    assert audit_payload.outcome == "rejected"
    assert diagnostic_payload.outcome == "indeterminate"
    assert audit_payload.error_code == "object_corrupt"
    assert audit_outcome_for_storage(outcome()).value == "rejected"
    assert audit_outcome_for_storage(
        outcome(status=StorageOutcomeStatus.RETRY_SCHEDULED)
    ).value == "retry_scheduled"


def test_storage_outcome_without_subject_is_still_tenant_scoped() -> None:
    registry = OutboxEventRegistry()
    register_storage_outcome_outbox_event(registry)

    intent = build_storage_outcome_intent(
        registry,
        outcome(subject_id=None, operation=StorageOutcomeKind.RECONCILIATION),
    )

    assert intent.aggregate is None
    assert intent.workspace_id == WORKSPACE_ID
