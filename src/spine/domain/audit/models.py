"""Immutable canonical Audit Event domain models."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from typing import Annotated, Any
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from spine.domain.common import ContextOrigin


AUDIT_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_.:-]{0,127}$"
_AUDIT_IDENTIFIER = re.compile(AUDIT_IDENTIFIER_PATTERN)


class AuditFieldKind(str, Enum):
    """Explicit semantic classifications accepted in Audit payload schemas."""

    IDENTIFIER = "identifier"


AuditIdentifier = Annotated[
    str,
    AuditFieldKind.IDENTIFIER,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=AUDIT_IDENTIFIER_PATTERN,
    ),
]


def _require_identifier(value: str, *, field_name: str) -> str:
    if _AUDIT_IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded identifier.")
    return value


def _require_non_zero_uuid(value: UUID | None, *, field_name: str) -> UUID | None:
    if value is not None and value.int == 0:
        raise ValueError(f"{field_name} must be a non-zero UUID.")
    return value


class AuditOutcome(str, Enum):
    """Safe outcome vocabulary used by registered Audit Event families."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    REPLAYED = "replayed"
    COMPLETED = "completed"
    COMMITTED = "committed"
    ALLOWED = "allowed"
    DENIED = "denied"
    SUCCEEDED = "succeeded"
    RETRY_SCHEDULED = "retry_scheduled"
    QUARANTINED = "quarantined"
    RECORDED = "recorded"


class AuditObjectReference(BaseModel):
    """Opaque non-content-bearing reference to one canonical object."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    object_type: AuditIdentifier
    object_id: UUID
    schema_version: int

    @field_validator("object_type")
    @classmethod
    def validate_object_type(cls, value: str) -> str:
        return _require_identifier(value, field_name="object_type")

    @field_validator("object_id")
    @classmethod
    def validate_object_id(cls, value: UUID) -> UUID:
        checked = _require_non_zero_uuid(value, field_name="object_id")
        assert checked is not None
        return checked

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        if isinstance(value, bool) or value <= 0:
            raise ValueError("schema_version must be a positive integer.")
        return value


class CommandAuditPayload(BaseModel):
    """Safe command-family payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_type: AuditIdentifier

    @field_validator("command_type")
    @classmethod
    def validate_command_type(cls, value: str) -> str:
        return _require_identifier(value, field_name="command_type")


class CanonicalTransitionAuditPayload(BaseModel):
    """Safe canonical-transition payload; the envelope owns the target."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    transition: AuditIdentifier

    @field_validator("transition")
    @classmethod
    def validate_transition(cls, value: str) -> str:
        return _require_identifier(value, field_name="transition")


class AccessDecisionAuditPayload(BaseModel):
    """Safe access-decision payload without protected target content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    purpose: AuditIdentifier
    operation: AuditIdentifier

    @field_validator("purpose", "operation")
    @classmethod
    def validate_policy_identifier(cls, value: str, info: Any) -> str:
        return _require_identifier(value, field_name=info.field_name)


class OutboxDeliveryAuditPayload(BaseModel):
    """Safe delivery-attempt payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outbox_event_id: UUID
    attempt_id: UUID
    attempt_number: int

    @field_validator("outbox_event_id", "attempt_id")
    @classmethod
    def validate_delivery_identifier(cls, value: UUID, info: Any) -> UUID:
        checked = _require_non_zero_uuid(value, field_name=info.field_name)
        assert checked is not None
        return checked

    @field_validator("attempt_number")
    @classmethod
    def validate_attempt_number(cls, value: int) -> int:
        if isinstance(value, bool) or value <= 0:
            raise ValueError("attempt_number must be a positive integer.")
        return value


class FeedbackAuditPayload(BaseModel):
    """Safe feedback-family payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    feedback_type: AuditIdentifier

    @field_validator("feedback_type")
    @classmethod
    def validate_feedback_type(cls, value: str) -> str:
        return _require_identifier(value, field_name="feedback_type")


class AuditEvent(BaseModel):
    """Detached immutable evidence snapshot validated by an event registry."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        arbitrary_types_allowed=True,
        revalidate_instances="always",
    )

    audit_event_id: UUID | None = None
    event_type: AuditIdentifier
    schema_version: int
    workspace_id: UUID
    environment_id: UUID | None = None
    origin: ContextOrigin
    acting_subject_id: UUID | None = None
    service_principal_id: UUID | None = None
    trace_id: UUID
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    occurred_at: datetime
    appended_at: datetime | None = None
    target: AuditObjectReference | None = None
    outcome: AuditOutcome
    reason: AuditIdentifier
    producer_deduplication_id: AuditIdentifier | None = None
    payload: BaseModel
    payload_snapshot: bytes | None = Field(default=None, exclude=True, repr=False)

    @field_validator("event_type", "reason", "producer_deduplication_id")
    @classmethod
    def validate_identifier(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _require_identifier(value, field_name=info.field_name)

    @field_validator("schema_version")
    @classmethod
    def validate_event_schema_version(cls, value: int) -> int:
        if isinstance(value, bool) or value <= 0:
            raise ValueError("schema_version must be a positive integer.")
        return value

    @field_validator(
        "audit_event_id",
        "workspace_id",
        "environment_id",
        "acting_subject_id",
        "service_principal_id",
        "trace_id",
        "correlation_id",
        "causation_id",
    )
    @classmethod
    def validate_identifiers(cls, value: UUID | None, info: Any) -> UUID | None:
        return _require_non_zero_uuid(value, field_name=info.field_name)

    @field_validator("occurred_at", "appended_at")
    @classmethod
    def validate_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("audit timestamps must be timezone-aware.")
        return value

    @model_validator(mode="after")
    def validate_actor_scope(self) -> AuditEvent:
        if self.origin is ContextOrigin.INTERACTIVE:
            if self.acting_subject_id is None:
                raise ValueError("interactive audit events require an acting subject.")
        elif self.origin is ContextOrigin.WORKER:
            if self.acting_subject_id is not None or self.service_principal_id is None:
                raise ValueError(
                    "worker audit events require only a service principal."
                )
        return self

    def payload_json(self) -> dict[str, Any]:
        """Return the registry-validated payload in JSON-compatible form."""

        if self.payload_snapshot is None:
            raise ValueError("Audit Event payload has not been registry validated.")
        payload = json.loads(self.payload_snapshot)
        if not isinstance(payload, dict):
            raise ValueError("Audit Event payload must serialize as an object.")
        return payload

    def detached_snapshot(
        self,
        *,
        update: Mapping[str, Any] | None = None,
    ) -> AuditEvent:
        """Return a deep detached copy revalidated after trusted updates."""

        copied = super().model_copy(update=update, deep=True)
        return type(self).model_validate(copied)
