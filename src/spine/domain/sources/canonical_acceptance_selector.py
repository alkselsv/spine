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
    required_canonical_acceptance_schema_version: int = Field(ge=1, le=2)
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
            if self.approval_reference != self.provider_approval_id:
                raise ValueError("provider approval reference must match exact connection approval")
            if self.approved_scope_reference != self.resource_scope_reference:
                raise ValueError("approved scope must match exact provider resource scope")
            if self.required_canonical_acceptance_schema_version == 2 and (
                self.provider_kind != "bitrix24"
                or self.source_contract_version != "v2"
                or self.root_policy_id is None
            ):
                raise ValueError("acceptance schema v2 is reserved for qualified Bitrix24 S2 authority")
            if (self.provider_kind == "bitrix24"
                    and self.source_contract_version == "v2"
                    and self.required_canonical_acceptance_schema_version != 2):
                raise ValueError("Bitrix24 S2 selectors cannot downgrade to canonical acceptance v1")
            if self.provider_kind != "bitrix24" and self.required_canonical_acceptance_schema_version != 1:
                raise ValueError("provider compatibility selectors must use canonical acceptance v1")
            if self.provider_kind != "bitrix24" and self.root_policy_id is not None:
                raise ValueError("root/content selector references are reserved for Bitrix24 scope policies")
        else:
            if self.upload_authority_reference is None or self.provider_connection_id is not None:
                raise ValueError("upload selector requires exact upload authority")
            if self.root_policy_id is not None:
                raise ValueError("upload selectors cannot bind provider root/content policies")
            if self.upload_authority_reference.int == 0:
                raise ValueError("upload authority identifier must be non-zero")
            if self.approval_reference != self.upload_authority_reference:
                raise ValueError("upload approval reference must match exact upload authority")
            if self.required_canonical_acceptance_schema_version != 1:
                raise ValueError("upload compatibility selectors must use canonical acceptance v1")
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

    @property
    def authority_identity(self) -> tuple[object, ...]:
        """Exact connection/upload approval selected for this immutable record."""

        return (
            *self.series_identity,
            self.provider_connection_version_id,
            self.provider_approval_id,
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
        if any(value.int == 0 for value in (
            self.workspace_id, self.environment_id, self.source_object_id,
            self.approved_scope_reference,
        )):
            raise ValueError("trusted source context identifiers must be non-zero")
        if (self.root_policy_id, self.root_policy_version, self.root_policy_digest,
            self.content_policy_id, self.content_policy_version, self.content_policy_digest).count(None) not in (0, 6):
            raise ValueError("trusted source context policy references must be complete")
        if self.authority_kind is CanonicalAcceptanceAuthorityKind.PROVIDER_CONNECTION:
            if any(value is None for value in (
                self.provider_kind, self.provider_connection_id,
                self.provider_connection_version_id, self.provider_approval_id,
                self.resource_scope_reference,
            )) or self.upload_authority_reference is not None:
                raise ValueError("provider source context requires exact connection authority")
            if self.approved_scope_reference != self.resource_scope_reference:
                raise ValueError("provider source context scope reference does not match approved scope")
        elif self.upload_authority_reference is None or self.provider_connection_id is not None:
            raise ValueError("upload source context requires exact upload authority")
        return self

    @property
    def authority_identity(self) -> tuple[object, ...]:
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
            self.provider_connection_version_id,
            self.provider_approval_id,
        )


def selector_applies(
    selector: TrustedCanonicalAcceptanceSelector,
    source: TrustedSourceSelectorContext,
) -> bool:
    """Return whether all persisted source and authority facts match exactly."""

    names = (
        "root_policy_id", "root_policy_version", "root_policy_digest",
        "content_policy_id", "content_policy_version", "content_policy_digest",
    )
    return selector.authority_identity == source.authority_identity and all(
        getattr(selector, name) == getattr(source, name) for name in names
    )


__all__ = [
    "CanonicalAcceptanceAuthorityKind",
    "CanonicalAcceptanceSelectorHead",
    "TrustedCanonicalAcceptanceSelector",
    "TrustedSourceSelectorContext",
    "selector_applies",
]
