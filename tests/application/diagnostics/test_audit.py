from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from spine.application.diagnostics.audit import (
    AccessDecisionAuditPayload,
    AuditEventRegistry,
    AuditOutcome,
    CanonicalTransitionAuditPayload,
    CommandAuditPayload,
    FeedbackAuditPayload,
    OutboxDeliveryAuditPayload,
)
from spine.application.persistence import OpaqueObjectReference
from spine.application.persistence.context import ContextOrigin


def synthetic_uuid(value: int) -> UUID:
    return UUID(int=value)


def test_registered_command_event_is_a_detached_immutable_snapshot() -> None:
    registry = AuditEventRegistry.with_default_families()
    payload = CommandAuditPayload(command_type="workspace.create")

    event = registry.build_event(
        workspace_id=synthetic_uuid(1),
        event_type="command.outcome",
        schema_version=1,
        payload=payload,
        origin=ContextOrigin.INTERACTIVE,
        acting_subject_id=synthetic_uuid(2),
        service_principal_id=None,
        trace_id=synthetic_uuid(3),
        occurred_at=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
        outcome=AuditOutcome.ACCEPTED,
        reason="command_valid",
    )

    assert event.payload_json() == {"command_type": "workspace.create"}
    assert event.audit_event_id is None
    assert event.appended_at is None
    with pytest.raises(Exception):
        event.outcome = AuditOutcome.REJECTED  # type: ignore[misc]


def test_registry_rejects_payload_with_mutable_fields() -> None:
    class UnsafePayload(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        values: list[str]

    registry = AuditEventRegistry()

    with pytest.raises(ValueError, match="deeply immutable"):
        registry.register(
            event_type="unsafe.event",
            schema_version=1,
            payload_type=UnsafePayload,
        )


def test_registry_rejects_payload_with_unbounded_string_field() -> None:
    class ContentBearingPayload(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        raw_value: str

    registry = AuditEventRegistry()

    with pytest.raises(ValueError, match="bounded safe fields"):
        registry.register(
            event_type="unsafe.content",
            schema_version=1,
            payload_type=ContentBearingPayload,
        )


@pytest.mark.parametrize(
    ("event_type", "payload", "outcome"),
    (
        (
            "canonical.transition",
            CanonicalTransitionAuditPayload(
                transition="workspace.activated",
                object=OpaqueObjectReference(
                    object_type="workspace",
                    object_id=synthetic_uuid(10),
                    schema_version=1,
                ),
            ),
            AuditOutcome.COMMITTED,
        ),
        (
            "access.decision",
            AccessDecisionAuditPayload(
                purpose="answer_question",
                operation="read_content",
            ),
            AuditOutcome.ALLOWED,
        ),
        (
            "outbox.delivery",
            OutboxDeliveryAuditPayload(
                outbox_event_id=synthetic_uuid(11),
                attempt_id=synthetic_uuid(12),
                attempt_number=2,
            ),
            AuditOutcome.RETRY_SCHEDULED,
        ),
        (
            "feedback.recorded",
            FeedbackAuditPayload(feedback_type="answer.helpful"),
            AuditOutcome.RECORDED,
        ),
    ),
)
def test_default_registry_supports_each_required_event_family(
    event_type: str,
    payload: BaseModel,
    outcome: AuditOutcome,
) -> None:
    event = AuditEventRegistry.with_default_families().build_event(
        workspace_id=synthetic_uuid(20),
        event_type=event_type,
        schema_version=1,
        payload=payload,
        origin=ContextOrigin.INTERACTIVE,
        acting_subject_id=synthetic_uuid(21),
        service_principal_id=None,
        trace_id=synthetic_uuid(22),
        occurred_at=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
        outcome=outcome,
        reason="registered_outcome",
    )

    assert type(event.payload) is type(payload)


def test_audit_envelope_rejects_content_bearing_producer_identity() -> None:
    registry = AuditEventRegistry.with_default_families()

    with pytest.raises(ValidationError):
        registry.build_event(
            workspace_id=synthetic_uuid(30),
            event_type="command.outcome",
            schema_version=1,
            payload=CommandAuditPayload(command_type="workspace.create"),
            origin=ContextOrigin.INTERACTIVE,
            acting_subject_id=synthetic_uuid(31),
            service_principal_id=None,
            trace_id=synthetic_uuid(32),
            occurred_at=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
            outcome=AuditOutcome.ACCEPTED,
            reason="command_valid",
            producer_deduplication_id="Bearer protected-token",
        )
