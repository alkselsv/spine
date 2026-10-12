"""Disclosure-safe outcomes emitted by original-object storage producers."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from spine.application.persistence.outbox import (
    OpaqueObjectReference,
    OutboxEventRegistry,
    OutboxIntent,
    OutboxWriter,
)
from spine.application.diagnostics.audit import AuditEventRegistry, AuditWriter
from spine.application.diagnostics.context import DiagnosticContext
from spine.application.diagnostics.events import (
    DiagnosticEventRegistry,
    DiagnosticSeverity,
    DiagnosticSink,
    StorageOutcomeDiagnosticPayload,
)
from spine.domain.audit import (
    AuditObjectReference,
    AuditOutcome,
    StorageOutcomeAuditPayload,
)
from spine.domain.common import ContextOrigin


_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")


class StorageOutcomeKind(str, Enum):
    UPLOAD = "upload"
    RETRY = "retry"
    INTEGRITY = "integrity"
    RECONCILIATION = "reconciliation"
    ORPHAN = "orphan"
    MISSING = "missing"
    DELETION = "deletion"
    DELIVERY = "delivery"


class StorageOutcomeStatus(str, Enum):
    SUCCEEDED = "succeeded"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    INDETERMINATE = "indeterminate"
    ABORTED = "aborted"
    APPROVED_DELETION = "approved_deletion"
    RETRY_SCHEDULED = "retry_scheduled"


class StorageOutcome(BaseModel):
    """Immutable, safe-to-forward storage fact.

    It intentionally contains opaque identities and stable categories only:
    no bytes, paths, filenames, provider details, URLs or exception text.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workspace_id: UUID
    environment_id: UUID | None = None
    trace_id: UUID
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    operation: StorageOutcomeKind
    status: StorageOutcomeStatus
    attempt_number: int = Field(default=1, ge=1)
    subject_id: UUID | None = None
    error_code: str | None = None
    producer_deduplication_id: str = Field(min_length=1, max_length=128)

    @field_validator("producer_deduplication_id")
    @classmethod
    def validate_deduplication_id(cls, value: str) -> str:
        if _SAFE_CODE.fullmatch(value) is None:
            raise ValueError("producer_deduplication_id must be a bounded code")
        return value

    @field_validator(
        "workspace_id",
        "environment_id",
        "trace_id",
        "correlation_id",
        "causation_id",
        "subject_id",
    )
    @classmethod
    def reject_zero_identity(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("storage outcome identities must be non-zero")
        return value

    @field_validator("error_code")
    @classmethod
    def validate_error_code(cls, value: str | None) -> str | None:
        if value is not None and (
            _SAFE_CODE.fullmatch(value) is None
        ):
            raise ValueError("error_code must be a bounded code")
        return value


class StorageOutcomeOutboxPayload(BaseModel):
    """Versioned payload for at-least-once downstream storage outcome delivery."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    operation: StorageOutcomeKind
    status: StorageOutcomeStatus
    attempt_number: int = Field(ge=1)
    subject_id: UUID | None = None
    error_code: str | None = None
    producer_deduplication_id: str = Field(min_length=1, max_length=128)

    @field_validator("subject_id")
    @classmethod
    def validate_outbox_subject_id(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("subject_id must be non-zero")
        return value

    @field_validator("producer_deduplication_id")
    @classmethod
    def validate_outbox_deduplication_id(cls, value: str) -> str:
        if _SAFE_CODE.fullmatch(value) is None:
            raise ValueError("producer_deduplication_id must be a bounded code")
        return value

    @field_validator("error_code")
    @classmethod
    def validate_outbox_error_code(cls, value: str | None) -> str | None:
        if value is not None and _SAFE_CODE.fullmatch(value) is None:
            raise ValueError("error_code must be a bounded code")
        return value


STORAGE_OUTCOME_EVENT_TYPE = "storage.outcome"
STORAGE_OUTCOME_SCHEMA_VERSION = 1


def register_storage_outcome_outbox_event(registry: OutboxEventRegistry) -> None:
    """Register the single storage outcome route in a composition root."""

    registry.register(
        event_type=STORAGE_OUTCOME_EVENT_TYPE,
        schema_version=STORAGE_OUTCOME_SCHEMA_VERSION,
        payload_type=StorageOutcomeOutboxPayload,
    )


def build_storage_outcome_audit_payload(
    outcome: StorageOutcome,
) -> StorageOutcomeAuditPayload:
    """Project the safe fact into the immutable Audit Event payload schema."""

    return StorageOutcomeAuditPayload(
        operation=outcome.operation.value,
        outcome=audit_outcome_for_storage(outcome).value,
        status=outcome.status.value,
        attempt_number=outcome.attempt_number,
        error_code=outcome.error_code,
    )


def audit_outcome_for_storage(outcome: StorageOutcome) -> AuditOutcome:
    """Map operational storage status to the closed Audit outcome vocabulary."""

    if outcome.status is StorageOutcomeStatus.RETRY_SCHEDULED:
        return AuditOutcome.RETRY_SCHEDULED
    if outcome.status in {
        StorageOutcomeStatus.INDETERMINATE,
        StorageOutcomeStatus.UNAVAILABLE,
    }:
        return AuditOutcome.REJECTED
    return AuditOutcome.COMPLETED


def build_storage_outcome_diagnostic_payload(
    outcome: StorageOutcome,
) -> StorageOutcomeDiagnosticPayload:
    """Project the same fact into the independently disposable diagnostic schema."""

    return StorageOutcomeDiagnosticPayload(
        operation=outcome.operation.value,
        outcome=outcome.status.value,
        status=outcome.status.value,
        attempt_number=outcome.attempt_number,
        error_code=outcome.error_code,
    )


def build_storage_outcome_intent(
    registry: OutboxEventRegistry,
    outcome: StorageOutcome,
) -> OutboxIntent:
    """Build an immutable, idempotent outbox intent from a storage outcome."""

    payload = StorageOutcomeOutboxPayload(
        operation=outcome.operation,
        status=outcome.status,
        attempt_number=outcome.attempt_number,
        subject_id=outcome.subject_id,
        error_code=outcome.error_code,
        producer_deduplication_id=outcome.producer_deduplication_id,
    )
    aggregate = (
        OpaqueObjectReference(
            object_type="storage.subject",
            object_id=outcome.subject_id,
            schema_version=1,
        )
        if outcome.subject_id is not None
        else None
    )
    return registry.build_intent(
        workspace_id=outcome.workspace_id,
        environment_id=outcome.environment_id,
        event_type=STORAGE_OUTCOME_EVENT_TYPE,
        schema_version=STORAGE_OUTCOME_SCHEMA_VERSION,
        payload=payload,
        trace_id=outcome.trace_id,
        aggregate=aggregate,
        producer_deduplication_id=outcome.producer_deduplication_id,
    )


class StorageOutcomeRecorder(Protocol):
    """Publish one outcome to the audit/diagnostic/outbox composition seam."""

    async def record(self, outcome: StorageOutcome) -> None: ...


class StorageOutcomeEventPublisher:
    """Publish one storage fact to the three existing event ports."""

    def __init__(
        self,
        *,
        audit_writer: AuditWriter,
        diagnostic_sink: DiagnosticSink,
        outbox_writer: OutboxWriter,
        audit_events: AuditEventRegistry,
        diagnostic_events: DiagnosticEventRegistry,
        outbox_events: OutboxEventRegistry,
        origin: ContextOrigin,
        acting_subject_id: UUID | None,
        service_principal_id: UUID | None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._audit_writer = audit_writer
        self._diagnostic_sink = diagnostic_sink
        self._outbox_writer = outbox_writer
        self._audit_events = audit_events
        self._diagnostic_events = diagnostic_events
        self._outbox_events = outbox_events
        self._origin = origin
        self._acting_subject_id = acting_subject_id
        self._service_principal_id = service_principal_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def record(self, outcome: StorageOutcome) -> None:
        occurred_at = self._clock()
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("storage outcome clock must return timezone-aware datetime")
        audit_event = self._audit_events.build_event(
            workspace_id=outcome.workspace_id,
            environment_id=outcome.environment_id,
            event_type="storage.outcome",
            schema_version=1,
            origin=self._origin,
            acting_subject_id=self._acting_subject_id,
            service_principal_id=self._service_principal_id,
            trace_id=outcome.trace_id,
            correlation_id=outcome.correlation_id,
            causation_id=outcome.causation_id,
            occurred_at=occurred_at,
            outcome=audit_outcome_for_storage(outcome),
            reason=outcome.status.value,
            target=(
                AuditObjectReference(
                    object_type="storage.subject",
                    object_id=outcome.subject_id,
                    schema_version=1,
                )
                if outcome.subject_id is not None
                else None
            ),
            producer_deduplication_id=outcome.producer_deduplication_id,
            payload=build_storage_outcome_audit_payload(outcome),
        )
        await self._audit_writer.append(audit_event)
        await self._outbox_writer.append(
            build_storage_outcome_intent(self._outbox_events, outcome)
        )
        diagnostic_context = DiagnosticContext(
            trace_id=outcome.trace_id,
            correlation_id=outcome.correlation_id,
            causation_id=outcome.causation_id,
        )
        severity = (
            DiagnosticSeverity.WARNING
            if outcome.status
            in {
                StorageOutcomeStatus.DEGRADED,
                StorageOutcomeStatus.UNAVAILABLE,
                StorageOutcomeStatus.INDETERMINATE,
                StorageOutcomeStatus.ABORTED,
            }
            else DiagnosticSeverity.INFO
        )
        diagnostic_event = self._diagnostic_events.build_event(
            event_type="storage.outcome",
            schema_version=1,
            severity=severity,
            occurred_at=occurred_at,
            diagnostic_context=diagnostic_context,
            payload=build_storage_outcome_diagnostic_payload(outcome),
            workspace_id=outcome.workspace_id,
            environment_id=outcome.environment_id,
        )
        await self._diagnostic_sink.emit(diagnostic_event)


class NullStorageOutcomeRecorder:
    """Default sink for compatibility callers that do not configure telemetry."""

    async def record(self, outcome: StorageOutcome) -> None:
        return None


class RecordingStorageOutcomeRecorder:
    """Deterministic recorder used by application and contract tests."""

    def __init__(self) -> None:
        self.outcomes: list[StorageOutcome] = []

    async def record(self, outcome: StorageOutcome) -> None:
        self.outcomes.append(outcome)


async def record_storage_outcome_safely(
    recorder: StorageOutcomeRecorder,
    outcome: StorageOutcome,
) -> None:
    """Never turn a post-commit observability failure into storage rollback."""

    try:
        await recorder.record(outcome)
    except Exception:
        return


__all__ = [
    "RecordingStorageOutcomeRecorder",
    "StorageOutcomeEventPublisher",
    "NullStorageOutcomeRecorder",
    "StorageOutcome",
    "StorageOutcomeKind",
    "StorageOutcomeOutboxPayload",
    "StorageOutcomeRecorder",
    "StorageOutcomeStatus",
    "STORAGE_OUTCOME_EVENT_TYPE",
    "STORAGE_OUTCOME_SCHEMA_VERSION",
    "build_storage_outcome_intent",
    "build_storage_outcome_audit_payload",
    "build_storage_outcome_diagnostic_payload",
    "audit_outcome_for_storage",
    "register_storage_outcome_outbox_event",
    "record_storage_outcome_safely",
]
