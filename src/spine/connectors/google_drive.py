"""Provider-neutral Google Drive file reading behind trusted admission.

The adapter deliberately stops at a typed observation.  It does not know about
Cognee, persistence, cursors or canonical deletion decisions.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

from pydantic import ConfigDict, Field, StrictBytes, StrictStr, model_validator

from spine.application.connections.google import (
    GoogleProvider,
    GoogleReadAdmission,
    GoogleReadCommand,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.connectors.observations import (
    ConnectorCoverage,
    NormalizedConnectorObservation,
)
from spine.domain.common import DefinitionModel, VersionIdentifier
from spine.domain.connections.google import (
    GoogleConnection,
    OpaqueGoogleReference,
)
from spine.domain.sources.google import GoogleDriveFileKey, google_source_identity


GOOGLE_DRIVE_FILE_LOCATOR = "locator:google-drive-file:v1"
GOOGLE_DRIVE_DOWNLOAD_PROFILE = "google-drive-download:v1"
GOOGLE_DRIVE_EXPORT_PROFILE = "google-drive-export:v1"
GOOGLE_DRIVE_VERSION_SCHEME = "google-drive-version:v1"


class GoogleDriveConcurrentModificationError(RuntimeError):
    """The file changed while its immutable observation was being read."""


class DriveFileMetadata(DefinitionModel):
    """The provider metadata needed to guard one exact file read."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    file_id: StrictStr = Field(min_length=1, max_length=64)
    name: StrictStr = Field(min_length=1, max_length=1024)
    mime_type: StrictStr = Field(min_length=1, max_length=255)
    version: StrictStr = Field(min_length=1, max_length=128)
    current_url: StrictStr | None = Field(default=None, max_length=2048)
    drive_id: StrictStr | None = Field(default=None, max_length=1024)
    parent_ids: tuple[StrictStr, ...] = Field(default=(), max_length=128)


class DriveFileContent(DefinitionModel):
    """Exact bytes returned by download or an approved export operation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    content: StrictBytes
    media_type: StrictStr = Field(min_length=1, max_length=255)


class DriveFileReadProfile(DefinitionModel):
    """Immutable representation selection for one Drive read."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    export_mime_type: StrictStr | None = Field(default=None, max_length=255)


class GoogleDriveService(Protocol):
    """Small fakeable boundary for official Drive API operations."""

    async def get_metadata(
        self,
        connection: GoogleConnection,
        resource_scope_reference: OpaqueGoogleReference,
    ) -> DriveFileMetadata: ...

    async def download(
        self,
        connection: GoogleConnection,
        resource_scope_reference: OpaqueGoogleReference,
        file_id: str,
        version: str,
    ) -> DriveFileContent: ...

    async def export(
        self,
        connection: GoogleConnection,
        resource_scope_reference: OpaqueGoogleReference,
        file_id: str,
        version: str,
        export_mime_type: str,
    ) -> DriveFileContent: ...


class _ConnectorReadCommand(Protocol):
    connection_id: UUID
    resource_scope_reference: UUID
    authorization_generation: int
    configuration_generation: int


class GoogleDriveObservation(DefinitionModel):
    """One complete, version-guarded Drive file observation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    resource_scope_reference: UUID
    authorization_generation: int = Field(ge=1)
    configuration_generation: int = Field(ge=1)
    file_id: StrictStr = Field(min_length=1, max_length=64)
    file_name: StrictStr = Field(min_length=1, max_length=1024)
    provider_media_type: StrictStr = Field(min_length=1, max_length=255)
    media_type: StrictStr = Field(min_length=1, max_length=255)
    representation_profile: VersionIdentifier
    provider_version_before: StrictStr = Field(min_length=1, max_length=128)
    provider_version_after: StrictStr = Field(min_length=1, max_length=128)
    original_bytes: StrictBytes
    current_url: StrictStr | None = Field(default=None, max_length=2048)
    drive_id: StrictStr | None = Field(default=None, max_length=1024)
    parent_ids: tuple[StrictStr, ...] = Field(default=(), max_length=128)
    received_at: datetime
    observed_at: datetime

    @model_validator(mode="after")
    def validate_observation(self) -> "GoogleDriveObservation":
        for name in (
            "workspace_id",
            "environment_id",
            "connection_id",
            "resource_scope_reference",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be a non-zero UUID")
        for value in (self.received_at, self.observed_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Drive observation timestamps must be timezone-aware")
        if self.received_at < self.observed_at:
            raise ValueError("received_at cannot precede observed_at")
        if self.provider_version_before != self.provider_version_after:
            raise ValueError("Drive provider version guard is not stable")
        return self

    def to_normalized_observation(self) -> NormalizedConnectorObservation:
        """Map Drive-specific evidence to the shared connector contract."""

        identity = google_source_identity(
            GoogleDriveFileKey(connection_id=self.connection_id, file_id=self.file_id)
        )
        event_identity = _event_identity(self.file_id, self.provider_version_after)
        return NormalizedConnectorObservation(
            workspace_id=self.workspace_id,
            environment_id=self.environment_id,
            connection_id=self.connection_id,
            resource_scope_reference=self.resource_scope_reference,
            authorization_generation=self.authorization_generation,
            configuration_generation=self.configuration_generation,
            source_kind="document",
            external_identity=identity,
            original_bytes=self.original_bytes,
            media_type=self.media_type,
            extraction_profile=self.representation_profile,
            locator_contract_version=GOOGLE_DRIVE_FILE_LOCATOR,
            coverage=ConnectorCoverage.COMPLETE,
            producer_reference="google-drive-reader",
            event_identity=event_identity,
            event_digest=_event_digest(self),
            order_scheme=GOOGLE_DRIVE_VERSION_SCHEME,
            order_token=_bounded_provider_version(self.provider_version_after),
            origin_locator_kind="google-drive-file",
            origin_locator_value=self.file_id,
            origin_locator_schema=GOOGLE_DRIVE_FILE_LOCATOR,
            origin_locator_digest=hashlib.sha256(self.file_id.encode("utf-8")).hexdigest(),
            observer_service="google-drive-reader",
            received_at=self.received_at,
            observed_at=self.observed_at,
            provider_media_type=self.provider_media_type,
            provider_version_before=self.provider_version_before,
            provider_version_after=self.provider_version_after,
        )


@dataclass(frozen=True, slots=True)
class GoogleDriveProviderAdapter(GoogleProvider[GoogleDriveObservation]):
    """Read exact Drive bytes only after ``GoogleReadAdmission`` has admitted."""

    service: GoogleDriveService
    profile: DriveFileReadProfile = DriveFileReadProfile()
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    async def read(
        self,
        connection: GoogleConnection,
        command: GoogleReadCommand,
    ) -> GoogleDriveObservation:
        metadata_before = await self.service.get_metadata(
            connection, command.resource_scope_reference
        )
        if self.profile.export_mime_type is None:
            content = await self.service.download(
                connection,
                command.resource_scope_reference,
                metadata_before.file_id,
                metadata_before.version,
            )
            representation_profile = GOOGLE_DRIVE_DOWNLOAD_PROFILE
        else:
            content = await self.service.export(
                connection,
                command.resource_scope_reference,
                metadata_before.file_id,
                metadata_before.version,
                self.profile.export_mime_type,
            )
            representation_profile = GOOGLE_DRIVE_EXPORT_PROFILE
            if content.media_type != self.profile.export_mime_type:
                raise ValueError("Drive export media type does not match the approved profile")

        metadata_after = await self.service.get_metadata(
            connection, command.resource_scope_reference
        )
        if (
            metadata_after.file_id != metadata_before.file_id
            or metadata_after.version != metadata_before.version
        ):
            raise GoogleDriveConcurrentModificationError(
                "Drive file changed during guarded read."
            )
        if self.profile.export_mime_type is None and content.media_type != metadata_before.mime_type:
            raise ValueError("Drive download media type does not match file metadata")

        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Google Drive reader clock must be timezone-aware")
        return GoogleDriveObservation(
            workspace_id=connection.workspace_id,
            environment_id=connection.environment_id,
            connection_id=connection.connection_id,
            resource_scope_reference=command.resource_scope_reference.value,
            authorization_generation=command.authorization_generation,
            configuration_generation=command.configuration_generation,
            file_id=metadata_before.file_id,
            file_name=metadata_before.name,
            provider_media_type=metadata_before.mime_type,
            media_type=content.media_type,
            representation_profile=representation_profile,
            provider_version_before=metadata_before.version,
            provider_version_after=metadata_after.version,
            original_bytes=content.content,
            current_url=metadata_before.current_url,
            drive_id=metadata_before.drive_id,
            parent_ids=metadata_before.parent_ids,
            received_at=now,
            observed_at=now,
        )


@dataclass(frozen=True, slots=True)
class GoogleDriveObservationReader:
    """Adapt admitted Drive observations to the shared connector handoff."""

    admission: GoogleReadAdmission[GoogleDriveObservation]

    async def read(
        self,
        command: _ConnectorReadCommand,
        context: TrustedPersistenceContext,
    ) -> NormalizedConnectorObservation:
        observation = await self.admission.read(
            GoogleReadCommand(
                connection_id=command.connection_id,
                resource_scope_reference=OpaqueGoogleReference(
                    value=command.resource_scope_reference
                ),
                authorization_generation=command.authorization_generation,
                configuration_generation=command.configuration_generation,
            ),
            context,
        )
        return observation.to_normalized_observation()


# Short names keep the adapter discoverable without exposing provider SDK terms.
GoogleDriveProvider = GoogleDriveProviderAdapter
GoogleDriveReader = GoogleDriveObservationReader


def _event_digest(observation: GoogleDriveObservation) -> str:
    payload = {
        "file_id": observation.file_id,
        "provider_media_type": observation.provider_media_type,
        "media_type": observation.media_type,
        "representation_profile": observation.representation_profile,
        "provider_version": observation.provider_version_after,
        "content_sha256": hashlib.sha256(observation.original_bytes).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _event_identity(file_id: str, version: str) -> str:
    digest = hashlib.sha256(f"{file_id}:{version}".encode("utf-8")).hexdigest()
    return f"drive:{digest[:56]}"


def _bounded_provider_version(value: str) -> str:
    """Encode numeric Drive versions into Spine's letter-prefixed identifiers."""

    encoded = f"gdrive-v:{value}"
    if len(encoded) > 64:
        raise ValueError("Google Drive provider version exceeds the contract bound")
    return encoded


__all__ = [
    "DriveFileContent",
    "DriveFileMetadata",
    "DriveFileReadProfile",
    "GOOGLE_DRIVE_DOWNLOAD_PROFILE",
    "GOOGLE_DRIVE_EXPORT_PROFILE",
    "GOOGLE_DRIVE_FILE_LOCATOR",
    "GOOGLE_DRIVE_VERSION_SCHEME",
    "GoogleDriveConcurrentModificationError",
    "GoogleDriveObservation",
    "GoogleDriveObservationReader",
    "GoogleDriveProvider",
    "GoogleDriveProviderAdapter",
    "GoogleDriveReader",
    "GoogleDriveService",
]
