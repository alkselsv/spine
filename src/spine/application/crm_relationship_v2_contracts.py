"""Application-owned contract for an admitted Bitrix24 CRM v2 provider read."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from pydantic import ConfigDict, Field

from spine.application.connections.bitrix24_oauth import Bitrix24PinnedGrant
from spine.domain.common import DefinitionModel
from spine.domain.connections.provider import OpaqueProviderReference
from spine.domain.sources.crm_relationship_v2 import CrmRelationshipMappingProfile


@dataclass(frozen=True, slots=True)
class Bitrix24CrmRelationshipV2ReadCommand:
    """One fully admitted exact CRM activity/entity read."""

    workspace_id: UUID
    environment_id: UUID
    acting_human_id: UUID
    human_authorization_generation: int
    connection_id: UUID
    connection_version_id: UUID
    authorization_generation: int
    configuration_generation: int
    resource_scope_reference: OpaqueProviderReference
    provider_instance: str
    provider_acting_user_id: str
    activity_namespace: str
    activity_type_id: str
    activity_id: str
    raw_entity_type_id: str
    entity_namespace: str
    entity_id: str
    mapping_profile: CrmRelationshipMappingProfile
    pinned_grant: Bitrix24PinnedGrant


class Bitrix24CrmRelationshipV2ReadResult(DefinitionModel):
    """Disclosure-safe evidence returned by the later qualified provider adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    binding_evidence_reference: str = Field(min_length=1, max_length=255)
    binding_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    positive_pair_observed: bool


class Bitrix24CrmRelationshipV2ProviderPort(Protocol):
    async def read(
        self, command: Bitrix24CrmRelationshipV2ReadCommand
    ) -> Bitrix24CrmRelationshipV2ReadResult: ...


__all__ = [
    "Bitrix24CrmRelationshipV2ProviderPort",
    "Bitrix24CrmRelationshipV2ReadCommand",
    "Bitrix24CrmRelationshipV2ReadResult",
]
