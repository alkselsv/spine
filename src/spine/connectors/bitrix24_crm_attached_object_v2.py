"""Fail-closed resolver for the qualified CRM ``attached_object`` strategy.

The provider's concrete response semantics remain unqualified. This module
defines the narrow read-only port and validates synthetic boundary results; it
does not retain provider payloads or infer ownership from unrelated fields.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field, StrictStr, model_validator

from spine.application.crm_relationship_v2_source_match import (
    CrmFilesItemIdentity,
    CrmMappedDiskRevisionEvidence,
    DiskRevisionIdentity,
)
from spine.application.crm_relationship_v2_contracts import (
    Bitrix24CrmRelationshipV2ReadCommand,
)
from spine.application.connections.bitrix24_oauth import Bitrix24PinnedGrant
from spine.connectors.bitrix24_crm_files_v2 import Bitrix24CrmFilesItem
from spine.domain.common import DefinitionModel
from spine.domain.connections.provider import OpaqueProviderReference


class Bitrix24AttachedObjectGetCommand(DefinitionModel):
    """One explicitly scoped, read-only attached-object lookup."""

    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    resource_scope_reference: OpaqueProviderReference
    acting_human_id: UUID
    provider_instance: StrictStr = Field(min_length=1, max_length=128)
    provider_acting_user_id: StrictStr = Field(min_length=1, max_length=128)
    authorization_generation: int = Field(ge=1)
    configuration_generation: int = Field(ge=1)
    pinned_grant: Bitrix24PinnedGrant
    activity_namespace: StrictStr = Field(min_length=1, max_length=128)
    activity_type_id: StrictStr = Field(min_length=1, max_length=128)
    activity_id: StrictStr = Field(min_length=1, max_length=256)
    entity_namespace: StrictStr = Field(min_length=1, max_length=128)
    raw_entity_type_id: StrictStr = Field(min_length=1, max_length=128)
    entity_id: StrictStr = Field(min_length=1, max_length=256)
    attached_object_id: StrictStr = Field(min_length=1, max_length=256)


class Bitrix24CrmAttachedObjectProviderPort(Protocol):
    """Read one Disk attachment by its exact FILES identifier.

    The result is intentionally raw because concrete provider semantics are
    unqualified. The resolver immediately projects only the four allowlisted
    ownership and Disk identity fields into a typed proof.
    """

    async def get_attached_object(
        self, command: Bitrix24AttachedObjectGetCommand
    ) -> object: ...


Bitrix24CrmAttachedObjectErrorCode = Literal[
    "profile_strategy_mismatch",
    "profile_scope_mismatch",
    "item_namespace_mismatch",
    "authority_unavailable",
    "provider_failure",
    "malformed_provider_result",
    "ambiguous_provider_result",
    "ownership_mismatch",
    "namespace_collision",
]


class Bitrix24CrmAttachedObjectOwnershipProof(DefinitionModel):
    """Safe proof tying one FILES item to a namespaced Disk object."""

    mapping_strategy: Literal["attached_object"] = "attached_object"
    profile_id: UUID
    profile_version: int = Field(ge=1)
    ownership_proof_reference: StrictStr = Field(min_length=1, max_length=255)
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    acting_human_id: UUID
    authorization_generation: int = Field(ge=1)
    configuration_generation: int = Field(ge=1)
    files_item: Bitrix24CrmFilesItem
    activity_namespace: StrictStr = Field(min_length=1, max_length=128)
    activity_id: StrictStr = Field(min_length=1, max_length=256)
    module_id: StrictStr = Field(min_length=1, max_length=32)
    entity_type: StrictStr = Field(min_length=1, max_length=128)
    entity_namespace: StrictStr = Field(min_length=1, max_length=128)
    entity_id: StrictStr = Field(min_length=1, max_length=256)
    disk_namespace: StrictStr = Field(min_length=1, max_length=128)
    disk_object_id: StrictStr = Field(min_length=1, max_length=256)
    evidence_reference: StrictStr = Field(min_length=1, max_length=255)
    evidence_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")


class Bitrix24CrmAttachedObjectResult(DefinitionModel):
    """Disclosure-safe result for exactly one FILES item."""

    status: Literal["resolved", "uncertain"]
    proof: Bitrix24CrmAttachedObjectOwnershipProof | None = None
    error_code: Bitrix24CrmAttachedObjectErrorCode | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "Bitrix24CrmAttachedObjectResult":
        if self.status == "resolved":
            if self.proof is None or self.error_code is not None:
                raise ValueError(
                    "resolved attached object requires only an ownership proof"
                )
        elif self.proof is not None or self.error_code is None:
            raise ValueError("uncertain attached object requires only a bounded error")
        return self

    @classmethod
    def resolved(
        cls, proof: Bitrix24CrmAttachedObjectOwnershipProof
    ) -> "Bitrix24CrmAttachedObjectResult":
        return cls(status="resolved", proof=proof)

    @classmethod
    def uncertain(
        cls, code: Bitrix24CrmAttachedObjectErrorCode
    ) -> "Bitrix24CrmAttachedObjectResult":
        return cls(status="uncertain", error_code=code)


AuthorityCheck = Callable[[Bitrix24CrmRelationshipV2ReadCommand], Awaitable[bool]]


async def resolve_bitrix24_crm_attached_object(
    command: Bitrix24CrmRelationshipV2ReadCommand,
    *,
    files_item: Bitrix24CrmFilesItem,
    provider: Bitrix24CrmAttachedObjectProviderPort,
    authority_is_current: AuthorityCheck,
) -> Bitrix24CrmAttachedObjectResult:
    """Resolve one attached FILES item after and before current-authority checks.

    The callback is checked immediately before the provider call and again
    before releasing proof. A stale generation, failed read, malformed payload
    or ownership mismatch returns only a bounded error code.
    """

    profile = command.mapping_profile
    if profile.mapping_strategy != "attached_object":
        return Bitrix24CrmAttachedObjectResult.uncertain("profile_strategy_mismatch")
    if not _profile_matches_command(command):
        return Bitrix24CrmAttachedObjectResult.uncertain("profile_scope_mismatch")
    if files_item.namespace != profile.files_namespace:
        return Bitrix24CrmAttachedObjectResult.uncertain("item_namespace_mismatch")
    if profile.files_namespace == profile.disk_namespace:
        return Bitrix24CrmAttachedObjectResult.uncertain("namespace_collision")

    try:
        if not await authority_is_current(command):
            return Bitrix24CrmAttachedObjectResult.uncertain("authority_unavailable")
    except Exception:
        return Bitrix24CrmAttachedObjectResult.uncertain("authority_unavailable")

    get_command = Bitrix24AttachedObjectGetCommand(
        workspace_id=command.workspace_id,
        environment_id=command.environment_id,
        connection_id=command.connection_id,
        connection_version_id=command.connection_version_id,
        resource_scope_reference=command.resource_scope_reference,
        acting_human_id=command.acting_human_id,
        provider_instance=command.provider_instance,
        provider_acting_user_id=command.provider_acting_user_id,
        authorization_generation=command.authorization_generation,
        configuration_generation=command.configuration_generation,
        pinned_grant=command.pinned_grant,
        activity_namespace=command.activity_namespace,
        activity_type_id=command.activity_type_id,
        activity_id=command.activity_id,
        entity_namespace=command.entity_namespace,
        raw_entity_type_id=command.raw_entity_type_id,
        entity_id=command.entity_id,
        attached_object_id=files_item.value,
    )
    try:
        raw_result = await provider.get_attached_object(get_command)
    except Exception:
        return Bitrix24CrmAttachedObjectResult.uncertain("provider_failure")

    try:
        if not await authority_is_current(command):
            return Bitrix24CrmAttachedObjectResult.uncertain("authority_unavailable")
    except Exception:
        return Bitrix24CrmAttachedObjectResult.uncertain("authority_unavailable")

    record, error = _single_record(raw_result)
    if error is not None:
        return Bitrix24CrmAttachedObjectResult.uncertain(error)
    assert record is not None
    module_id = _required_string(record, "MODULE_ID")
    entity_type = _required_string(record, "ENTITY_TYPE")
    entity_id = _required_string(record, "ENTITY_ID")
    disk_object_id = _required_string(record, "OBJECT_ID")
    if None in (module_id, entity_type, entity_id, disk_object_id):
        return Bitrix24CrmAttachedObjectResult.uncertain("malformed_provider_result")

    # Bitrix CRM attachment ownership is represented by this exact tuple in
    # the synthetic port contract. Live provider semantics remain unqualified.
    if (
        module_id != "CRM"
        or entity_type != command.raw_entity_type_id
        or entity_id != command.entity_id
    ):
        return Bitrix24CrmAttachedObjectResult.uncertain("ownership_mismatch")

    digest_payload = {
        "files_namespace": files_item.namespace,
        "files_item_id": files_item.value,
        "activity_namespace": command.activity_namespace,
        "activity_id": command.activity_id,
        "module_id": module_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "entity_namespace": command.entity_namespace,
        "disk_namespace": profile.disk_namespace,
        "disk_object_id": disk_object_id,
        "profile_id": str(profile.profile_id),
        "profile_version": profile.profile_version,
        "workspace_id": str(command.workspace_id),
        "environment_id": str(command.environment_id),
        "connection_id": str(command.connection_id),
        "connection_version_id": str(command.connection_version_id),
        "authorization_generation": command.authorization_generation,
        "configuration_generation": command.configuration_generation,
    }
    digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return Bitrix24CrmAttachedObjectResult.resolved(
        Bitrix24CrmAttachedObjectOwnershipProof(
            profile_id=profile.profile_id,
            profile_version=profile.profile_version,
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            files_item=files_item,
            activity_namespace=command.activity_namespace,
            activity_id=command.activity_id,
            module_id=module_id,
            entity_type=entity_type,
            entity_namespace=command.entity_namespace,
            entity_id=entity_id,
            disk_namespace=profile.disk_namespace,
            disk_object_id=disk_object_id,
            ownership_proof_reference=profile.ownership_proof_reference or "",
            connection_id=command.connection_id,
            connection_version_id=command.connection_version_id,
            acting_human_id=command.acting_human_id,
            authorization_generation=command.authorization_generation,
            configuration_generation=command.configuration_generation,
            evidence_reference=(
                f"attached-object-proof:{profile.profile_id}:"
                f"{profile.profile_version}:{digest}"
            ),
            evidence_digest=digest,
        )
    )


def _profile_matches_command(
    command: Bitrix24CrmRelationshipV2ReadCommand,
) -> bool:
    profile = command.mapping_profile
    return (
        profile.workspace_id == command.workspace_id
        and profile.environment_id == command.environment_id
        and profile.connection_id == command.connection_id
        and profile.provider_instance == command.provider_instance
        and profile.activity_namespace == command.activity_namespace
        and command.activity_type_id in profile.activity_type_ids
        and command.raw_entity_type_id in profile.raw_entity_type_ids
        and profile.entity_namespace == command.entity_namespace
        and "disk.attachedObject.get" in profile.read_methods
    )


def bind_attached_disk_revision_evidence(
    proof: Bitrix24CrmAttachedObjectOwnershipProof,
    *,
    provider_revision: str,
) -> CrmMappedDiskRevisionEvidence:
    """Adapt one qualified T4 ownership proof to the inward match contract."""

    return CrmMappedDiskRevisionEvidence(
        workspace_id=proof.workspace_id,
        environment_id=proof.environment_id,
        connection_id=proof.connection_id,
        connection_version_id=proof.connection_version_id,
        mapping_strategy=proof.mapping_strategy,
        mapping_profile_id=proof.profile_id,
        mapping_profile_version=proof.profile_version,
        files_item=CrmFilesItemIdentity(
            namespace=proof.files_item.namespace,
            value=proof.files_item.value,
        ),
        disk_revision=DiskRevisionIdentity(
            namespace=proof.disk_namespace,
            object_id=proof.disk_object_id,
            provider_revision=provider_revision,
        ),
        ownership_proof_reference=proof.evidence_reference,
        ownership_proof_digest=proof.evidence_digest,
    )


def _single_record(
    value: object,
) -> tuple[Mapping[str, object] | None, Bitrix24CrmAttachedObjectErrorCode | None]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) != 1:
            return None, "ambiguous_provider_result"
        value = value[0]
    if not isinstance(value, Mapping):
        return None, "malformed_provider_result"
    return value, None


def _required_string(record: Mapping[str, object], key: str) -> str | None:
    value = record.get(key)
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    return value


__all__ = [
    "Bitrix24AttachedObjectGetCommand",
    "Bitrix24CrmAttachedObjectErrorCode",
    "Bitrix24CrmAttachedObjectOwnershipProof",
    "Bitrix24CrmAttachedObjectProviderPort",
    "Bitrix24CrmAttachedObjectResult",
    "bind_attached_disk_revision_evidence",
    "resolve_bitrix24_crm_attached_object",
]
