"""Trusted, immutable selectors for canonical acceptance schema versions."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from spine.domain.common import DefinitionModel, VersionIdentifier, require_version_identifier


class CanonicalAcceptanceAuthorityKind(str, Enum):
    PROVIDER_CONNECTION = "provider_connection"
    UPLOAD = "upload"


class TrustedCanonicalAcceptanceSelector(DefinitionModel):
    """Immutable authority binding that chooses a canonical acceptance schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["trusted-canonical-acceptance-selector:v1"] = (
        "trusted-canonical-acceptance-selector:v1"
    )
    selector_id: UUID
    selector_series_id: UUID
    selector_version: int = Field(ge=1)
    workspace_id: UUID
    environment_id: UUID
    source_kind: str = Field(min_length=1, max_length=128)
    source_contract_id: str = Field(min_length=1, max_length=128)
    source_contract_version: VersionIdentifier
    authority_kind: CanonicalAcceptanceAuthorityKind
    approved_scope_reference: UUID
    provider_kind: str | None = None
    provider_connection_id: UUID | None = None
    provider_connection_version_id: UUID | None = None
    provider_approval_id: UUID | None = None
    resource_scope_reference: UUID | None = None
    upload_authority_reference: UUID | None = None
    root_policy_id: UUID | None = None
    root_policy_version: VersionIdentifier | None = None
    root_policy_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    content_policy_id: UUID | None = None
    content_policy_version: VersionIdentifier | None = None
    content_policy_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    required_canonical_acceptance_schema_version: int = Field(ge=1)
    approval_reference: UUID
    predecessor_selector_id: UUID | None = None
    audit_reference: UUID
    idempotency_identity: str = Field(min_length=1, max_length=255)
    created_at: datetime

    @model_validator(mode="after")
    def validate_selector(self) -> "TrustedCanonicalAcceptanceSelector":
        ids = (
            self.selector_id, self.selector_series_id, self.workspace_id,
            self.environment_id, self.approved_scope_reference,
            self.approval_reference, self.audit_reference,
        )
        if any(value.int == 0 for value in ids):
            raise ValueError("selector identifiers must be non-zero")
        require_version_identifier(self.source_contract_version, field_name="source_contract_version")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        if self.predecessor_selector_id == self.selector_id:
            raise ValueError("selector cannot precede itself")
        if (self.selector_version == 1) != (self.predecessor_selector_id is None):
            raise ValueError("selector version and predecessor do not form a valid history")
        policy_fields = (
            self.root_policy_id, self.root_policy_version, self.root_policy_digest,
            self.content_policy_id, self.content_policy_version, self.content_policy_digest,
        )
        if any(value is None for value in policy_fields) and any(value is not None for value in policy_fields):
            raise ValueError("root and content policy references must be complete")
        if any(value is not None and value.int == 0 for value in (self.root_policy_id, self.content_policy_id)):
            raise ValueError("policy identifiers must be non-zero")
        if self.authority_kind is CanonicalAcceptanceAuthorityKind.PROVIDER_CONNECTION:
            provider_fields = (
                self.provider_kind, self.provider_connection_id,
                self.provider_connection_version_id, self.provider_approval_id,
                self.resource_scope_reference,
            )
            if any(value is None for value in provider_fields) or self.upload_authority_reference is not None:
                raise ValueError("provider selector requires exact connection authority")
            if any(value.int == 0 for value in (
                self.provider_connection_id, self.provider_connection_version_id,
                self.provider_approval_id, self.resource_scope_reference,
            )):
                raise ValueError("provider authority identifiers must be non-zero")
        else:
            if self.upload_authority_reference is None or self.provider_connection_id is not None:
                raise ValueError("upload selector requires exact upload authority")
            if self.upload_authority_reference.int == 0:
                raise ValueError("upload authority identifier must be non-zero")
        return self

    @property
    def series_identity(self) -> tuple[object, ...]:
        """Dimensions immutable across selector successors in this series."""

        return (
            self.workspace_id,
            self.environment_id,
            self.source_kind,
            self.source_contract_id,
            self.source_contract_version,
            self.authority_kind,
            self.approved_scope_reference,
            self.provider_kind,
            self.provider_connection_id,
            self.resource_scope_reference,
            self.upload_authority_reference,
        )


class CanonicalAcceptanceSelectorHead(DefinitionModel):
    """Current selector pointer with a compare-and-swap generation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    environment_id: UUID
    selector_series_id: UUID
    current_selector_id: UUID
    cas_version: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_head(self) -> "CanonicalAcceptanceSelectorHead":
        if any(value.int == 0 for value in (
            self.workspace_id, self.environment_id, self.selector_series_id,
            self.current_selector_id,
        )):
            raise ValueError("selector head identifiers must be non-zero")
        return self


class TrustedSourceSelectorContext(DefinitionModel):
    """Persisted source authority facts supplied by a trusted server-side reader."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_kind: str
    source_contract_id: str
    source_contract_version: str
    authority_kind: CanonicalAcceptanceAuthorityKind
    approved_scope_reference: UUID
    provider_kind: str | None = None
    provider_connection_id: UUID | None = None
    provider_connection_version_id: UUID | None = None
    provider_approval_id: UUID | None = None
    resource_scope_reference: UUID | None = None
    upload_authority_reference: UUID | None = None
    root_policy_id: UUID | None = None
    root_policy_version: str | None = None
    root_policy_digest: str | None = None
    content_policy_id: UUID | None = None
    content_policy_version: str | None = None
    content_policy_digest: str | None = None

    @model_validator(mode="after")
    def validate_context(self) -> "TrustedSourceSelectorContext":
        if self.source_object_id.int == 0:
            raise ValueError("source_object_id must be non-zero")
        return self


def selector_applies(
    selector: TrustedCanonicalAcceptanceSelector,
    source: TrustedSourceSelectorContext,
) -> bool:
    """Return whether all persisted source and authority facts match exactly."""

    names = (
        "workspace_id", "environment_id", "source_kind", "source_contract_id",
        "source_contract_version", "authority_kind", "approved_scope_reference",
        "provider_kind", "provider_connection_id", "provider_connection_version_id",
        "provider_approval_id", "resource_scope_reference", "upload_authority_reference",
        "root_policy_id", "root_policy_version", "root_policy_digest",
        "content_policy_id", "content_policy_version", "content_policy_digest",
    )
    return all(getattr(selector, name) == getattr(source, name) for name in names)


__all__ = [
    "CanonicalAcceptanceAuthorityKind",
    "CanonicalAcceptanceSelectorHead",
    "TrustedCanonicalAcceptanceSelector",
    "TrustedSourceSelectorContext",
    "selector_applies",
]
