"""Revision-scoped links observed in Google register rows.

These records preserve provider claims separately from canonical business
associations. Accepting a link requires an explicit, same-scope target revision.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from spine.domain.common import DefinitionModel, VersionIdentifier
from .google import GoogleDriveFileKey, GoogleRegisterRowKey


class RegisterLinkRole(str, Enum):
    REQUEST = "request"
    MEETING = "meeting"
    CLIENT = "client"
    PROJECT = "project"
    HISTORICAL_ESTIMATE = "historical_estimate"
    SOURCE_ESTIMATE = "source_estimate"
    FINAL_ESTIMATE = "final_estimate"
    DRIVE_MATERIAL = "drive_material"


class RegisterAssociationFutureOwner(str, Enum):
    COMMERCIAL_REQUEST = "commercial_request"
    COMMERCIAL_MEETING = "commercial_meeting"
    COMMERCIAL_CLIENT = "commercial_client"
    COMMERCIAL_PROJECT = "commercial_project"
    COMMERCIAL_ESTIMATE_HISTORY = "commercial_estimate_history"
    COMMERCIAL_SOURCE_ESTIMATE = "commercial_source_estimate"
    COMMERCIAL_FINAL_ESTIMATE = "commercial_final_estimate"
    COMMERCIAL_DRIVE_MATERIAL = "commercial_drive_material"


class RegisterExternalIdentifierType(str, Enum):
    GOOGLE_DRIVE_FILE_ID = "google_drive_file_id"
    PROVIDER_BUSINESS_ID = "provider_business_id"
    URL = "url"
    DISPLAY_TEXT = "display_text"


class RegisterObservedLinkClaim(DefinitionModel):
    """Immutable extraction claim tied to exact accepted register evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: UUID
    workspace_id: UUID
    environment_id: UUID
    row_source_object_id: UUID
    row_source_revision_id: UUID
    row_provider_identity: GoogleRegisterRowKey
    row_revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    row_original_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    role: RegisterLinkRole
    claimed_external_identifier: str = Field(min_length=1, max_length=1024)
    external_identifier_type: RegisterExternalIdentifierType
    extraction_profile: VersionIdentifier
    evidence_locator: str = Field(min_length=1, max_length=1024)
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance_id: UUID
    observed_at: datetime

    @model_validator(mode="after")
    def validate_claim(self) -> "RegisterObservedLinkClaim":
        for name in (
            "claim_id",
            "workspace_id",
            "environment_id",
            "row_source_object_id",
            "row_source_revision_id",
            "provenance_id",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be non-zero")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.claimed_external_identifier != self.claimed_external_identifier.strip():
            raise ValueError("claimed identifier must be normalized")
        if not self.evidence_locator.strip():
            raise ValueError("evidence locator must not be empty")
        return self


class RegisterAssociationAcceptance(DefinitionModel):
    """Explicitly accepted source-to-source link pinned to two revisions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    acceptance_id: UUID
    claim_id: UUID
    workspace_id: UUID
    environment_id: UUID
    row_source_object_id: UUID
    row_source_revision_id: UUID
    target_source_object_id: UUID
    target_source_revision_id: UUID
    target_provider_identity: GoogleDriveFileKey
    role: RegisterLinkRole
    accepted_by: UUID
    accepted_at: datetime

    @model_validator(mode="after")
    def validate_acceptance(self) -> "RegisterAssociationAcceptance":
        for name in (
            "acceptance_id",
            "claim_id",
            "workspace_id",
            "environment_id",
            "row_source_object_id",
            "row_source_revision_id",
            "target_source_object_id",
            "target_source_revision_id",
            "accepted_by",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be non-zero")
        if self.row_source_object_id == self.target_source_object_id:
            raise ValueError("register row cannot be associated to itself")
        if self.accepted_at.tzinfo is None or self.accepted_at.utcoffset() is None:
            raise ValueError("accepted_at must be timezone-aware")
        return self


class RegisterAssociationHandoff(DefinitionModel):
    """Typed output for either accepted or explicitly unresolved claims."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim: RegisterObservedLinkClaim
    acceptance: RegisterAssociationAcceptance | None = None
    unresolved_owner: RegisterAssociationFutureOwner | None = None
    unresolved_reason: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_handoff(self) -> "RegisterAssociationHandoff":
        if (self.acceptance is None) == (self.unresolved_owner is None):
            raise ValueError("handoff must be accepted or name an unresolved owner")
        if (self.unresolved_owner is None) != (self.unresolved_reason is None):
            raise ValueError("unresolved owner and reason must be supplied together")
        if self.acceptance is not None and (
            self.acceptance.claim_id != self.claim.claim_id
            or self.acceptance.role is not self.claim.role
            or self.acceptance.workspace_id != self.claim.workspace_id
            or self.acceptance.environment_id != self.claim.environment_id
            or self.acceptance.row_source_object_id != self.claim.row_source_object_id
            or self.acceptance.row_source_revision_id
            != self.claim.row_source_revision_id
            or self.claim.external_identifier_type
            is not RegisterExternalIdentifierType.GOOGLE_DRIVE_FILE_ID
            or self.acceptance.target_provider_identity.file_id
            != self.claim.claimed_external_identifier
        ):
            raise ValueError(
                "acceptance must pin the claim's exact row revision and scope"
            )
        return self


__all__ = [
    "RegisterAssociationAcceptance",
    "RegisterAssociationHandoff",
    "RegisterAssociationFutureOwner",
    "RegisterExternalIdentifierType",
    "RegisterLinkRole",
    "RegisterObservedLinkClaim",
]
