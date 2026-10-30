"""Immutable, references-only Bitrix24 OAuth authority contracts."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

from spine.domain.common import ValidatedDefinitionModel
from spine.domain.connections.provider import OpaqueProviderReference, ProviderKind

BoundedProviderIdentity = Annotated[
    str,
    StringConstraints(
        strip_whitespace=False,
        min_length=1,
        max_length=255,
        pattern=r"^[^\s\x00-\x1f\x7f]+$",
    ),
]


def _non_nil_uuid(value: UUID, info: ValidationInfo) -> UUID:
    if value.int == 0:
        raise ValueError(f"{info.field_name} must be a non-nil UUID")
    return value


class Bitrix24GrantState(str, Enum):
    """Canonical authorization state; it is not provider connection health."""

    READY = "ready"
    REFRESH_CLAIMED = "refresh_claimed"
    UNCERTAIN = "uncertain"
    REAUTHORIZATION_REQUIRED = "reauthorization_required"
    CONTAINED = "contained"


class Bitrix24Scope(str, Enum):
    """Closed provider scopes approved for the offline read-only integration."""

    DISK = "disk"
    CRM = "crm"


class Bitrix24GrantBinding(ValidatedDefinitionModel):
    """One immutable exact provider identity bound to a connection version."""

    schema_version: Literal["bitrix24-grant-binding:v1"] = "bitrix24-grant-binding:v1"
    binding_id: UUID
    workspace_id: UUID
    environment_id: UUID
    approval_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    provider_kind: Literal[ProviderKind.BITRIX24] = ProviderKind.BITRIX24
    grant_reference: OpaqueProviderReference
    portal_member_id: BoundedProviderIdentity
    application_id: BoundedProviderIdentity
    acting_user_id: BoundedProviderIdentity
    granted_scopes: tuple[Bitrix24Scope, ...] = Field(min_length=1)
    authorization_generation: int = Field(ge=1)
    configuration_generation: int = Field(ge=1)

    @field_validator(
        "binding_id",
        "workspace_id",
        "environment_id",
        "approval_id",
        "connection_id",
        "connection_version_id",
    )
    @classmethod
    def _validate_ids(cls, value: UUID, info: ValidationInfo) -> UUID:
        return _non_nil_uuid(value, info)

    @field_validator("granted_scopes")
    @classmethod
    def _validate_scopes(
        cls, values: tuple[Bitrix24Scope, ...]
    ) -> tuple[Bitrix24Scope, ...]:
        if len(set(values)) != len(values):
            raise ValueError("granted_scopes must be unique")
        if values != (Bitrix24Scope.DISK,):
            raise ValueError("offline Bitrix24 authority permits only the disk scope")
        return values


class Bitrix24GrantAuthority(ValidatedDefinitionModel):
    """Canonical reference-only snapshot selecting one complete token pair."""

    schema_version: Literal["bitrix24-grant-authority:v1"] = (
        "bitrix24-grant-authority:v1"
    )
    binding: Bitrix24GrantBinding
    active_pair_reference: OpaqueProviderReference
    token_epoch: int = Field(ge=1)
    security_fence: int = Field(ge=0)
    state: Bitrix24GrantState

    @model_validator(mode="after")
    def _validate_fenced_state(self) -> "Bitrix24GrantAuthority":
        if self.state is Bitrix24GrantState.CONTAINED and self.security_fence < 1:
            raise ValueError("contained grant authority requires a security fence")
        return self


__all__ = [
    "Bitrix24GrantAuthority",
    "Bitrix24GrantBinding",
    "Bitrix24GrantState",
    "Bitrix24Scope",
    "BoundedProviderIdentity",
]
