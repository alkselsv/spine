"""Fail-closed admission for one exact Bitrix24 CRM relationship v2 read."""

from __future__ import annotations

from enum import Enum
from typing import NoReturn, Protocol
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from spine.application.connections.bitrix24_oauth import (
    Bitrix24GrantAdmission,
    Bitrix24OAuthError,
    Bitrix24ReadAuthorization,
    Bitrix24ReadFenceRepository,
    Bitrix24ReadMethod,
)
from spine.application.crm_relationship_v2_contracts import (
    Bitrix24CrmRelationshipV2ProviderPort,
    Bitrix24CrmRelationshipV2ReadCommand,
    Bitrix24CrmRelationshipV2ReadResult,
)
from spine.domain.common import DefinitionModel
from spine.domain.connections.bitrix24_oauth import Bitrix24Scope
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapabilityKind,
    ProviderConnection,
    ProviderConnectionStatus,
    ProviderKind,
)
from spine.domain.sources.crm_relationship_v2 import CrmRelationshipMappingProfile

_REQUIRED_CAPABILITIES = (
    ProviderCapabilityKind.ENUMERATION,
    ProviderCapabilityKind.METADATA_READ,
    ProviderCapabilityKind.BINARY_READ,
    ProviderCapabilityKind.STABLE_IDENTITY,
    ProviderCapabilityKind.CRM,
    ProviderCapabilityKind.STRUCTURED_READ,
)


class CrmRelationshipV2DenialReason(str, Enum):
    HUMAN_AUTHORITY_UNAVAILABLE = "human_authority_unavailable"
    SERVICE_PRINCIPAL_SUBSTITUTION = "service_principal_substitution"
    CONNECTION_UNAVAILABLE = "connection_unavailable"
    GRANT_UNAVAILABLE = "grant_unavailable"
    SCOPE_UNAPPROVED = "scope_unapproved"
    PROFILE_UNAVAILABLE = "profile_unavailable"
    PROFILE_AMBIGUOUS = "profile_ambiguous"
    AUTHORITY_SUPERSEDED = "authority_superseded"


class CrmRelationshipV2AdmissionDenied(PermissionError):
    """Typed denial whose text never contains tenant or provider values."""

    def __init__(self, reason: CrmRelationshipV2DenialReason) -> None:
        self.reason = reason
        super().__init__("CRM relationship read denied.")


class CrmRelationshipV2ReadRequest(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    environment_id: UUID
    acting_human_id: UUID | None
    service_principal_id: UUID | None = None
    connection_id: UUID
    connection_version_id: UUID
    authorization_generation: int = Field(ge=1)
    configuration_generation: int = Field(ge=1)
    resource_scope_reference: OpaqueProviderReference
    provider_instance: str = Field(min_length=1, max_length=128)
    activity_namespace: str = Field(min_length=1, max_length=128)
    activity_type_id: str = Field(min_length=1, max_length=128)
    activity_id: str = Field(min_length=1, max_length=256)
    raw_entity_type_id: str = Field(min_length=1, max_length=128)
    entity_namespace: str = Field(min_length=1, max_length=128)
    entity_id: str = Field(min_length=1, max_length=256)
    mapping_profile_id: UUID
    mapping_profile_version: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_ids(self) -> "CrmRelationshipV2ReadRequest":
        required = (
            self.workspace_id,
            self.environment_id,
            self.connection_id,
            self.connection_version_id,
            self.mapping_profile_id,
        )
        if any(value.int == 0 for value in required):
            raise ValueError("CRM relationship read IDs must be non-zero")
        if self.acting_human_id is not None and self.acting_human_id.int == 0:
            raise ValueError("acting_human_id must be non-zero when present")
        if self.service_principal_id is not None and self.service_principal_id.int == 0:
            raise ValueError("service_principal_id must be non-zero when present")
        return self


class CurrentCrmRelationshipHumanAuthority(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    acting_human_id: UUID
    workspace_id: UUID
    environment_id: UUID
    authorization_generation: int = Field(ge=1)
    allowed: bool


class CrmRelationshipV2ReadApproval(DefinitionModel):
    """Current exact entity/resource/flow approval and its trusted references."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    environment_id: UUID
    acting_human_id: UUID
    human_authorization_generation: int = Field(ge=1)
    connection_id: UUID
    connection_version_id: UUID
    resource_scope_reference: OpaqueProviderReference
    provider_instance: str = Field(min_length=1, max_length=128)
    provider_acting_user_id: str = Field(min_length=1, max_length=255)
    approved_connection_entity_type: str = Field(min_length=1, max_length=128)
    activity_namespace: str = Field(min_length=1, max_length=128)
    activity_type_id: str = Field(min_length=1, max_length=128)
    activity_id: str = Field(min_length=1, max_length=256)
    raw_entity_type_id: str = Field(min_length=1, max_length=128)
    entity_namespace: str = Field(min_length=1, max_length=128)
    entity_id: str = Field(min_length=1, max_length=256)
    mapping_profile_id: UUID
    mapping_profile_version: int = Field(ge=1)
    connection_approval_id: UUID
    qualification_reference: UUID
    approval_reference: UUID
    approved_read_methods: tuple[str, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_approval(self) -> "CrmRelationshipV2ReadApproval":
        ids = (
            self.workspace_id,
            self.environment_id,
            self.acting_human_id,
            self.connection_id,
            self.connection_version_id,
            self.mapping_profile_id,
            self.connection_approval_id,
            self.qualification_reference,
            self.approval_reference,
        )
        if any(value.int == 0 for value in ids):
            raise ValueError("CRM relationship approval IDs must be non-zero")
        if len(set(self.approved_read_methods)) != len(self.approved_read_methods):
            raise ValueError("approved read methods must be unique")
        return self


class CrmRelationshipHumanAuthorityPort(Protocol):
    async def resolve_current(
        self, *, acting_human_id: UUID, workspace_id: UUID, environment_id: UUID
    ) -> CurrentCrmRelationshipHumanAuthority | None: ...


class CrmRelationshipConnectionAdmissionPort(Protocol):
    async def resolve_current(
        self, requested: CrmRelationshipV2ReadRequest
    ) -> ProviderConnection | None: ...


class CrmRelationshipScopeApprovalPort(Protocol):
    async def resolve_current(
        self,
        requested: CrmRelationshipV2ReadRequest,
        admitted_connection: ProviderConnection,
    ) -> CrmRelationshipV2ReadApproval | None: ...


class CrmRelationshipMappingProfilePort(Protocol):
    async def find_current(
        self, requested: CrmRelationshipV2ReadRequest
    ) -> tuple[CrmRelationshipMappingProfile, ...]: ...


class CrmRelationshipV2AdmissionService:
    def __init__(
        self,
        *,
        human_authority: CrmRelationshipHumanAuthorityPort,
        connection_admission: CrmRelationshipConnectionAdmissionPort,
        grant_admission: Bitrix24GrantAdmission,
        grant_fence: Bitrix24ReadFenceRepository,
        scope_approval: CrmRelationshipScopeApprovalPort,
        profiles: CrmRelationshipMappingProfilePort,
        provider: Bitrix24CrmRelationshipV2ProviderPort,
    ) -> None:
        self._human_authority = human_authority
        self._connection_admission = connection_admission
        self._grant_admission = grant_admission
        self._grant_fence = grant_fence
        self._scope_approval = scope_approval
        self._profiles = profiles
        self._provider = provider

    async def read(
        self, requested: CrmRelationshipV2ReadRequest
    ) -> Bitrix24CrmRelationshipV2ReadResult:
        acting_human_id = requested.acting_human_id
        if acting_human_id is None:
            self._deny(CrmRelationshipV2DenialReason.HUMAN_AUTHORITY_UNAVAILABLE)
        if requested.service_principal_id is not None:
            self._deny(CrmRelationshipV2DenialReason.SERVICE_PRINCIPAL_SUBSTITUTION)

        human = await self._resolve_human(requested, acting_human_id)
        connection = await self._resolve_connection(requested, acting_human_id)
        approval = await self._resolve_approval(requested, connection, human)
        profile = await self._resolve_profile(requested, connection, approval)
        try:
            pinned = await self._grant_admission.resolve(
                connection,
                Bitrix24ReadAuthorization(
                    method=Bitrix24ReadMethod.CRM_MATERIAL_READ,
                    required_scope=Bitrix24Scope.CRM,
                ),
            )
        except Bitrix24OAuthError:
            self._deny(CrmRelationshipV2DenialReason.GRANT_UNAVAILABLE)
        if (
            pinned.acting_user_id != approval.provider_acting_user_id
            or not await self._grant_fence.is_current(connection, pinned)
        ):
            self._deny(CrmRelationshipV2DenialReason.GRANT_UNAVAILABLE)

        result = await self._provider.read(
            Bitrix24CrmRelationshipV2ReadCommand(
                workspace_id=requested.workspace_id,
                environment_id=requested.environment_id,
                acting_human_id=acting_human_id,
                human_authorization_generation=human.authorization_generation,
                connection_id=requested.connection_id,
                connection_version_id=requested.connection_version_id,
                authorization_generation=requested.authorization_generation,
                configuration_generation=requested.configuration_generation,
                resource_scope_reference=requested.resource_scope_reference,
                provider_instance=requested.provider_instance,
                provider_acting_user_id=approval.provider_acting_user_id,
                activity_namespace=requested.activity_namespace,
                activity_type_id=requested.activity_type_id,
                activity_id=requested.activity_id,
                raw_entity_type_id=requested.raw_entity_type_id,
                entity_namespace=requested.entity_namespace,
                entity_id=requested.entity_id,
                mapping_profile=profile,
                pinned_grant=pinned,
            )
        )

        if not await self._grant_fence.is_current(connection, pinned):
            self._deny(CrmRelationshipV2DenialReason.AUTHORITY_SUPERSEDED)
        try:
            current_human = await self._resolve_human(requested, acting_human_id)
            current_connection = await self._resolve_connection(
                requested, acting_human_id
            )
            current_approval = await self._resolve_approval(
                requested, current_connection, current_human
            )
            current_profile = await self._resolve_profile(
                requested, current_connection, current_approval
            )
        except CrmRelationshipV2AdmissionDenied:
            self._deny(CrmRelationshipV2DenialReason.AUTHORITY_SUPERSEDED)
        if (
            current_human != human
            or current_connection != connection
            or current_approval != approval
            or current_profile != profile
        ):
            self._deny(CrmRelationshipV2DenialReason.AUTHORITY_SUPERSEDED)
        return result

    async def _resolve_human(
        self, requested: CrmRelationshipV2ReadRequest, acting_human_id: UUID
    ) -> CurrentCrmRelationshipHumanAuthority:
        current = await self._human_authority.resolve_current(
            acting_human_id=acting_human_id,
            workspace_id=requested.workspace_id,
            environment_id=requested.environment_id,
        )
        if (
            current is None
            or not current.allowed
            or (
                current.acting_human_id,
                current.workspace_id,
                current.environment_id,
            )
            != (
                acting_human_id,
                requested.workspace_id,
                requested.environment_id,
            )
        ):
            self._deny(CrmRelationshipV2DenialReason.HUMAN_AUTHORITY_UNAVAILABLE)
        return current

    async def _resolve_connection(
        self, requested: CrmRelationshipV2ReadRequest, acting_human_id: UUID
    ) -> ProviderConnection:
        current = await self._connection_admission.resolve_current(requested)
        if (
            current is None
            or current.service_principal_id == acting_human_id
            or not (
                current.provider_kind is ProviderKind.BITRIX24
                and current.status is ProviderConnectionStatus.ACTIVE
                and (
                    current.workspace_id,
                    current.environment_id,
                    current.connection_id,
                    current.connection_version_id,
                    current.authorization_generation,
                    current.configuration_generation,
                )
                == (
                    requested.workspace_id,
                    requested.environment_id,
                    requested.connection_id,
                    requested.connection_version_id,
                    requested.authorization_generation,
                    requested.configuration_generation,
                )
                and requested.resource_scope_reference
                in current.approved_resource_scope_references
                and all(current.supports(item) for item in _REQUIRED_CAPABILITIES)
            )
        ):
            self._deny(CrmRelationshipV2DenialReason.CONNECTION_UNAVAILABLE)
        return current

    async def _resolve_approval(
        self,
        requested: CrmRelationshipV2ReadRequest,
        connection: ProviderConnection,
        human: CurrentCrmRelationshipHumanAuthority,
    ) -> CrmRelationshipV2ReadApproval:
        current = await self._scope_approval.resolve_current(requested, connection)
        if (
            current is None
            or (
                current.workspace_id,
                current.environment_id,
                current.acting_human_id,
                current.human_authorization_generation,
                current.connection_id,
                current.connection_version_id,
                current.resource_scope_reference,
                current.provider_instance,
                current.activity_namespace,
                current.activity_type_id,
                current.activity_id,
                current.raw_entity_type_id,
                current.entity_namespace,
                current.entity_id,
                current.mapping_profile_id,
                current.mapping_profile_version,
                current.connection_approval_id,
            )
            != (
                requested.workspace_id,
                requested.environment_id,
                requested.acting_human_id,
                human.authorization_generation,
                requested.connection_id,
                requested.connection_version_id,
                requested.resource_scope_reference,
                requested.provider_instance,
                requested.activity_namespace,
                requested.activity_type_id,
                requested.activity_id,
                requested.raw_entity_type_id,
                requested.entity_namespace,
                requested.entity_id,
                requested.mapping_profile_id,
                requested.mapping_profile_version,
                connection.approval_id,
            )
            or current.approved_connection_entity_type
            not in connection.approved_crm_entity_types
        ):
            self._deny(CrmRelationshipV2DenialReason.SCOPE_UNAPPROVED)
        return current

    async def _resolve_profile(
        self,
        requested: CrmRelationshipV2ReadRequest,
        connection: ProviderConnection,
        approval: CrmRelationshipV2ReadApproval,
    ) -> CrmRelationshipMappingProfile:
        candidates = await self._profiles.find_current(requested)
        if not candidates:
            self._deny(CrmRelationshipV2DenialReason.PROFILE_UNAVAILABLE)
        if len(candidates) != 1:
            self._deny(CrmRelationshipV2DenialReason.PROFILE_AMBIGUOUS)
        profile = candidates[0]
        if not (
            profile.profile_id == requested.mapping_profile_id
            and profile.profile_version == requested.mapping_profile_version
            and profile.workspace_id == requested.workspace_id
            and profile.environment_id == requested.environment_id
            and profile.provider_instance == requested.provider_instance
            and profile.connection_id == requested.connection_id
            and profile.connection_approval_id == connection.approval_id
            and profile.qualification_reference == approval.qualification_reference
            and profile.approval_reference == approval.approval_reference
            and profile.activity_namespace == requested.activity_namespace
            and requested.activity_type_id in profile.activity_type_ids
            and requested.raw_entity_type_id in profile.raw_entity_type_ids
            and profile.entity_namespace == requested.entity_namespace
            and len(profile.read_methods) == len(approval.approved_read_methods)
            and frozenset(profile.read_methods)
            == frozenset(approval.approved_read_methods)
        ):
            self._deny(CrmRelationshipV2DenialReason.PROFILE_UNAVAILABLE)
        return profile

    @staticmethod
    def _deny(reason: CrmRelationshipV2DenialReason) -> NoReturn:
        raise CrmRelationshipV2AdmissionDenied(reason)


__all__ = [
    "CrmRelationshipConnectionAdmissionPort",
    "CrmRelationshipHumanAuthorityPort",
    "CrmRelationshipMappingProfilePort",
    "CrmRelationshipScopeApprovalPort",
    "CrmRelationshipV2AdmissionDenied",
    "CrmRelationshipV2AdmissionService",
    "CrmRelationshipV2DenialReason",
    "CrmRelationshipV2ReadApproval",
    "CrmRelationshipV2ReadRequest",
    "CurrentCrmRelationshipHumanAuthority",
]
