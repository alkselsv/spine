"""Immutable authority snapshots for Bitrix24 Disk root admission."""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from spine.domain.common import DefinitionModel, VersionIdentifier, require_version_identifier
from spine.domain.connections.provider import OpaqueProviderReference

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ObjectId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]


class Bitrix24DiskStorageClass(str, Enum):
    COMMON = "common"
    GROUP = "group"


class Bitrix24DiskContentPolicy(DefinitionModel):
    """Per-root, versioned content eligibility. Empty allowlists deny all."""

    policy_id: UUID
    version: VersionIdentifier
    digest: str = Field(default="", validate_default=False)
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    resource_scope_reference: OpaqueProviderReference
    allowed_formats: tuple[str, ...] = ()
    processing_mode: Literal["processor_required", "opaque_acceptance"] = "processor_required"

    @field_validator("allowed_formats")
    @classmethod
    def validate_formats(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted(value.strip().lower() for value in values))
        if any(not value or len(value) > 64 for value in normalized):
            raise ValueError("content formats must be bounded identifiers")
        if len(set(normalized)) != len(normalized):
            raise ValueError("content formats must be unique")
        return normalized

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        return require_version_identifier(value, field_name="version")

    @model_validator(mode="after")
    def validate_identity_digest(self) -> "Bitrix24DiskContentPolicy":
        if any(value.int == 0 for value in (self.policy_id, self.workspace_id, self.environment_id, self.connection_id, self.connection_version_id)):
            raise ValueError("policy scope identifiers must be non-zero")
        expected = bitrix24_content_policy_digest(self)
        if self.digest and self.digest != expected:
            raise ValueError("content policy digest does not match its immutable fields")
        if not self.digest:
            object.__setattr__(self, "digest", expected)
        return self


class Bitrix24DiskRootPolicy(DefinitionModel):
    """Exact storage/root authority; root IDs are never inferred from paths."""

    policy_id: UUID
    version: VersionIdentifier
    digest: str = Field(default="", validate_default=False)
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    resource_scope_reference: OpaqueProviderReference
    storage_id: ObjectId
    storage_class: Bitrix24DiskStorageClass
    group_entity_id: ObjectId | None = None
    storage_root_object_id: ObjectId
    approved_root_folder_id: ObjectId
    recurse: bool = False
    future_discovery: bool = False
    manifest_object_ids: tuple[ObjectId, ...] = ()
    content_policy_id: UUID
    content_policy_version: VersionIdentifier
    content_policy_digest: Digest
    traversal_profile_reference: OpaqueProviderReference

    @field_validator("version", "content_policy_version")
    @classmethod
    def validate_versions(cls, value: str, info: object) -> str:
        return require_version_identifier(value, field_name=getattr(info, "field_name", "version"))

    @field_validator("manifest_object_ids")
    @classmethod
    def validate_manifest(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("manifest object IDs must be unique")
        return tuple(sorted(values))

    @model_validator(mode="after")
    def validate_scope(self) -> "Bitrix24DiskRootPolicy":
        identifiers = (self.policy_id, self.workspace_id, self.environment_id, self.connection_id, self.connection_version_id, self.content_policy_id)
        if any(value.int == 0 for value in identifiers):
            raise ValueError("policy scope identifiers must be non-zero")
        if self.storage_class is Bitrix24DiskStorageClass.GROUP and self.group_entity_id is None:
            raise ValueError("group storage requires an exact group entity")
        if self.storage_class is Bitrix24DiskStorageClass.COMMON and self.group_entity_id is not None:
            raise ValueError("common storage cannot carry a group entity")
        if not self.future_discovery and not self.manifest_object_ids:
            raise ValueError("fixed-manifest policy requires at least one exact object")
        expected = bitrix24_root_policy_digest(self)
        if self.digest and self.digest != expected:
            raise ValueError("root policy digest does not match its immutable fields")
        if not self.digest:
            object.__setattr__(self, "digest", expected)
        return self


class Bitrix24DiskMembershipEvidence(DefinitionModel):
    """Positive exact ancestry evidence for one Disk object under one policy."""

    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    resource_scope_reference: OpaqueProviderReference
    root_policy_id: UUID
    root_policy_version: VersionIdentifier
    root_policy_digest: Digest
    content_policy_id: UUID
    content_policy_version: VersionIdentifier
    content_policy_digest: Digest
    authorization_generation: int = Field(ge=1)
    configuration_generation: int = Field(ge=1)
    grant_binding_id: UUID
    grant_token_epoch: int = Field(ge=1)
    grant_security_fence: int = Field(ge=0)
    storage_id: str
    storage_class: Bitrix24DiskStorageClass
    group_entity_id: str | None = None
    storage_root_object_id: str
    approved_root_folder_id: str
    object_id: str
    parent_folder_ids: tuple[str, ...]
    provider_revision: str | None = None

    @model_validator(mode="after")
    def validate_exact_chain(self) -> "Bitrix24DiskMembershipEvidence":
        if self.parent_folder_ids[-1:] != (self.approved_root_folder_id,):
            raise ValueError("membership evidence must end at the approved root")
        if len(set(self.parent_folder_ids)) != len(self.parent_folder_ids):
            raise ValueError("membership ancestry cannot contain a cycle")
        if self.storage_class is Bitrix24DiskStorageClass.GROUP and self.group_entity_id is None:
            raise ValueError("group membership requires its exact entity identity")
        if self.storage_class is Bitrix24DiskStorageClass.COMMON and self.group_entity_id is not None:
            raise ValueError("common membership cannot carry a group entity")
        if any(value.int == 0 for value in (self.workspace_id, self.environment_id, self.connection_id, self.connection_version_id, self.root_policy_id, self.content_policy_id, self.grant_binding_id)):
            raise ValueError("membership scope identifiers must be non-zero")
        return self


def _digest(model: DefinitionModel, excluded: frozenset[str]) -> str:
    payload = model.model_dump(mode="json", exclude=excluded)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def bitrix24_root_policy_digest(policy: Bitrix24DiskRootPolicy) -> str:
    return _digest(policy, frozenset({"digest"}))


def bitrix24_content_policy_digest(policy: Bitrix24DiskContentPolicy) -> str:
    return _digest(policy, frozenset({"digest"}))


__all__ = [
    "Bitrix24DiskContentPolicy",
    "Bitrix24DiskMembershipEvidence",
    "Bitrix24DiskRootPolicy",
    "Bitrix24DiskStorageClass",
    "bitrix24_content_policy_digest",
    "bitrix24_root_policy_digest",
]
