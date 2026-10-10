"""Typed logical records owned by the canonical authorization directory."""

from __future__ import annotations

from enum import Enum
from uuid import UUID

from pydantic import Field, ValidationInfo, field_validator

from spine.auth.contracts import AuthenticationAlias, AuthorizationRole
from spine.domain.common import DefinitionModel


def _non_nil_uuid(value: UUID, info: ValidationInfo) -> UUID:
    if value.int == 0:
        raise ValueError(f"{info.field_name} must be a non-nil UUID")
    return value


class AuthorizationRecordStatus(str, Enum):
    """Lifecycle states retained in immutable authorization history."""

    ACTIVE = "active"
    DISABLED = "disabled"
    REVOKED = "revoked"


class AuthorizationRecordProvenance(DefinitionModel):
    """Bounded trusted provenance; provider claims and raw tokens are excluded."""

    change_id: UUID
    recorded_by: str = Field(min_length=1, max_length=255)

    @field_validator("change_id")
    @classmethod
    def _validate_change_id(cls, value: UUID, info: ValidationInfo) -> UUID:
        return _non_nil_uuid(value, info)


class CanonicalHumanIdentity(DefinitionModel):
    identity_id: UUID
    version: int = Field(ge=1)
    status: AuthorizationRecordStatus
    provenance: AuthorizationRecordProvenance

    @field_validator("identity_id")
    @classmethod
    def _validate_identity_id(cls, value: UUID, info: ValidationInfo) -> UUID:
        return _non_nil_uuid(value, info)


class AuthenticationAliasBinding(DefinitionModel):
    binding_id: UUID
    alias: AuthenticationAlias
    canonical_human_identity_id: UUID
    version: int = Field(ge=1)
    status: AuthorizationRecordStatus
    provenance: AuthorizationRecordProvenance

    @field_validator("binding_id", "canonical_human_identity_id")
    @classmethod
    def _validate_ids(cls, value: UUID, info: ValidationInfo) -> UUID:
        return _non_nil_uuid(value, info)


class WorkspaceMembership(DefinitionModel):
    membership_id: UUID
    workspace_id: UUID
    canonical_human_identity_id: UUID
    version: int = Field(ge=1)
    status: AuthorizationRecordStatus
    provenance: AuthorizationRecordProvenance

    @field_validator(
        "membership_id", "workspace_id", "canonical_human_identity_id"
    )
    @classmethod
    def _validate_ids(cls, value: UUID, info: ValidationInfo) -> UUID:
        return _non_nil_uuid(value, info)


class EnvironmentMembership(DefinitionModel):
    membership_id: UUID
    workspace_id: UUID
    environment_id: UUID
    canonical_human_identity_id: UUID
    version: int = Field(ge=1)
    status: AuthorizationRecordStatus
    provenance: AuthorizationRecordProvenance

    @field_validator(
        "membership_id",
        "workspace_id",
        "environment_id",
        "canonical_human_identity_id",
    )
    @classmethod
    def _validate_ids(cls, value: UUID, info: ValidationInfo) -> UUID:
        return _non_nil_uuid(value, info)


class EnvironmentRoleBinding(DefinitionModel):
    binding_id: UUID
    workspace_id: UUID
    environment_id: UUID
    canonical_human_identity_id: UUID
    role: AuthorizationRole
    version: int = Field(ge=1)
    status: AuthorizationRecordStatus
    provenance: AuthorizationRecordProvenance

    @field_validator(
        "binding_id",
        "workspace_id",
        "environment_id",
        "canonical_human_identity_id",
    )
    @classmethod
    def _validate_ids(cls, value: UUID, info: ValidationInfo) -> UUID:
        return _non_nil_uuid(value, info)


class AuthorizationGeneration(DefinitionModel):
    workspace_id: UUID
    environment_id: UUID
    generation: int = Field(ge=1)

    @field_validator("workspace_id", "environment_id")
    @classmethod
    def _validate_ids(cls, value: UUID, info: ValidationInfo) -> UUID:
        return _non_nil_uuid(value, info)


class EnvironmentOwnership(DefinitionModel):
    """In-memory projection of the canonical Environment owner relation."""

    workspace_id: UUID
    environment_id: UUID

    @field_validator("workspace_id", "environment_id")
    @classmethod
    def _validate_ids(cls, value: UUID, info: ValidationInfo) -> UUID:
        return _non_nil_uuid(value, info)


class AuthorizationDirectoryRecords(DefinitionModel):
    """Detached immutable record set used by deterministic directory adapters."""

    identities: tuple[CanonicalHumanIdentity, ...] = ()
    alias_bindings: tuple[AuthenticationAliasBinding, ...] = ()
    workspace_memberships: tuple[WorkspaceMembership, ...] = ()
    environment_memberships: tuple[EnvironmentMembership, ...] = ()
    role_bindings: tuple[EnvironmentRoleBinding, ...] = ()
    generations: tuple[AuthorizationGeneration, ...] = ()
    environment_owners: tuple[EnvironmentOwnership, ...] = ()


class CurrentAuthorizationSnapshot(DefinitionModel):
    """One adapter-neutral, transactionally consistent current snapshot."""

    canonical_human_identity_id: UUID
    workspace_id: UUID
    environment_id: UUID
    roles: frozenset[AuthorizationRole]
    authorization_generation: int = Field(ge=1)

    @field_validator(
        "canonical_human_identity_id", "workspace_id", "environment_id"
    )
    @classmethod
    def _validate_ids(cls, value: UUID, info: ValidationInfo) -> UUID:
        return _non_nil_uuid(value, info)


__all__ = [
    "AuthenticationAliasBinding",
    "AuthorizationDirectoryRecords",
    "AuthorizationGeneration",
    "AuthorizationRecordProvenance",
    "AuthorizationRecordStatus",
    "CanonicalHumanIdentity",
    "CurrentAuthorizationSnapshot",
    "EnvironmentMembership",
    "EnvironmentOwnership",
    "EnvironmentRoleBinding",
    "WorkspaceMembership",
]
