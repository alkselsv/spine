"""Provider-neutral, immutable external connection contracts."""

from __future__ import annotations

from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import Field, ValidationInfo, field_validator, model_validator

from spine.domain.common import DefinitionModel
from spine.domain.connections.google import GoogleConnection


def _non_nil_uuid(value: UUID, info: ValidationInfo) -> UUID:
    if value.int == 0:
        raise ValueError(f"{info.field_name} must be a non-nil UUID")
    return value


class ProviderKind(str, Enum):
    """Provider families approved by the bounded R2 source specification."""

    GOOGLE = "google"
    YANDEX_DISK = "yandex_disk"
    BITRIX24 = "bitrix24"
    MOUNTED_FILESYSTEM = "mounted_filesystem"


class OpaqueProviderReference(DefinitionModel):
    """Reference to governed state; never credential or grant material itself."""

    value: UUID

    @field_validator("value")
    @classmethod
    def _validate_value(cls, value: UUID, info: ValidationInfo) -> UUID:
        return _non_nil_uuid(value, info)


class ProviderConnectionStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


class ProviderConnectionHealth(str, Enum):
    """Disclosure-safe operator health for one exact connection version."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    REVOKED = "revoked"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class ProviderDiagnosticCode(str, Enum):
    """Allowlisted diagnostics that cannot carry provider-controlled text."""

    CONNECTION_READY = "connection_ready"
    PROVIDER_DEGRADED = "provider_degraded"
    AUTHORIZATION_REVOKED = "authorization_revoked"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class ProviderCapabilityKind(str, Enum):
    ENUMERATION = "enumeration"
    METADATA_READ = "metadata_read"
    BINARY_READ = "binary_read"
    TABULAR_READ = "tabular_read"
    STRUCTURED_READ = "structured_read"
    STABLE_IDENTITY = "stable_identity"
    INCREMENTAL_CHANGE_DISCOVERY = "incremental_change_discovery"
    FULL_RECONCILIATION = "full_reconciliation"
    LIFECYCLE_EVIDENCE = "lifecycle_evidence"
    HEALTH_DIAGNOSTICS = "health_diagnostics"
    CRM = "crm"


class ProviderCapabilityStatus(str, Enum):
    """Whether a capability is safe to use as a provider guarantee."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNQUALIFIED = "unqualified"


class ProviderCapability(DefinitionModel):
    """One explicit capability statement for an immutable connection version."""

    schema_version: Literal["provider-capability:v2"] = "provider-capability:v2"
    kind: ProviderCapabilityKind
    status: ProviderCapabilityStatus
    qualification_reference: OpaqueProviderReference | None = None

    @model_validator(mode="after")
    def validate_qualification(self) -> "ProviderCapability":
        if self.status is ProviderCapabilityStatus.SUPPORTED:
            if self.qualification_reference is None:
                raise ValueError("supported capability requires a qualification reference")
        elif self.qualification_reference is not None:
            raise ValueError(
                "unsupported or unqualified capability cannot carry a qualification reference"
            )
        return self


class ProviderConnection(DefinitionModel):
    """One refs-only, tenant-scoped provider connection version."""

    schema_version: Literal["provider-connection:v2"] = "provider-connection:v2"
    connection_version_id: UUID
    connection_id: UUID
    approval_id: UUID
    version: int = Field(ge=1)
    predecessor_version_id: UUID | None = None
    workspace_id: UUID
    environment_id: UUID
    service_principal_id: UUID
    provider_kind: ProviderKind
    credential_reference: OpaqueProviderReference
    grant_reference: OpaqueProviderReference
    approved_resource_scope_references: tuple[OpaqueProviderReference, ...] = Field(
        min_length=1
    )
    approved_crm_entity_types: tuple[str, ...] = ()
    authorization_generation: int = Field(ge=1)
    configuration_generation: int = Field(ge=1)
    status: ProviderConnectionStatus
    capabilities: tuple[ProviderCapability, ...] = ()

    @field_validator(
        "connection_version_id",
        "connection_id",
        "approval_id",
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
        cls, values: tuple[OpaqueProviderReference, ...]
    ) -> tuple[OpaqueProviderReference, ...]:
        if len({item.value for item in values}) != len(values):
            raise ValueError("approved resource scope references must be unique")
        return tuple(sorted(values, key=lambda item: item.value.int))

    @field_validator("capabilities")
    @classmethod
    def _validate_capabilities(
        cls, values: tuple[ProviderCapability, ...]
    ) -> tuple[ProviderCapability, ...]:
        if len({item.kind for item in values}) != len(values):
            raise ValueError("provider capability kinds must be unique")
        return tuple(sorted(values, key=lambda item: item.kind.value))

    @field_validator("approved_crm_entity_types")
    @classmethod
    def _validate_crm_entity_types(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip().lower() for value in values)
        if any(not value for value in normalized):
            raise ValueError("approved CRM entity types must not be empty")
        if len(set(normalized)) != len(normalized):
            raise ValueError("approved CRM entity types must be unique")
        return tuple(sorted(normalized))

    @model_validator(mode="after")
    def _validate_version_history(self) -> "ProviderConnection":
        if self.version == 1:
            if self.predecessor_version_id is not None:
                raise ValueError("connection version 1 cannot have a predecessor")
        elif self.predecessor_version_id is None:
            raise ValueError("connection versions after version 1 require a predecessor")
        if self.predecessor_version_id == self.connection_version_id:
            raise ValueError("connection version cannot be its own predecessor")
        return self

    def capability(self, kind: ProviderCapabilityKind) -> ProviderCapability | None:
        return next((item for item in self.capabilities if item.kind is kind), None)

    def supports(self, kind: ProviderCapabilityKind) -> bool:
        capability = self.capability(kind)
        return (
            capability is not None
            and capability.status is ProviderCapabilityStatus.SUPPORTED
        )


class ProviderConnectionApproval(DefinitionModel):
    """Server-owned approval that configuration may reference but cannot create."""

    schema_version: Literal["provider-connection-approval:v2"] = (
        "provider-connection-approval:v2"
    )
    approval_id: UUID
    workspace_id: UUID
    environment_id: UUID
    service_principal_id: UUID
    provider_kind: ProviderKind
    credential_reference: OpaqueProviderReference
    grant_reference: OpaqueProviderReference
    approved_resource_scope_references: tuple[OpaqueProviderReference, ...] = Field(
        min_length=1
    )
    approved_crm_entity_types: tuple[str, ...] = ()
    authorization_generation: int = Field(ge=1)
    configuration_generation: int = Field(ge=1)
    status: ProviderConnectionStatus

    @field_validator(
        "approval_id",
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
        cls, values: tuple[OpaqueProviderReference, ...]
    ) -> tuple[OpaqueProviderReference, ...]:
        if len({item.value for item in values}) != len(values):
            raise ValueError("approved resource scope references must be unique")
        return tuple(sorted(values, key=lambda item: item.value.int))

    @field_validator("approved_crm_entity_types")
    @classmethod
    def _validate_crm_entity_types(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip().lower() for value in values)
        if any(not value for value in normalized):
            raise ValueError("approved CRM entity types must not be empty")
        if len(set(normalized)) != len(normalized):
            raise ValueError("approved CRM entity types must be unique")
        return tuple(sorted(normalized))


def google_v1_connection_to_v2(
    connection: GoogleConnection,
    *,
    capabilities: tuple[ProviderCapability, ...],
) -> ProviderConnection:
    """Expand a Google v1 definition into the provider-neutral v2 form."""

    return ProviderConnection(
        connection_version_id=connection.connection_version_id,
        connection_id=connection.connection_id,
        approval_id=connection.approval_id,
        version=connection.version,
        predecessor_version_id=connection.predecessor_version_id,
        workspace_id=connection.workspace_id,
        environment_id=connection.environment_id,
        service_principal_id=connection.service_principal_id,
        provider_kind=ProviderKind.GOOGLE,
        credential_reference=OpaqueProviderReference(
            value=connection.credential_reference.value
        ),
        grant_reference=OpaqueProviderReference(value=connection.grant_reference.value),
        approved_resource_scope_references=tuple(
            OpaqueProviderReference(value=item.value)
            for item in connection.approved_resource_scope_references
        ),
        authorization_generation=connection.authorization_generation,
        configuration_generation=connection.configuration_generation,
        status=ProviderConnectionStatus(connection.status.value),
        capabilities=capabilities,
    )


__all__ = [
    "OpaqueProviderReference",
    "ProviderCapability",
    "ProviderCapabilityKind",
    "ProviderCapabilityStatus",
    "ProviderConnection",
    "ProviderConnectionApproval",
    "ProviderConnectionHealth",
    "ProviderConnectionStatus",
    "ProviderDiagnosticCode",
    "ProviderKind",
    "google_v1_connection_to_v2",
]
