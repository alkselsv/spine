"""Provider-neutral v2 identity, representation, and observation contracts."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
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


ProviderIdentityKey = (
    ProviderFileKey
    | ProviderTabularScopeKey
    | ProviderTabularRecordKey
    | ProviderStructuredObjectKey
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


class _ContentObservationRepresentation(DefinitionModel):
    original_bytes: StrictBytes
    media_type: str = Field(min_length=1, max_length=255)
    extraction_profile: VersionIdentifier
    locator_contract_version: VersionIdentifier


class BinaryObservationRepresentation(_ContentObservationRepresentation):
    kind: Literal["binary"] = "binary"
    schema_version: Literal["binary-observation:v2"] = "binary-observation:v2"


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
    "ProviderLifecycleState",
    "ProviderObservationDiscovery",
    "ProviderStructuredObjectKey",
    "ProviderTabularRecordKey",
    "ProviderTabularScopeKey",
    "StructuredObservationRepresentation",
    "TabularObservationRepresentation",
]
