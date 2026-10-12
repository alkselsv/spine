from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.persistence.outbox import OutboxEventRegistry
from spine.application.diagnostics.audit import AuditEventRegistry
from spine.application.diagnostics.events import DiagnosticEvent, DiagnosticEventRegistry
from spine.application.storage import (
    StorageOutcomeEventPublisher,
    StorageOutcome,
    StorageOutcomeKind,
    StorageOutcomeStatus,
    build_storage_outcome_audit_payload,
    build_storage_outcome_diagnostic_payload,
    audit_outcome_for_storage,
    build_storage_outcome_intent,
    register_storage_outcome_outbox_event,
)
from spine.domain.common import ContextOrigin


WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000099")
TRACE_ID = UUID("20000000-0000-0000-0000-000000000099")
SUBJECT_ID = UUID("30000000-0000-0000-0000-000000000099")
ACTOR_ID = UUID("40000000-0000-0000-0000-000000000099")


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


class _AuditWriter:
    def __init__(self) -> None:
        self.events = []

    async def append(self, event):
        self.events.append(event)
        return event.audit_event_id or SUBJECT_ID


class _DiagnosticSink:
    def __init__(self) -> None:
        self.events: list[DiagnosticEvent] = []

    async def emit(self, event: DiagnosticEvent) -> None:
        self.events.append(event)


class _OutboxWriter:
    def __init__(self) -> None:
        self.intents = []

    async def append(self, intent):
        self.intents.append(intent)
        return intent.event_id or SUBJECT_ID


@pytest.mark.asyncio
async def test_event_publisher_writes_distinct_audit_diagnostic_and_outbox_records() -> None:
    audit_writer = _AuditWriter()
    diagnostic_sink = _DiagnosticSink()
    outbox_writer = _OutboxWriter()
    outbox_registry = OutboxEventRegistry()
    register_storage_outcome_outbox_event(outbox_registry)
    publisher = StorageOutcomeEventPublisher(
        audit_writer=audit_writer,
        diagnostic_sink=diagnostic_sink,
        outbox_writer=outbox_writer,
        audit_events=AuditEventRegistry.with_default_families(),
        diagnostic_events=DiagnosticEventRegistry.with_default_families(),
        outbox_events=outbox_registry,
        origin=ContextOrigin.INTERACTIVE,
        acting_subject_id=ACTOR_ID,
        service_principal_id=None,
    )

    await publisher.record(outcome())

    assert len(audit_writer.events) == 1
    assert len(diagnostic_sink.events) == 1
    assert len(outbox_writer.intents) == 1
    assert audit_writer.events[0].event_type == "storage.outcome"
    assert diagnostic_sink.events[0].event_type == "storage.outcome"
    assert outbox_writer.intents[0].event_type == "storage.outcome"
    assert audit_writer.events[0].payload_json() == {
        "attempt_number": 1,
        "error_code": "object_corrupt",
        "operation": "integrity",
        "outcome": "rejected",
        "status": "indeterminate",
    }
    assert diagnostic_sink.events[0].payload_json()["outcome"] == "indeterminate"


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
