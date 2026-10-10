from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

import pytest
from pydantic import (
    BaseModel,
    ConfigDict,
    StringConstraints,
    ValidationError,
    create_model,
)

from spine.application.diagnostics.audit import (
    AccessDecisionAuditPayload,
    AuditEventRegistry,
    AuditIdentifier,
    AuditObjectReference,
    AuditOutcome,
    CanonicalTransitionAuditPayload,
    CommandAuditPayload,
    FeedbackAuditPayload,
    OutboxDeliveryAuditPayload,
    UnsupportedAuditEventError,
)
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
    with pytest.raises(ValidationError) as captured:
        event.outcome = AuditOutcome.REJECTED  # type: ignore[misc]
    assert captured.value.errors()[0]["type"] == "frozen_instance"


@pytest.mark.parametrize(
    ("origin", "acting_subject_id", "service_principal_id"),
    (
        (ContextOrigin.INTERACTIVE, None, None),
        (ContextOrigin.WORKER, None, None),
        (ContextOrigin.WORKER, synthetic_uuid(4), synthetic_uuid(5)),
    ),
)
def test_audit_envelope_rejects_actor_scope_inconsistent_with_origin(
    origin: ContextOrigin,
    acting_subject_id: UUID | None,
    service_principal_id: UUID | None,
) -> None:
    registry = AuditEventRegistry.with_default_families()

    with pytest.raises(ValidationError):
        registry.build_event(
            workspace_id=synthetic_uuid(1),
            event_type="command.outcome",
            schema_version=1,
            payload=CommandAuditPayload(command_type="workspace.create"),
            origin=origin,
            acting_subject_id=acting_subject_id,
            service_principal_id=service_principal_id,
            trace_id=synthetic_uuid(3),
            occurred_at=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
            outcome=AuditOutcome.ACCEPTED,
            reason="command_valid",
        )


def test_audit_envelope_preserves_only_an_opaque_immutable_target() -> None:
    target = AuditObjectReference(
        object_type="document",
        object_id=synthetic_uuid(6),
        schema_version=1,
    )
    event = AuditEventRegistry.with_default_families().build_event(
        workspace_id=synthetic_uuid(1),
        event_type="command.outcome",
        schema_version=1,
        payload=CommandAuditPayload(command_type="workspace.create"),
        origin=ContextOrigin.INTERACTIVE,
        acting_subject_id=synthetic_uuid(2),
        service_principal_id=None,
        trace_id=synthetic_uuid(3),
        occurred_at=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
        outcome=AuditOutcome.ACCEPTED,
        reason="command_valid",
        target=target,
    )

    assert event.target == target
    assert event.target is not target
    with pytest.raises(ValidationError):
        event.target.object_type = "secret"  # type: ignore[misc,union-attr]


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
            allowed_outcomes=frozenset({AuditOutcome.ACCEPTED}),
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
            allowed_outcomes=frozenset({AuditOutcome.ACCEPTED}),
        )


@pytest.mark.parametrize(
    ("event_type", "payload", "outcome", "target"),
    (
        (
            "canonical.transition",
            CanonicalTransitionAuditPayload(
                transition="workspace.activated",
            ),
            AuditOutcome.COMMITTED,
            AuditObjectReference(
                object_type="workspace",
                object_id=synthetic_uuid(10),
                schema_version=1,
            ),
        ),
        (
            "access.decision",
            AccessDecisionAuditPayload(
                purpose="answer_question",
                operation="read_content",
            ),
            AuditOutcome.ALLOWED,
            None,
        ),
        (
            "outbox.delivery",
            OutboxDeliveryAuditPayload(
                outbox_event_id=synthetic_uuid(11),
                attempt_id=synthetic_uuid(12),
                attempt_number=2,
            ),
            AuditOutcome.RETRY_SCHEDULED,
            None,
        ),
        (
            "feedback.recorded",
            FeedbackAuditPayload(feedback_type="answer.helpful"),
            AuditOutcome.RECORDED,
            None,
        ),
    ),
)
def test_default_registry_supports_each_required_event_family(
    event_type: str,
    payload: BaseModel,
    outcome: AuditOutcome,
    target: AuditObjectReference | None,
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
        target=target,
    )

    assert type(event.payload) is type(payload)


def test_canonical_transition_requires_one_envelope_target() -> None:
    registry = AuditEventRegistry.with_default_families()

    with pytest.raises(UnsupportedAuditEventError, match="target"):
        registry.build_event(
            workspace_id=synthetic_uuid(20),
            event_type="canonical.transition",
            schema_version=1,
            payload=CanonicalTransitionAuditPayload(
                transition="workspace.activated",
            ),
            origin=ContextOrigin.INTERACTIVE,
            acting_subject_id=synthetic_uuid(21),
            service_principal_id=None,
            trace_id=synthetic_uuid(22),
            occurred_at=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
            outcome=AuditOutcome.COMMITTED,
            reason="transition_committed",
        )


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


def test_registry_rejects_content_bearing_payload_field_name() -> None:
    class PromptPayload(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        prompt: AuditIdentifier

    with pytest.raises(ValueError, match="content-bearing"):
        AuditEventRegistry().register(
            event_type="unsafe.prompt",
            schema_version=1,
            payload_type=PromptPayload,
            allowed_outcomes=frozenset({AuditOutcome.ACCEPTED}),
        )


def test_registry_rejects_sensitive_field_in_optional_nested_payload() -> None:
    class SensitiveDetails(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        prompt: AuditIdentifier

    class WrapperPayload(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        details: SensitiveDetails | None

    with pytest.raises(ValueError, match="content-bearing"):
        AuditEventRegistry().register(
            event_type="unsafe.nested_prompt",
            schema_version=1,
            payload_type=WrapperPayload,
            allowed_outcomes=frozenset({AuditOutcome.ACCEPTED}),
        )


def test_registry_rejects_password_field_name() -> None:
    class PasswordPayload(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        password: AuditIdentifier

    with pytest.raises(ValueError, match="content-bearing"):
        AuditEventRegistry().register(
            event_type="unsafe.password",
            schema_version=1,
            payload_type=PasswordPayload,
            allowed_outcomes=frozenset({AuditOutcome.ACCEPTED}),
        )


@pytest.mark.parametrize(
    "field_name",
    ("api_key", "connection_string", "cookie", "authorization"),
)
def test_registry_rejects_credential_shaped_field_names(field_name: str) -> None:
    payload_type = create_model(
        "CredentialPayload",
        __config__=ConfigDict(extra="forbid", frozen=True),
        **{field_name: (AuditIdentifier, ...)},
    )

    with pytest.raises(ValueError, match="content-bearing"):
        AuditEventRegistry().register(
            event_type="unsafe.credential",
            schema_version=1,
            payload_type=payload_type,
            allowed_outcomes=frozenset({AuditOutcome.ACCEPTED}),
        )


def test_registry_accepts_safe_count_despite_content_related_name() -> None:
    class SafeCountPayload(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        message_count: int

    registry = AuditEventRegistry()
    registry.register(
        event_type="safe.count",
        schema_version=1,
        payload_type=SafeCountPayload,
        allowed_outcomes=frozenset({AuditOutcome.RECORDED}),
    )
    registry.seal()


def test_registry_rejects_unclassified_bounded_string() -> None:
    BoundedString = Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=128,
            pattern=r"^[a-z][a-z0-9_.:-]{0,127}$",
        ),
    ]

    class UnclassifiedPayload(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        operation: BoundedString

    with pytest.raises(ValueError, match="bounded safe fields"):
        AuditEventRegistry().register(
            event_type="unsafe.unclassified",
            schema_version=1,
            payload_type=UnclassifiedPayload,
            allowed_outcomes=frozenset({AuditOutcome.ACCEPTED}),
        )


def test_registry_rejects_outcome_from_another_event_family() -> None:
    registry = AuditEventRegistry.with_default_families()

    with pytest.raises(UnsupportedAuditEventError, match="outcome"):
        registry.build_event(
            workspace_id=synthetic_uuid(40),
            event_type="access.decision",
            schema_version=1,
            payload=AccessDecisionAuditPayload(
                purpose="answer_question",
                operation="read_content",
            ),
            origin=ContextOrigin.INTERACTIVE,
            acting_subject_id=synthetic_uuid(41),
            service_principal_id=None,
            trace_id=synthetic_uuid(42),
            occurred_at=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
            outcome=AuditOutcome.COMPLETED,
            reason="invalid_family_outcome",
        )


def test_default_registry_cannot_be_mutated_after_construction() -> None:
    registry = AuditEventRegistry.with_default_families()

    with pytest.raises(RuntimeError, match="sealed"):
        registry.register(
            event_type="command.secondary",
            schema_version=1,
            payload_type=CommandAuditPayload,
            allowed_outcomes=frozenset({AuditOutcome.ACCEPTED}),
        )
