"""Bounded, immutable qualification-provisioning definitions.

These definitions describe an approved operation without carrying credentials,
provider payloads, or request-controlled authority selectors.  The application
service owns the lifecycle; the domain only validates the shape and identity of
the approved profile and its safe evidence.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
import hashlib
import json
import re
from typing import Any, Literal, Mapping
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from spine.auth.records import AuthorizationRecordStatus, QualificationAuthorityBinding
from spine.domain.common import ValidatedDefinitionModel


QUALIFICATION_PROFILE_SCHEMA = "qualification-profile:v1"
QUALIFICATION_OPERATION_SCHEMA = "qualification-operation:v1"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_REFERENCE = re.compile(r"^[a-z][a-z0-9_.-]{0,31}:[a-z0-9_.:/-]{1,95}$")


def _require_id(value: UUID, *, field_name: str) -> UUID:
    if not isinstance(value, UUID) or value.int == 0:
        raise ValueError(f"{field_name} must be a non-zero UUID")
    return value


def _require_opaque_reference(value: str, *, field_name: str) -> str:
    if _OPAQUE_REFERENCE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded opaque reference")
    return value


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="python"))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, frozenset):
        return sorted((_json_value(item) for item in value), key=repr)
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


class QualificationOperation(str, Enum):
    """The only mutations exposed by the qualification operator boundary."""

    PROVISION = "provision"
    REVOKE = "revoke"
    CLEANUP = "cleanup"


class QualificationOperationState(str, Enum):
    """Durable states for one idempotent operator operation."""

    REQUESTED = "requested"
    VALIDATED = "validated"
    INACTIVE = "inactive"
    INCOMPLETE = "incomplete"
    ACTIVE = "active"
    REVOKED = "revoked"
    CLEANUP_PENDING = "cleanup_pending"
    CLEANED = "cleaned"


class QualificationOpaqueReference(ValidatedDefinitionModel):
    """An approved reference; it is never a secret value or a selector."""

    value: str = Field(min_length=1, max_length=128)

    @field_validator("value")
    @classmethod
    def _validate_value(cls, value: str) -> str:
        return _require_opaque_reference(value, field_name="reference")


class QualificationProfile(ValidatedDefinitionModel):
    """One immutable, server-approved qualification operation profile.

    The profile contains only references to external targets, roles, policies,
    and secrets.  It carries a disabled canonical binding plan so activation is
    always a separate, auditable transition.
    """

    schema_version: Literal[QUALIFICATION_PROFILE_SCHEMA] = QUALIFICATION_PROFILE_SCHEMA
    profile_id: UUID
    profile_version: int = Field(ge=1)
    profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    qualification_id: UUID
    workspace_id: UUID
    environment_id: UUID
    target_reference: QualificationOpaqueReference
    binding: QualificationAuthorityBinding
    allowed_operations: frozenset[QualificationOperation] = Field(min_length=1)
    secret_references: tuple[QualificationOpaqueReference, ...] = Field(
        default=(), min_length=1
    )
    database_role_references: tuple[QualificationOpaqueReference, ...] = Field(
        default=(), min_length=1
    )
    access_policy_references: tuple[QualificationOpaqueReference, ...] = Field(
        default=(), min_length=1
    )
    approved: bool

    @classmethod
    def create(cls, **values: Any) -> "QualificationProfile":
        """Create a profile while calculating its canonical digest."""

        values = dict(values)
        values.pop("profile_digest", None)
        values.setdefault("schema_version", QUALIFICATION_PROFILE_SCHEMA)
        payload = _json_value(values)
        encoded = json.dumps(
            {"schema": QUALIFICATION_PROFILE_SCHEMA, "payload": payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        values["profile_digest"] = hashlib.sha256(encoded).hexdigest()
        return cls.model_validate(values)

    @classmethod
    def calculate_digest(cls, profile: "QualificationProfile") -> str:
        values = profile.model_dump(mode="python", exclude={"profile_digest"})
        payload = _json_value(values)
        encoded = json.dumps(
            {"schema": QUALIFICATION_PROFILE_SCHEMA, "payload": payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @field_validator(
        "profile_id",
        "qualification_id",
        "workspace_id",
        "environment_id",
    )
    @classmethod
    def _validate_ids(cls, value: UUID, info: Any) -> UUID:
        return _require_id(value, field_name=info.field_name)

    @field_validator("profile_digest")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("profile_digest must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def _validate_profile(self) -> "QualificationProfile":
        if self.schema_version != QUALIFICATION_PROFILE_SCHEMA:
            raise ValueError("unsupported qualification profile schema")
        if not self.approved:
            raise ValueError("qualification profile is not approved")
        if self.profile_digest != self.calculate_digest(self):
            raise ValueError("qualification profile digest does not match content")
        if not self.allowed_operations:
            raise ValueError("qualification profile must allow at least one operation")
        if self.binding.status is not AuthorizationRecordStatus.DISABLED:
            raise ValueError("qualification profile binding must start inactive")
        if (
            self.binding.qualification_id != self.qualification_id
            or self.binding.workspace_id != self.workspace_id
            or self.binding.environment_id != self.environment_id
        ):
            raise ValueError("qualification profile binding scope does not match profile")
        return self


class QualificationOperationReceipt(ValidatedDefinitionModel):
    """Safe durable result of one qualification mutation request."""

    schema_version: Literal[QUALIFICATION_OPERATION_SCHEMA] = QUALIFICATION_OPERATION_SCHEMA
    operation_id: UUID
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation: QualificationOperation
    state: QualificationOperationState
    profile_id: UUID
    profile_version: int = Field(ge=1)
    profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_reference: QualificationOpaqueReference
    binding_id: UUID
    generation_before: int | None = Field(default=None, ge=1)
    generation_after: int | None = Field(default=None, ge=1)
    audit_reference: QualificationOpaqueReference | None = None
    cleanup_pending: bool = False
    replay: bool = False

    @field_validator("operation_id", "profile_id", "binding_id")
    @classmethod
    def _validate_receipt_ids(cls, value: UUID, info: Any) -> UUID:
        return _require_id(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _validate_generation(self) -> "QualificationOperationReceipt":
        if self.generation_after is not None and self.generation_before is None:
            raise ValueError("generation_before is required with generation_after")
        if self.generation_before is not None and self.generation_after is not None:
            if self.generation_after < self.generation_before:
                raise ValueError("authorization generation cannot move backwards")
        if self.cleanup_pending != (
            self.state is QualificationOperationState.CLEANUP_PENDING
        ):
            raise ValueError("cleanup_pending must match the operation state")
        return self


class QualificationAuditRecord(ValidatedDefinitionModel):
    """Content-free immutable evidence for a lifecycle transition."""

    event_id: UUID
    operation_id: UUID
    operation: QualificationOperation
    transition: QualificationOperationState
    profile_id: UUID
    profile_version: int = Field(ge=1)
    profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_reference: QualificationOpaqueReference
    operator_reference: QualificationOpaqueReference
    approval_reference: QualificationOpaqueReference
    binding_id: UUID
    generation_before: int | None = Field(default=None, ge=1)
    generation_after: int | None = Field(default=None, ge=1)
    reason_code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")

    @field_validator("event_id", "operation_id", "profile_id", "binding_id")
    @classmethod
    def _validate_audit_ids(cls, value: UUID, info: Any) -> UUID:
        return _require_id(value, field_name=info.field_name)


__all__ = [
    "QUALIFICATION_OPERATION_SCHEMA",
    "QUALIFICATION_PROFILE_SCHEMA",
    "QualificationAuditRecord",
    "QualificationOpaqueReference",
    "QualificationOperation",
    "QualificationOperationReceipt",
    "QualificationOperationState",
    "QualificationProfile",
]
