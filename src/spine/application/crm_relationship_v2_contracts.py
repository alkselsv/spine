"""Application-owned contract for an admitted Bitrix24 CRM v2 provider read."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal, Protocol
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from spine.application.connections.bitrix24_oauth import Bitrix24PinnedGrant
from spine.domain.common import DefinitionModel
from spine.domain.connections.provider import OpaqueProviderReference
from spine.domain.sources.crm_relationship_v2 import CrmRelationshipMappingProfile


class CrmRelationshipBindingOutcome(str, Enum):
    POSITIVE_OBSERVED = "POSITIVE_OBSERVED"
    NOT_VERIFIED = "NOT_VERIFIED"
    INACCESSIBLE = "INACCESSIBLE"
    AUTHORITY_UNAVAILABLE = "AUTHORITY_UNAVAILABLE"


class CrmRelationshipBindingOperationsProfile(DefinitionModel):
    """Caller supplied synthetic bounds for one binding-list observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str = Field(min_length=1, max_length=128)
    profile_version: str = Field(min_length=1, max_length=64)
    profile_kind: Literal["synthetic"] = "synthetic"
    page_size: int = Field(ge=1)
    max_pages: int = Field(ge=1, le=100)
    max_response_bytes: int = Field(ge=1)
    max_attempts: int = Field(ge=1)


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
    operations_profile: CrmRelationshipBindingOperationsProfile
    pinned_grant: Bitrix24PinnedGrant


class Bitrix24CrmRelationshipV2ReadResult(DefinitionModel):
    """Disclosure-safe evidence returned by the later qualified provider adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    binding_evidence_reference: str = Field(min_length=1, max_length=255)
    binding_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    positive_pair_observed: bool
    outcome: CrmRelationshipBindingOutcome
    binding_method: Literal["crm.activity.binding.list"]
    raw_entity_type_id: str = Field(min_length=1, max_length=128)
    entity_id: str = Field(min_length=1, max_length=256)
    normalizer_id: str = Field(min_length=1, max_length=128)
    normalizer_version: str = Field(min_length=1, max_length=64)
    page_count: int = Field(ge=0, le=100)
    attempt_count: int = Field(ge=0)
    response_bytes: int = Field(ge=0)
    snapshot_atomic: Literal[False] = False

    @model_validator(mode="after")
    def validate_outcome(self) -> "Bitrix24CrmRelationshipV2ReadResult":
        if self.positive_pair_observed != (
            self.outcome is CrmRelationshipBindingOutcome.POSITIVE_OBSERVED
        ):
            raise ValueError("positive binding evidence requires a positive outcome")
        if self.page_count > self.attempt_count:
            raise ValueError("binding page count cannot exceed provider attempts")
        if self.positive_pair_observed and (
            self.page_count == 0 or self.attempt_count == 0
        ):
            raise ValueError("positive binding evidence requires a provider page")
        return self


class Bitrix24CrmRelationshipV2ProviderPort(Protocol):
    async def read(
        self, command: Bitrix24CrmRelationshipV2ReadCommand
    ) -> Bitrix24CrmRelationshipV2ReadResult: ...


__all__ = [
    "CrmRelationshipBindingOperationsProfile",
    "CrmRelationshipBindingOutcome",
    "Bitrix24CrmRelationshipV2ProviderPort",
    "Bitrix24CrmRelationshipV2ReadCommand",
    "Bitrix24CrmRelationshipV2ReadResult",
]
