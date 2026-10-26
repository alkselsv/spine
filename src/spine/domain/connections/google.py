"""Versioned, references-only Google connection definitions."""

from __future__ import annotations

from enum import Enum
import re
from uuid import UUID

from pydantic import Field, ValidationInfo, field_validator, model_validator

from spine.domain.common import ValidatedDefinitionModel


_OPAQUE_REFERENCE = re.compile(r"^[a-z][a-z0-9_.-]{0,31}:[A-Za-z0-9_.:/-]{1,191}$")


def _non_nil_uuid(value: UUID, info: ValidationInfo) -> UUID:
    if value.int == 0:
        raise ValueError(f"{info.field_name} must be a non-nil UUID")
    return value


class OpaqueGoogleReference(ValidatedDefinitionModel):
    """A bounded provider-neutral reference, never credential material."""

    value: str = Field(min_length=3, max_length=224)

    @field_validator("value")
    @classmethod
    def _validate_value(cls, value: str) -> str:
        if _OPAQUE_REFERENCE.fullmatch(value) is None:
            raise ValueError("value must be a bounded opaque reference")
        return value


class GoogleConnectionStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


class GoogleConnection(ValidatedDefinitionModel):
    """One immutable version of a Spine-owned Google connection."""

    connection_version_id: UUID
    connection_id: UUID
    version: int = Field(ge=1)
    predecessor_version_id: UUID | None = None
    workspace_id: UUID
    environment_id: UUID
    service_principal_id: UUID
    credential_reference: OpaqueGoogleReference
    grant_reference: OpaqueGoogleReference
    approved_resource_scope_references: tuple[OpaqueGoogleReference, ...] = Field(
        min_length=1
    )
    authorization_generation: int = Field(ge=1)
    configuration_generation: int = Field(ge=1)
    status: GoogleConnectionStatus

    @field_validator(
        "connection_version_id",
        "connection_id",
        "workspace_id",
        "environment_id",
        "service_principal_id",
    )
    @classmethod
    def _validate_ids(cls, value: UUID, info: ValidationInfo) -> UUID:
        return _non_nil_uuid(value, info)

    @field_validator("approved_resource_scope_references")
    @classmethod
    def _validate_resource_scopes(
        cls, values: tuple[OpaqueGoogleReference, ...]
    ) -> tuple[OpaqueGoogleReference, ...]:
        if len({value.value for value in values}) != len(values):
            raise ValueError("approved resource scope references must be unique")
        return tuple(sorted(values, key=lambda item: item.value))

    @model_validator(mode="after")
    def _validate_version_history(self) -> "GoogleConnection":
        if self.version == 1:
            if self.predecessor_version_id is not None:
                raise ValueError("connection version 1 cannot have a predecessor")
        elif self.predecessor_version_id is None:
            raise ValueError("connection versions after version 1 require a predecessor")
        if self.predecessor_version_id == self.connection_version_id:
            raise ValueError("connection version cannot be its own predecessor")
        return self


__all__ = [
    "GoogleConnection",
    "GoogleConnectionStatus",
    "OpaqueGoogleReference",
]
