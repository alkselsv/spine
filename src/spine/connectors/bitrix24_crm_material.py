"""Read-only Bitrix24 CRM activity attachments as exact relationship evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, StrictInt, field_validator, model_validator

from spine.connectors.observations import ConnectorCoverage
from spine.connectors.observations_v2 import (
    ExternalIdentityAssurance,
    NormalizedExternalObservationV2,
    ProviderExternalIdentity,
    ProviderStructuredObjectKey,
    StructuredObservationRepresentation,
)
from spine.domain.common import DefinitionModel
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapability,
    ProviderCapabilityKind,
    ProviderCapabilityStatus,
    ProviderConnection,
    ProviderKind,
)
from spine.domain.sources import Bitrix24CrmMaterialClaim

BITRIX24_CRM_EXTERNAL_GENERATION = "bitrix24-crm-activity-attachment:v1"
BITRIX24_CRM_REQUIRED_CAPABILITIES = (
    ProviderCapabilityKind.ENUMERATION,
    ProviderCapabilityKind.METADATA_READ,
    ProviderCapabilityKind.BINARY_READ,
    ProviderCapabilityKind.STABLE_IDENTITY,
    ProviderCapabilityKind.CRM,
    ProviderCapabilityKind.STRUCTURED_READ,
)


class Bitrix24CrmMaterialReadError(RuntimeError):
    """Safe base error for one bounded CRM relationship read."""


class Bitrix24CrmMaterialPermissionLostError(Bitrix24CrmMaterialReadError):
    def __init__(self) -> None:
        super().__init__("Bitrix24 CRM relationship access was lost.")


class Bitrix24CrmMaterialAuthorizationRevokedError(Bitrix24CrmMaterialReadError):
    def __init__(self) -> None:
        super().__init__("Bitrix24 CRM authorization was revoked.")


class Bitrix24CrmMaterialNotFoundError(Bitrix24CrmMaterialReadError):
    def __init__(self) -> None:
        super().__init__("Bitrix24 CRM activity attachment was not found.")


class Bitrix24CrmMaterialContractError(Bitrix24CrmMaterialReadError):
    def __init__(self) -> None:
        super().__init__("Bitrix24 CRM returned an invalid relationship response.")


class Bitrix24CrmActivity(DefinitionModel):
    """Allowlisted identity fields from a CRM activity response."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    activity_id: str = Field(alias="ID", min_length=1, max_length=128)
    entity_type: str = Field(alias="OWNER_TYPE_ID", min_length=1, max_length=128)
    entity_id: str = Field(alias="OWNER_ID", min_length=1, max_length=128)
    updated_at: datetime = Field(alias="LAST_UPDATED")
    activity_revision: str = Field(
        alias="ACTIVITY_REVISION", min_length=1, max_length=128
    )

    @field_validator("activity_id", "entity_type", "entity_id", "activity_revision")
    @classmethod
    def _normalize_identifier(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Bitrix24 CRM identifiers must not be empty")
        return value

    @model_validator(mode="after")
    def _validate_time(self) -> "Bitrix24CrmActivity":
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("Bitrix24 CRM activity timestamp must be timezone-aware")
        return self


class Bitrix24CrmAttachment(DefinitionModel):
    """Exact CRM-to-Disk binding returned by a qualified relationship read."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    attachment_id: str = Field(alias="ID", min_length=1, max_length=128)
    activity_id: str = Field(alias="ACTIVITY_ID", min_length=1, max_length=128)
    entity_type: str = Field(alias="OWNER_TYPE_ID", min_length=1, max_length=128)
    entity_id: str = Field(alias="OWNER_ID", min_length=1, max_length=128)
    disk_file_id: str = Field(alias="DISK_FILE_ID", min_length=1, max_length=128)
    disk_file_revision: str = Field(
        alias="DISK_FILE_REVISION", min_length=1, max_length=128
    )
    relationship_revision: str = Field(
        alias="RELATIONSHIP_REVISION", min_length=1, max_length=128
    )

    @field_validator(
        "attachment_id",
        "activity_id",
        "entity_type",
        "entity_id",
        "disk_file_id",
        "disk_file_revision",
        "relationship_revision",
    )
    @classmethod
    def _normalize_identifier(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Bitrix24 CRM relationship identifiers must not be empty")
        return value


class Bitrix24CrmMaterialReadProfile(DefinitionModel):
    max_entity_types: StrictInt = Field(default=32, ge=1, le=256)


class Bitrix24CrmMaterialReadCommand(DefinitionModel):
    """Scope and exact target supplied to the CRM relationship adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    resource_scope_reference: OpaqueProviderReference
    authorization_generation: StrictInt = Field(ge=1)
    configuration_generation: StrictInt = Field(ge=1)
    entity_type: str = Field(min_length=1, max_length=128)
    entity_id: str = Field(min_length=1, max_length=128)
    activity_id: str = Field(min_length=1, max_length=128)
    disk_file_id: str = Field(min_length=1, max_length=128)
    target_source_object_id: UUID
    target_source_revision_id: UUID
    approved_entity_types: tuple[str, ...] = Field(min_length=1, max_length=256)
    required_capabilities: tuple[ProviderCapabilityKind, ...] = (
        BITRIX24_CRM_REQUIRED_CAPABILITIES
    )
    acting_subject_id: UUID | None = None

    @field_validator("entity_type", "entity_id", "activity_id", "disk_file_id")
    @classmethod
    def _normalize_identifier(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Bitrix24 CRM target identifiers must not be empty")
        return value

    @field_validator("approved_entity_types")
    @classmethod
    def _normalize_entity_types(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip().lower() for value in values)
        if any(not value for value in normalized):
            raise ValueError("approved CRM entity types must not be empty")
        if len(set(normalized)) != len(normalized):
            raise ValueError("approved CRM entity types must be unique")
        return normalized

    @model_validator(mode="after")
    def _validate_ids(self) -> "Bitrix24CrmMaterialReadCommand":
        for name in (
            "workspace_id",
            "environment_id",
            "connection_id",
            "connection_version_id",
            "target_source_object_id",
            "target_source_revision_id",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be non-zero")
        return self


class Bitrix24CrmMaterialService(Protocol):
    async def authorize_read(
        self,
        connection: ProviderConnection,
        resource_scope_reference: OpaqueProviderReference,
        *,
        entity_type: str,
        entity_id: str,
        activity_id: str,
        disk_file_id: str,
        acting_principal_id: UUID,
    ) -> None: ...

    async def get_activity(
        self,
        connection: ProviderConnection,
        resource_scope_reference: OpaqueProviderReference,
        *,
        activity_id: str,
        acting_principal_id: UUID,
    ) -> Bitrix24CrmActivity: ...

    async def get_attachment(
        self,
        connection: ProviderConnection,
        resource_scope_reference: OpaqueProviderReference,
        *,
        activity_id: str,
        disk_file_id: str,
        acting_principal_id: UUID,
    ) -> Bitrix24CrmAttachment: ...


class Bitrix24CrmMaterialObservation(DefinitionModel):
    """Content-free claim ready for canonical association acceptance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "bitrix24-crm-material-observation:v1"
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    resource_scope_reference: UUID
    crm_entity_type: str
    crm_entity_id: str
    crm_activity_id: str
    crm_activity_revision: str
    crm_attachment_id: str
    relationship_revision: str
    target_source_object_id: UUID
    target_source_revision_id: UUID
    disk_file_id: str
    disk_file_revision: str
    binding_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime


def bitrix24_crm_capability(
    *,
    qualification_reference: OpaqueProviderReference,
    approved_entity_types: tuple[str, ...],
) -> tuple[ProviderCapability, ...]:
    """Return the conditional CRM capability declaration.

    The Disk file capabilities remain independent.  This declaration is only
    added to a connection version after the CRM relationship qualification and
    approved entity scope have been configured.
    """

    if not approved_entity_types or any(
        not value.strip() for value in approved_entity_types
    ):
        raise ValueError("supported CRM capability requires an approved entity scope")
    return (
        ProviderCapability(
            kind=ProviderCapabilityKind.CRM,
            status=ProviderCapabilityStatus.SUPPORTED,
            qualification_reference=qualification_reference,
        ),
        ProviderCapability(
            kind=ProviderCapabilityKind.STRUCTURED_READ,
            status=ProviderCapabilityStatus.SUPPORTED,
            qualification_reference=qualification_reference,
        ),
    )


def bitrix24_crm_claim_from_observation(
    observation: Bitrix24CrmMaterialObservation,
) -> Bitrix24CrmMaterialClaim:
    """Map the untrusted connector result into the immutable domain claim."""

    return Bitrix24CrmMaterialClaim(
        claim_id=uuid5(
            NAMESPACE_URL,
            f"bitrix24-crm-material:{observation.connection_id}:"
            f"{observation.crm_activity_id}:{observation.crm_attachment_id}:"
            f"{observation.binding_evidence_digest}",
        ),
        workspace_id=observation.workspace_id,
        environment_id=observation.environment_id,
        connection_id=observation.connection_id,
        connection_version_id=observation.connection_version_id,
        resource_scope_reference=observation.resource_scope_reference,
        crm_entity_type=observation.crm_entity_type,
        crm_entity_id=observation.crm_entity_id,
        crm_activity_id=observation.crm_activity_id,
        crm_activity_revision=observation.crm_activity_revision,
        crm_attachment_id=observation.crm_attachment_id,
        relationship_revision=observation.relationship_revision,
        target_source_object_id=observation.target_source_object_id,
        target_source_revision_id=observation.target_source_revision_id,
        disk_file_id=observation.disk_file_id,
        disk_file_revision=observation.disk_file_revision,
        binding_evidence_digest=observation.binding_evidence_digest,
        observed_at=observation.observed_at,
    )


def bitrix24_crm_normalized_observation(
    observation: Bitrix24CrmMaterialObservation,
    *,
    authorization_generation: int,
    configuration_generation: int,
) -> NormalizedExternalObservationV2:
    """Map qualified CRM relationship evidence to the shared connector envelope."""

    payload = observation.model_dump_json().encode()
    return NormalizedExternalObservationV2(
        workspace_id=observation.workspace_id,
        environment_id=observation.environment_id,
        connection_id=observation.connection_id,
        connection_version_id=observation.connection_version_id,
        provider_kind=ProviderKind.BITRIX24,
        resource_scope_reference=OpaqueProviderReference(
            value=observation.resource_scope_reference
        ),
        authorization_generation=authorization_generation,
        configuration_generation=configuration_generation,
        source_kind="relationship",
        external_identity=ProviderExternalIdentity(
            external_namespace="bitrix24-crm-relationship",
            external_generation=BITRIX24_CRM_EXTERNAL_GENERATION,
            external_object_id=(
                f"activity-{observation.crm_activity_id}-"
                f"attachment-{observation.crm_attachment_id}"
            ),
            provider_key=ProviderStructuredObjectKey(
                connection_id=observation.connection_id,
                object_type="crm_activity_attachment",
                provider_object_id=(
                    f"{observation.crm_activity_id}:" f"{observation.crm_attachment_id}"
                ),
            ),
            assurance=ExternalIdentityAssurance.PROVIDER_STABLE,
            assurance_evidence_reference="bitrix24-crm-binding:v1",
        ),
        representation=StructuredObservationRepresentation(
            original_bytes=payload,
            media_type="application/json",
            extraction_profile="bitrix24-crm-material:v1",
            locator_contract_version="locator:bitrix24-crm-binding:v1",
        ),
        coverage=ConnectorCoverage.COMPLETE,
        producer_reference="bitrix24-crm-material-adapter",
        event_identity=f"bitrix24-crm:{observation.binding_evidence_digest[:48]}",
        event_digest=hashlib.sha256(payload).hexdigest(),
        observer_service="connector-worker",
        received_at=observation.observed_at,
        observed_at=observation.observed_at,
    )


class Bitrix24CrmMaterialAdapter:
    def __init__(
        self,
        *,
        service: Bitrix24CrmMaterialService,
        profile: Bitrix24CrmMaterialReadProfile,
        clock: Callable[[], datetime],
    ) -> None:
        self._service = service
        self._profile = profile
        self._clock = clock

    async def read(
        self,
        connection: ProviderConnection,
        command: Bitrix24CrmMaterialReadCommand,
    ) -> Bitrix24CrmMaterialObservation:
        if len(command.approved_entity_types) > self._profile.max_entity_types:
            raise Bitrix24CrmMaterialContractError()
        self._validate_admission(connection, command)
        acting_principal_id = (
            command.acting_subject_id or connection.service_principal_id
        )
        await self._service.authorize_read(
            connection,
            command.resource_scope_reference,
            entity_type=command.entity_type,
            entity_id=command.entity_id,
            activity_id=command.activity_id,
            disk_file_id=command.disk_file_id,
            acting_principal_id=acting_principal_id,
        )
        activity = await self._service.get_activity(
            connection,
            command.resource_scope_reference,
            activity_id=command.activity_id,
            acting_principal_id=acting_principal_id,
        )
        if (
            activity.activity_id != command.activity_id
            or activity.entity_type.lower() != command.entity_type.lower()
            or activity.entity_id != command.entity_id
        ):
            raise Bitrix24CrmMaterialNotFoundError()
        attachment = await self._service.get_attachment(
            connection,
            command.resource_scope_reference,
            activity_id=command.activity_id,
            disk_file_id=command.disk_file_id,
            acting_principal_id=acting_principal_id,
        )
        if (
            attachment.activity_id != activity.activity_id
            or attachment.entity_type.lower() != activity.entity_type.lower()
            or attachment.entity_id != activity.entity_id
            or attachment.disk_file_id != command.disk_file_id
        ):
            raise Bitrix24CrmMaterialNotFoundError()
        observed_at = self._clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("Bitrix24 CRM reader clock must be timezone-aware")
        digest = hashlib.sha256(
            json.dumps(
                {
                    "activity_id": activity.activity_id,
                    "activity_revision": activity.activity_revision,
                    "entity_id": activity.entity_id,
                    "entity_type": activity.entity_type,
                    "attachment_id": attachment.attachment_id,
                    "disk_file_id": attachment.disk_file_id,
                    "disk_file_revision": attachment.disk_file_revision,
                    "relationship_revision": attachment.relationship_revision,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return Bitrix24CrmMaterialObservation(
            workspace_id=connection.workspace_id,
            environment_id=connection.environment_id,
            connection_id=connection.connection_id,
            connection_version_id=connection.connection_version_id,
            resource_scope_reference=command.resource_scope_reference.value,
            crm_entity_type=activity.entity_type,
            crm_entity_id=activity.entity_id,
            crm_activity_id=activity.activity_id,
            crm_activity_revision=activity.activity_revision,
            crm_attachment_id=attachment.attachment_id,
            relationship_revision=attachment.relationship_revision,
            target_source_object_id=command.target_source_object_id,
            target_source_revision_id=command.target_source_revision_id,
            disk_file_id=attachment.disk_file_id,
            disk_file_revision=attachment.disk_file_revision,
            binding_evidence_digest=digest,
            observed_at=observed_at,
        )

    @staticmethod
    def _validate_admission(
        connection: ProviderConnection,
        command: Bitrix24CrmMaterialReadCommand,
    ) -> None:
        if connection.provider_kind is not ProviderKind.BITRIX24:
            raise Bitrix24CrmMaterialContractError()
        if connection.status.value != "active":
            raise Bitrix24CrmMaterialAuthorizationRevokedError()
        if (
            connection.workspace_id != command.workspace_id
            or connection.environment_id != command.environment_id
            or connection.connection_id != command.connection_id
            or connection.connection_version_id != command.connection_version_id
            or connection.authorization_generation != command.authorization_generation
            or connection.configuration_generation != command.configuration_generation
        ):
            raise ValueError(
                "Bitrix24 CRM read is outside the trusted connection scope"
            )
        if (
            command.resource_scope_reference
            not in connection.approved_resource_scope_references
        ):
            raise ValueError("Bitrix24 CRM resource scope is not approved")
        if not connection.approved_crm_entity_types:
            raise ValueError("Bitrix24 CRM entity scope is not configured")
        if set(command.approved_entity_types) != set(
            connection.approved_crm_entity_types
        ):
            raise ValueError(
                "Bitrix24 CRM approved entity scope does not match the connection"
            )
        if command.entity_type.lower() not in connection.approved_crm_entity_types:
            raise ValueError(
                "Bitrix24 CRM entity type is not in the approved entity scope"
            )
        if tuple(command.required_capabilities) != BITRIX24_CRM_REQUIRED_CAPABILITIES:
            raise Bitrix24CrmMaterialContractError()
        if any(
            not connection.supports(capability)
            for capability in command.required_capabilities
        ):
            raise Bitrix24CrmMaterialContractError()


__all__ = [
    "BITRIX24_CRM_EXTERNAL_GENERATION",
    "BITRIX24_CRM_REQUIRED_CAPABILITIES",
    "Bitrix24CrmActivity",
    "Bitrix24CrmAttachment",
    "Bitrix24CrmMaterialAdapter",
    "Bitrix24CrmMaterialAuthorizationRevokedError",
    "Bitrix24CrmMaterialContractError",
    "Bitrix24CrmMaterialNotFoundError",
    "Bitrix24CrmMaterialObservation",
    "Bitrix24CrmMaterialPermissionLostError",
    "Bitrix24CrmMaterialReadCommand",
    "Bitrix24CrmMaterialReadError",
    "Bitrix24CrmMaterialReadProfile",
    "Bitrix24CrmMaterialService",
    "bitrix24_crm_capability",
    "bitrix24_crm_claim_from_observation",
    "bitrix24_crm_normalized_observation",
]
