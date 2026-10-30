"""Immutable evidence for a Bitrix24 CRM-to-Disk material relationship."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from spine.domain.common import DefinitionModel


class Bitrix24CrmMaterialState(str, Enum):
    OBSERVED = "observed"
    ACCEPTED = "accepted"
    RELATION_REVOKED = "relation_revoked"
    SOURCE_REVOKED = "source_revoked"
    INACCESSIBLE = "inaccessible"


class Bitrix24CrmMaterialClaim(DefinitionModel):
    """One exact CRM activity attachment observation.

    Names and free-form CRM text are intentionally absent.  A filename or a
    subject can be useful to an operator, but it cannot prove a relationship.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: UUID
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    resource_scope_reference: UUID
    crm_entity_type: str = Field(min_length=1, max_length=128)
    crm_entity_id: str = Field(min_length=1, max_length=128)
    crm_activity_id: str = Field(min_length=1, max_length=128)
    crm_activity_revision: str = Field(min_length=1, max_length=128)
    crm_attachment_id: str = Field(min_length=1, max_length=128)
    relationship_revision: str = Field(min_length=1, max_length=128)
    target_source_object_id: UUID
    target_source_revision_id: UUID
    disk_file_id: str = Field(min_length=1, max_length=128)
    disk_file_revision: str = Field(min_length=1, max_length=128)
    binding_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime

    @model_validator(mode="after")
    def validate_claim(self) -> "Bitrix24CrmMaterialClaim":
        for name in (
            "claim_id",
            "workspace_id",
            "environment_id",
            "connection_id",
            "connection_version_id",
            "resource_scope_reference",
            "target_source_object_id",
            "target_source_revision_id",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be non-zero")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        for name in (
            "crm_entity_type",
            "crm_entity_id",
            "crm_activity_id",
            "crm_activity_revision",
            "crm_attachment_id",
            "relationship_revision",
            "disk_file_id",
            "disk_file_revision",
        ):
            value = getattr(self, name)
            if value != value.strip():
                raise ValueError(f"{name} must be normalized")
        return self


class Bitrix24CrmMaterialAcceptance(DefinitionModel):
    """Explicit acceptance pinned to the observed Disk source revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    acceptance_id: UUID
    claim_id: UUID
    workspace_id: UUID
    environment_id: UUID
    target_source_object_id: UUID
    target_source_revision_id: UUID
    disk_file_id: str = Field(min_length=1, max_length=128)
    disk_file_revision: str = Field(min_length=1, max_length=128)
    accepted_by: UUID
    accepted_at: datetime

    @model_validator(mode="after")
    def validate_acceptance(self) -> "Bitrix24CrmMaterialAcceptance":
        for name in (
            "acceptance_id",
            "claim_id",
            "workspace_id",
            "environment_id",
            "target_source_object_id",
            "target_source_revision_id",
            "accepted_by",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be non-zero")
        if self.accepted_at.tzinfo is None or self.accepted_at.utcoffset() is None:
            raise ValueError("accepted_at must be timezone-aware")
        return self


class Bitrix24CrmMaterialRevocation(DefinitionModel):
    """Append-only visibility for source or relationship revocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    revocation_id: UUID
    claim_id: UUID
    workspace_id: UUID
    environment_id: UUID
    state: Bitrix24CrmMaterialState
    reason: str = Field(min_length=1, max_length=128)
    observed_at: datetime

    @model_validator(mode="after")
    def validate_revocation(self) -> "Bitrix24CrmMaterialRevocation":
        for name in (
            "revocation_id",
            "claim_id",
            "workspace_id",
            "environment_id",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be non-zero")
        if self.state not in {
            Bitrix24CrmMaterialState.RELATION_REVOKED,
            Bitrix24CrmMaterialState.SOURCE_REVOKED,
            Bitrix24CrmMaterialState.INACCESSIBLE,
        }:
            raise ValueError("revocation state must represent a loss of authority")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return self


class Bitrix24CrmMaterialHandoff(DefinitionModel):
    """Claim plus an optional acceptance or explicit revocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim: Bitrix24CrmMaterialClaim
    acceptance: Bitrix24CrmMaterialAcceptance | None = None
    revocation: Bitrix24CrmMaterialRevocation | None = None

    @model_validator(mode="after")
    def validate_handoff(self) -> "Bitrix24CrmMaterialHandoff":
        if self.acceptance is not None and self.revocation is not None:
            raise ValueError("handoff cannot be accepted and revoked")
        if self.acceptance is not None:
            acceptance = self.acceptance
            claim = self.claim
            if (
                acceptance.claim_id != claim.claim_id
                or acceptance.workspace_id != claim.workspace_id
                or acceptance.environment_id != claim.environment_id
                or acceptance.target_source_object_id != claim.target_source_object_id
                or acceptance.target_source_revision_id != claim.target_source_revision_id
                or acceptance.disk_file_id != claim.disk_file_id
                or acceptance.disk_file_revision != claim.disk_file_revision
            ):
                raise ValueError("acceptance must pin the exact CRM binding")
        if self.revocation is not None:
            revocation = self.revocation
            if (
                revocation.claim_id != self.claim.claim_id
                or revocation.workspace_id != self.claim.workspace_id
                or revocation.environment_id != self.claim.environment_id
            ):
                raise ValueError("revocation must pin the exact CRM claim scope")
        return self

    @property
    def state(self) -> Bitrix24CrmMaterialState:
        if self.revocation is not None:
            return self.revocation.state
        if self.acceptance is not None:
            return Bitrix24CrmMaterialState.ACCEPTED
        return Bitrix24CrmMaterialState.OBSERVED

    @property
    def grants_source_disclosure(self) -> bool:
        return False

    @property
    def grants_pricing_basis_authority(self) -> bool:
        return False


__all__ = [
    "Bitrix24CrmMaterialAcceptance",
    "Bitrix24CrmMaterialClaim",
    "Bitrix24CrmMaterialHandoff",
    "Bitrix24CrmMaterialRevocation",
    "Bitrix24CrmMaterialState",
]
