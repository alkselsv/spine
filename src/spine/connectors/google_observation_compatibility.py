"""Explicit mapping between Google v1 and provider-neutral v2 observations."""

from __future__ import annotations

from uuid import UUID

from spine.connectors.observations import NormalizedConnectorObservation
from spine.connectors.observations_v2 import (
    BinaryObservationRepresentation,
    ExternalIdentityAssurance,
    NormalizedExternalObservationV2,
    ProviderExternalIdentity,
    ProviderFileKey,
    ProviderTabularRecordKey,
    ProviderTabularScopeKey,
    TabularObservationRepresentation,
)
from spine.domain.connections.provider import OpaqueProviderReference, ProviderKind
from spine.domain.sources.google import (
    GOOGLE_EXTERNAL_GENERATION,
    GOOGLE_EXTERNAL_NAMESPACE,
    GoogleDriveFileKey,
    GoogleRegisterRowKey,
    GoogleSheetScopeKey,
    GoogleSourceIdentity,
)


def google_v1_observation_to_v2(
    observation: NormalizedConnectorObservation,
    *,
    connection_version_id: UUID,
) -> NormalizedExternalObservationV2:
    """Expand one accepted Google v1 observation without changing its semantics."""

    key = observation.external_identity.provider_key
    if isinstance(key, GoogleDriveFileKey):
        provider_key = ProviderFileKey(
            connection_id=key.connection_id,
            provider_object_id=key.file_id,
        )
        representation = BinaryObservationRepresentation(
            original_bytes=observation.original_bytes,
            media_type=observation.media_type,
            extraction_profile=observation.extraction_profile,
            locator_contract_version=observation.locator_contract_version,
        )
        assurance = ExternalIdentityAssurance.PROVIDER_STABLE
        assurance_reference = "google-drive-file-id:v1"
    elif isinstance(key, GoogleRegisterRowKey):
        provider_key = ProviderTabularRecordKey(
            connection_id=key.connection_id,
            collection_id=key.spreadsheet_id,
            table_id=str(key.sheet_id),
            record_id=key.immutable_business_id,
        )
        representation = TabularObservationRepresentation(
            original_bytes=observation.original_bytes,
            media_type=observation.media_type,
            extraction_profile=observation.extraction_profile,
            locator_contract_version=observation.locator_contract_version,
        )
        assurance = ExternalIdentityAssurance.SCOPE_STABLE
        assurance_reference = "google-sheets-business-id:v1"
    else:
        provider_key = ProviderTabularScopeKey(
            connection_id=key.connection_id,
            collection_id=key.spreadsheet_id,
            table_id=str(key.sheet_id),
        )
        representation = TabularObservationRepresentation(
            original_bytes=observation.original_bytes,
            media_type=observation.media_type,
            extraction_profile=observation.extraction_profile,
            locator_contract_version=observation.locator_contract_version,
        )
        assurance = ExternalIdentityAssurance.SCOPE_STABLE
        assurance_reference = "google-sheet-id:v1"
    values = observation.model_dump(
        exclude={
            "schema_version",
            "external_identity",
            "resource_scope_reference",
            "original_bytes",
            "media_type",
            "extraction_profile",
            "locator_contract_version",
        }
    )
    return NormalizedExternalObservationV2(
        **values,
        connection_version_id=connection_version_id,
        provider_kind=ProviderKind.GOOGLE,
        resource_scope_reference=OpaqueProviderReference(
            value=observation.resource_scope_reference
        ),
        external_identity=ProviderExternalIdentity(
            external_namespace=observation.external_identity.external_namespace,
            external_generation=observation.external_identity.external_generation,
            external_object_id=observation.external_identity.external_object_id,
            provider_key=provider_key,
            assurance=assurance,
            assurance_evidence_reference=assurance_reference,
        ),
        representation=representation,
    )


def google_v2_observation_to_v1(
    observation: NormalizedExternalObservationV2,
) -> NormalizedConnectorObservation:
    """Recover the exact Google v1 form for compatibility replay identity."""

    if observation.provider_kind is not ProviderKind.GOOGLE:
        raise ValueError("only Google v2 observations have a v1 compatibility form")
    if (
        observation.external_identity.external_namespace != GOOGLE_EXTERNAL_NAMESPACE
        or observation.external_identity.external_generation != GOOGLE_EXTERNAL_GENERATION
    ):
        raise ValueError("Google v2 observation does not use the v1 identity profile")
    identity = _google_identity(observation.external_identity)
    values = observation.model_dump(
        exclude={
            "schema_version",
            "connection_version_id",
            "provider_kind",
            "resource_scope_reference",
            "external_identity",
            "representation",
            "discovery",
        }
    )
    return NormalizedConnectorObservation(
        **values,
        resource_scope_reference=observation.resource_scope_reference.value,
        external_identity=identity,
        original_bytes=observation.original_bytes,
        media_type=observation.media_type,
        extraction_profile=observation.extraction_profile,
        locator_contract_version=observation.locator_contract_version,
    )


def _google_identity(identity: ProviderExternalIdentity) -> GoogleSourceIdentity:
    key = identity.provider_key
    if isinstance(key, ProviderFileKey):
        google_key = GoogleDriveFileKey(
            connection_id=key.connection_id, file_id=key.provider_object_id
        )
    elif isinstance(key, ProviderTabularRecordKey):
        google_key = GoogleRegisterRowKey(
            connection_id=key.connection_id,
            spreadsheet_id=key.collection_id,
            sheet_id=int(key.table_id),
            immutable_business_id=key.record_id,
        )
    elif isinstance(key, ProviderTabularScopeKey):
        google_key = GoogleSheetScopeKey(
            connection_id=key.connection_id,
            spreadsheet_id=key.collection_id,
            sheet_id=int(key.table_id),
        )
    else:
        raise ValueError("structured Google v1 identity has no compatibility mapping")
    return GoogleSourceIdentity(
        external_object_id=identity.external_object_id,
        provider_key=google_key,
    )


__all__ = ["google_v1_observation_to_v2", "google_v2_observation_to_v1"]
