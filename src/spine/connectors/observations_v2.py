"""Provider-neutral v2 identity, representation, and observation contracts."""

from __future__ import annotations

import base64
from datetime import datetime
from enum import Enum
import hashlib
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, StrictBytes, StrictInt, model_validator

from spine.connectors.observations import (
    BoundedIdentifier,
    ConnectorCoverage,
    DigestHex,
    validate_observation_provenance,
)
from spine.domain.common import DefinitionModel, VersionIdentifier
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapabilityKind,
    ProviderKind,
)
from spine.domain.connections.bitrix24_disk_scope import Bitrix24DiskMembershipEvidence


EXTERNAL_OBSERVATION_SCHEMA_V2 = "external-observation:v2"


class ExternalIdentityAssurance(str, Enum):
    PROVIDER_STABLE = "provider_stable"
    SCOPE_STABLE = "scope_stable"
    PATH_SCOPED = "path_scoped"
    UNPROVEN = "unproven"


class ProviderObservationDiscovery(str, Enum):
    DIRECT = "direct"
    SNAPSHOT = "snapshot"
    INCREMENTAL = "incremental"
    RECONCILIATION = "reconciliation"


class ProviderLifecycleState(str, Enum):
    CONFIRMED_DELETED = "confirmed_deleted"
    OUT_OF_SCOPE = "out_of_scope"
    PERMISSION_LOST = "permission_lost"
    CONNECTION_REVOKED = "connection_revoked"
    AMBIGUOUS_ABSENCE = "ambiguous_absence"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"


class _ProviderKey(DefinitionModel):
    connection_id: UUID

    @model_validator(mode="after")
    def validate_connection(self) -> "_ProviderKey":
        if self.connection_id.int == 0:
            raise ValueError("connection_id must be a non-zero UUID")
        return self


class ProviderFileKey(_ProviderKey):
    kind: Literal["file"] = "file"
    schema_version: Literal["provider-file-key:v2"] = "provider-file-key:v2"
    provider_object_id: str = Field(min_length=1, max_length=1024)


class ProviderTabularScopeKey(_ProviderKey):
    kind: Literal["tabular_scope"] = "tabular_scope"
    schema_version: Literal["provider-tabular-scope-key:v2"] = (
        "provider-tabular-scope-key:v2"
    )
    collection_id: str = Field(min_length=1, max_length=1024)
    table_id: str = Field(min_length=1, max_length=1024)


class ProviderTabularRecordKey(_ProviderKey):
    kind: Literal["tabular_record"] = "tabular_record"
    schema_version: Literal["provider-tabular-record-key:v2"] = (
        "provider-tabular-record-key:v2"
    )
    collection_id: str = Field(min_length=1, max_length=1024)
    table_id: str = Field(min_length=1, max_length=1024)
    record_id: str = Field(min_length=1, max_length=1024)


class ProviderStructuredObjectKey(_ProviderKey):
    kind: Literal["structured_object"] = "structured_object"
    schema_version: Literal["provider-structured-object-key:v2"] = (
        "provider-structured-object-key:v2"
    )
    object_type: str = Field(min_length=1, max_length=128)
    provider_object_id: str = Field(min_length=1, max_length=1024)


class ProviderImapMessageKey(_ProviderKey):
    """Inspectable qualified IMAP identity retained in the shared envelope."""

    kind: Literal["imap_message"] = "imap_message"
    schema_version: Literal["provider-imap-message-key:v1"] = (
        "provider-imap-message-key:v1"
    )
    workspace_id: UUID
    mailbox_binding_id: UUID
    folder_binding_id: UUID
    uid_validity: StrictInt = Field(ge=1, le=2**32 - 1)
    uid: StrictInt = Field(ge=1, le=2**32 - 1)

    @model_validator(mode="after")
    def validate_bindings(self) -> "ProviderImapMessageKey":
        if (
            self.workspace_id.int == 0
            or self.mailbox_binding_id.int == 0
            or self.folder_binding_id.int == 0
        ):
            raise ValueError("IMAP identity bindings must be non-zero")
        return self


ProviderIdentityKey = (
    ProviderFileKey
    | ProviderTabularScopeKey
    | ProviderTabularRecordKey
    | ProviderStructuredObjectKey
    | ProviderImapMessageKey
)


class ProviderExternalIdentity(DefinitionModel):
    schema_version: Literal["provider-external-identity:v2"] = (
        "provider-external-identity:v2"
    )
    external_namespace: str = Field(min_length=1, max_length=64)
    external_generation: VersionIdentifier
    external_object_id: str = Field(min_length=1, max_length=64)
    provider_key: ProviderIdentityKey = Field(discriminator="kind")
    assurance: ExternalIdentityAssurance
    assurance_evidence_reference: VersionIdentifier

    @model_validator(mode="after")
    def validate_qualified_identity(self) -> "ProviderExternalIdentity":
        if isinstance(self.provider_key, ProviderImapMessageKey):
            from spine.domain.sources.imap import ImapMessageKey, imap_source_identity

            expected = imap_source_identity(
                ImapMessageKey(
                    workspace_id=self.provider_key.workspace_id,
                    connection_id=self.provider_key.connection_id,
                    mailbox_binding_id=self.provider_key.mailbox_binding_id,
                    folder_binding_id=self.provider_key.folder_binding_id,
                    uid_validity=self.provider_key.uid_validity,
                    uid=self.provider_key.uid,
                )
            )
            if (
                self.external_namespace != expected.external_namespace
                or self.external_generation != expected.external_generation
                or self.external_object_id != expected.external_object_id
            ):
                raise ValueError("IMAP external identity does not match provider key")
        return self


class ProviderFileRevisionEvidence(DefinitionModel):
    """Exact provider file metadata retained with one binary observation."""

    schema_version: Literal["provider-file-revision-evidence:v2"] = (
        "provider-file-revision-evidence:v2"
    )
    provider_revision: str = Field(min_length=1, max_length=128)
    provider_resource_id: str | None = Field(default=None, min_length=1, max_length=1024)
    resource_name: str | None = Field(default=None, min_length=1, max_length=1024)
    resource_path: str | None = Field(default=None, min_length=1, max_length=2048)
    content_md5: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    content_sha256: DigestHex | None = None
    provider_payload_digest: DigestHex | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    provider_payload: dict[str, str | int | None] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    byte_length: StrictInt = Field(ge=0)
    modified_at: datetime

    @model_validator(mode="after")
    def validate_modified_at(self) -> "ProviderFileRevisionEvidence":
        if self.modified_at.tzinfo is None or self.modified_at.utcoffset() is None:
            raise ValueError("provider file modified_at must be timezone-aware")
        return self


class ProviderImapRevisionEvidence(DefinitionModel):
    """Exact raw-message and provider evidence without public content fields."""

    schema_version: Literal["provider-imap-revision-evidence:v1"] = (
        "provider-imap-revision-evidence:v1"
    )
    mailbox_binding_id: UUID
    location_binding_id: UUID
    uid_validity: StrictInt = Field(ge=1, le=2**32 - 1)
    uid: StrictInt = Field(ge=1, le=2**32 - 1)
    content_sha256: DigestHex
    byte_length: StrictInt = Field(ge=0)
    declared_octets: StrictInt = Field(ge=0)
    envelope: StrictBytes = Field(repr=False, exclude=True)
    envelope_sha256: DigestHex
    internal_date: datetime
    fetch_data_item: Literal["BODY.PEEK[]"]
    seen_preserved: Literal[True]
    mutation_evidence_complete: Literal[True]

    @model_validator(mode="after")
    def validate_provider_evidence(self) -> "ProviderImapRevisionEvidence":
        if self.mailbox_binding_id.int == 0 or self.location_binding_id.int == 0:
            raise ValueError("email evidence bindings must be non-zero")
        if self.internal_date.tzinfo is None or self.internal_date.utcoffset() is None:
            raise ValueError("email internal_date must be timezone-aware")
        if hashlib.sha256(self.envelope).hexdigest() != self.envelope_sha256:
            raise ValueError("email envelope digest does not match evidence")
        return self

    def canonical_payload(self) -> dict[str, str | int | None]:
        """Return the versioned, JSON-safe provider evidence stored canonically."""

        return {
            "schema_version": self.schema_version,
            "mailbox_binding_id": str(self.mailbox_binding_id),
            "location_binding_id": str(self.location_binding_id),
            "uid_validity": self.uid_validity,
            "uid": self.uid,
            "envelope_base64": base64.b64encode(self.envelope).decode("ascii"),
            "envelope_sha256": self.envelope_sha256,
            "declared_octets": self.declared_octets,
            "fetch_data_item": self.fetch_data_item,
            "seen_preserved": "true",
            "mutation_evidence_complete": "true",
        }


class _ContentObservationRepresentation(DefinitionModel):
    original_bytes: StrictBytes
    media_type: str = Field(min_length=1, max_length=255)
    extraction_profile: VersionIdentifier
    locator_contract_version: VersionIdentifier


class BinaryObservationRepresentation(_ContentObservationRepresentation):
    kind: Literal["binary"] = "binary"
    schema_version: Literal["binary-observation:v2"] = "binary-observation:v2"
    revision_evidence: ProviderFileRevisionEvidence | None = None
    imap_revision_evidence: ProviderImapRevisionEvidence | None = None

    @model_validator(mode="after")
    def validate_revision_evidence(self) -> "BinaryObservationRepresentation":
        if self.revision_evidence is not None and self.imap_revision_evidence is not None:
            raise ValueError("binary observation revision evidence must be unambiguous")
        imap_evidence = self.imap_revision_evidence
        if imap_evidence is not None and (
            imap_evidence.byte_length != len(self.original_bytes)
            or imap_evidence.content_sha256
            != hashlib.sha256(self.original_bytes).hexdigest()
            or imap_evidence.declared_octets != len(self.original_bytes)
        ):
            raise ValueError("email revision evidence does not match original bytes")
        evidence = self.revision_evidence
        if evidence is None:
            return self
        if evidence.byte_length != len(self.original_bytes):
            raise ValueError("provider revision byte length does not match original bytes")
        if (
            evidence.content_sha256 is not None
            and hashlib.sha256(self.original_bytes).hexdigest()
            != evidence.content_sha256
        ):
            raise ValueError("provider revision sha256 does not match original bytes")
        if (
            evidence.content_md5 is not None
            and hashlib.md5(self.original_bytes, usedforsecurity=False).hexdigest()
            != evidence.content_md5
        ):
            raise ValueError("provider revision md5 does not match original bytes")
        return self


class TabularObservationRepresentation(_ContentObservationRepresentation):
    kind: Literal["tabular"] = "tabular"
    schema_version: Literal["tabular-observation:v2"] = "tabular-observation:v2"


class StructuredObservationRepresentation(_ContentObservationRepresentation):
    kind: Literal["structured"] = "structured"
    schema_version: Literal["structured-observation:v2"] = "structured-observation:v2"


class LifecycleObservationRepresentation(DefinitionModel):
    kind: Literal["lifecycle"] = "lifecycle"
    schema_version: Literal["lifecycle-observation:v2"] = "lifecycle-observation:v2"
    state: ProviderLifecycleState
    evidence_reference: BoundedIdentifier
    evidence_digest: DigestHex


ProviderObservationRepresentation = (
    BinaryObservationRepresentation
    | TabularObservationRepresentation
    | StructuredObservationRepresentation
    | LifecycleObservationRepresentation
)


class NormalizedExternalObservationV2(DefinitionModel):
    """Provider-neutral envelope immediately upstream of canonical ingestion."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[EXTERNAL_OBSERVATION_SCHEMA_V2] = (
        EXTERNAL_OBSERVATION_SCHEMA_V2
    )
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    provider_kind: ProviderKind
    resource_scope_reference: OpaqueProviderReference
    authorization_generation: StrictInt = Field(ge=1)
    configuration_generation: StrictInt = Field(ge=1)
    source_kind: BoundedIdentifier
    external_identity: ProviderExternalIdentity
    representation: ProviderObservationRepresentation = Field(discriminator="kind")
    coverage: ConnectorCoverage
    discovery: ProviderObservationDiscovery = ProviderObservationDiscovery.DIRECT
    producer_reference: BoundedIdentifier
    event_identity: BoundedIdentifier
    event_digest: DigestHex
    order_scheme: VersionIdentifier | None = None
    order_token: BoundedIdentifier | None = None
    origin_locator_kind: BoundedIdentifier | None = None
    origin_locator_value: BoundedIdentifier | None = None
    origin_locator_schema: VersionIdentifier | None = None
    origin_locator_digest: DigestHex | None = None
    observer_service: BoundedIdentifier
    received_at: datetime
    observed_at: datetime
    provider_media_type: str | None = Field(default=None, max_length=255)
    provider_version_before: str | None = Field(default=None, min_length=1, max_length=128)
    provider_version_after: str | None = Field(default=None, min_length=1, max_length=128)
    scope_membership: Bitrix24DiskMembershipEvidence | None = None

    @model_validator(mode="after")
    def validate_scope_and_provenance(self) -> "NormalizedExternalObservationV2":
        for name in (
            "workspace_id",
            "environment_id",
            "connection_id",
            "connection_version_id",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be a non-zero UUID")
        if self.external_identity.provider_key.connection_id != self.connection_id:
            raise ValueError("external identity connection does not match observation")
        if (
            isinstance(self.external_identity.provider_key, ProviderImapMessageKey)
            and self.external_identity.provider_key.workspace_id != self.workspace_id
        ):
            raise ValueError("IMAP external identity workspace does not match observation")
        imap_key = self.external_identity.provider_key
        if isinstance(imap_key, ProviderImapMessageKey):
            if (
                self.provider_kind is not ProviderKind.YANDEX_MAIL
                or not isinstance(self.representation, BinaryObservationRepresentation)
                or self.representation.media_type != "message/rfc822"
                or self.representation.imap_revision_evidence is None
            ):
                raise ValueError("IMAP identity requires raw Yandex Mail evidence")
            imap_evidence = self.representation.imap_revision_evidence
            if (
                imap_evidence.mailbox_binding_id != imap_key.mailbox_binding_id
                or imap_evidence.location_binding_id != imap_key.folder_binding_id
                or imap_evidence.uid_validity != imap_key.uid_validity
                or imap_evidence.uid != imap_key.uid
            ):
                raise ValueError("IMAP revision evidence does not match identity")
        validate_observation_provenance(
            order_scheme=self.order_scheme,
            order_token=self.order_token,
            provider_version_before=self.provider_version_before,
            provider_version_after=self.provider_version_after,
            received_at=self.received_at,
            observed_at=self.observed_at,
        )
        return self

    def required_capabilities(self) -> frozenset[ProviderCapabilityKind]:
        required: set[ProviderCapabilityKind] = set()
        if isinstance(self.representation, BinaryObservationRepresentation):
            required.add(ProviderCapabilityKind.BINARY_READ)
        elif isinstance(self.representation, TabularObservationRepresentation):
            required.add(ProviderCapabilityKind.TABULAR_READ)
        elif isinstance(self.representation, StructuredObservationRepresentation):
            required.add(ProviderCapabilityKind.STRUCTURED_READ)
        else:
            required.add(ProviderCapabilityKind.LIFECYCLE_EVIDENCE)
        if self.external_identity.assurance in {
            ExternalIdentityAssurance.PROVIDER_STABLE,
            ExternalIdentityAssurance.SCOPE_STABLE,
        }:
            required.add(ProviderCapabilityKind.STABLE_IDENTITY)
        if self.discovery is ProviderObservationDiscovery.INCREMENTAL:
            required.add(ProviderCapabilityKind.INCREMENTAL_CHANGE_DISCOVERY)
        return frozenset(required)

    @property
    def original_bytes(self) -> bytes:
        return _content_representation(self).original_bytes

    @property
    def media_type(self) -> str:
        return _content_representation(self).media_type

    @property
    def extraction_profile(self) -> str:
        return _content_representation(self).extraction_profile

    @property
    def locator_contract_version(self) -> str:
        return _content_representation(self).locator_contract_version


def _content_representation(
    observation: NormalizedExternalObservationV2,
) -> _ContentObservationRepresentation:
    if isinstance(observation.representation, LifecycleObservationRepresentation):
        raise ValueError("lifecycle observations cannot enter content ingestion")
    return observation.representation


__all__ = [
    "BinaryObservationRepresentation",
    "EXTERNAL_OBSERVATION_SCHEMA_V2",
    "ExternalIdentityAssurance",
    "LifecycleObservationRepresentation",
    "NormalizedExternalObservationV2",
    "ProviderExternalIdentity",
    "ProviderFileKey",
    "ProviderFileRevisionEvidence",
    "ProviderImapMessageKey",
    "ProviderImapRevisionEvidence",
    "ProviderLifecycleState",
    "ProviderObservationDiscovery",
    "ProviderStructuredObjectKey",
    "ProviderTabularRecordKey",
    "ProviderTabularScopeKey",
    "StructuredObservationRepresentation",
    "TabularObservationRepresentation",
]
