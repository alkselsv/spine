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

from pydantic import ConfigDict, Field, StrictBool, StrictBytes, StrictInt, StrictStr, model_validator

from spine.application.connections.google import (
    GoogleProvider,
    GoogleConnectionRepository,
    GoogleReadAdmission,
    GoogleReadCommand,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.lifecycle import connector_source_object_id
from spine.connectors.observations import (
    ConnectorCoverage,
    NormalizedConnectorObservation,
)
from spine.domain.common import DefinitionModel, VersionIdentifier
from spine.domain.connections.google import (
    GoogleConnection,
    OpaqueGoogleReference,
)
from spine.domain.sources.google import (
    GoogleDriveFileKey,
    GoogleDriveLifecycleSignal,
    google_source_identity,
)


GOOGLE_DRIVE_FILE_LOCATOR = "locator:google-drive-file:v1"
GOOGLE_DRIVE_DOWNLOAD_PROFILE = "google-drive-download:v1"
GOOGLE_DRIVE_EXPORT_PROFILE = "google-drive-export:v1"
GOOGLE_DRIVE_VERSION_SCHEME = "google-drive-version:v1"


class GoogleDriveConcurrentModificationError(RuntimeError):
    """The file changed while its immutable observation was being read."""


class GoogleDriveTokenExpiredError(RuntimeError):
    """The provider no longer accepts the requested change-feed token."""


class GoogleDriveRetryExhaustedError(RuntimeError):
    """Bounded provider retries could not obtain a change-feed page."""


class GoogleDriveChangeGapError(RuntimeError):
    """The provider reported a gap that cannot be continued safely."""


class GoogleDriveAccessOutcomeError(RuntimeError):
    """A Drive 401/403/404 response mapped to a typed, content-free outcome."""

    def __init__(self, signal: GoogleDriveLifecycleSignal) -> None:
        self.signal = signal
        super().__init__(signal.value)


def drive_access_signal(status_code: int) -> GoogleDriveLifecycleSignal:
    """Map provider access statuses without interpreting them as deletion."""

    return {
        401: GoogleDriveLifecycleSignal.HTTP_401,
        403: GoogleDriveLifecycleSignal.HTTP_403,
        404: GoogleDriveLifecycleSignal.HTTP_404,
    }.get(status_code, GoogleDriveLifecycleSignal.AMBIGUOUS)


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
    modified_time: datetime | None = None


class DriveFileContent(DefinitionModel):
    """Exact bytes returned by download or an approved export operation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    content: StrictBytes
    media_type: StrictStr = Field(min_length=1, max_length=255)


class DriveFileReadProfile(DefinitionModel):
    """Immutable representation selection for one Drive read."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    export_mime_type: StrictStr | None = Field(default=None, max_length=255)
    max_attempts: StrictInt = Field(default=2, ge=1, le=3)


class DriveSnapshotItem(DefinitionModel):
    """One stable file identity returned by an initial Drive enumeration."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    file_id: StrictStr = Field(min_length=1, max_length=256)
    drive_id: StrictStr | None = Field(default=None, max_length=1024)


class DriveSnapshotPage(DefinitionModel):
    """One provider page whose item set is immutable for a sync attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    page_token: StrictStr | None = Field(default=None, max_length=4096)
    next_page_token: StrictStr | None = Field(default=None, max_length=4096)
    incomplete_search: bool
    items: tuple[DriveSnapshotItem, ...] = ()

    @model_validator(mode="after")
    def validate_items(self) -> "DriveSnapshotPage":
        if len({item.file_id for item in self.items}) != len(self.items):
            raise ValueError("Drive snapshot page file IDs must be unique")
        return self


class DriveChange(DefinitionModel):
    """One provider change with a stable replay identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    file_id: StrictStr = Field(min_length=1, max_length=256)
    drive_id: StrictStr | None = Field(default=None, max_length=1024)
    change_id: StrictStr | None = Field(default=None, max_length=256)
    provider_version: StrictStr | None = Field(default=None, max_length=128)
    removed: bool = False
    trashed: StrictBool = False
    in_scope: StrictBool | None = None
    parent_ids: tuple[StrictStr, ...] = Field(default=(), max_length=128)

    @property
    def change_key(self) -> str:
        if self.change_id:
            return f"id:{self.change_id}"
        return (
            f"file:{self.file_id}:version:{self.provider_version or 'unknown'}"
            f":removed:{str(self.removed).lower()}"
        )

    def lifecycle_signal(
        self, *, in_scope: bool | None = None
    ) -> GoogleDriveLifecycleSignal:
        """Classify the provider observation without granting deletion authority."""

        if self.removed:
            return GoogleDriveLifecycleSignal.REMOVED
        if self.trashed:
            return GoogleDriveLifecycleSignal.TRASHED
        if in_scope is None:
            in_scope = self.in_scope
        if in_scope is False:
            return GoogleDriveLifecycleSignal.MOVED_OUT_OF_SCOPE
        if in_scope is True:
            return GoogleDriveLifecycleSignal.PRESENT
        return GoogleDriveLifecycleSignal.AMBIGUOUS


class DriveChangePage(DefinitionModel):
    """One immutable page from Drive ``changes.list``."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    page_token: StrictStr = Field(min_length=1, max_length=4096)
    next_page_token: StrictStr | None = Field(default=None, max_length=4096)
    new_start_page_token: StrictStr | None = Field(default=None, max_length=4096)
    incomplete_search: bool = False
    changes: tuple[DriveChange, ...] = ()

    @model_validator(mode="after")
    def validate_changes(self) -> "DriveChangePage":
        if self.next_page_token is not None and self.new_start_page_token is not None:
            raise ValueError("terminal Drive start token cannot accompany another page")
        return self


class GoogleDriveSnapshotService(Protocol):
    """Official Drive operations needed by the bounded initial snapshot."""

    async def get_start_page_token(
        self,
        connection: GoogleConnection,
        resource_scope_reference: OpaqueGoogleReference,
    ) -> str: ...

    async def list_page(
        self,
        connection: GoogleConnection,
        resource_scope_reference: OpaqueGoogleReference,
        page_token: str | None,
    ) -> DriveSnapshotPage: ...

    async def get_metadata_for_file(
        self,
        connection: GoogleConnection,
        resource_scope_reference: OpaqueGoogleReference,
        file_id: str,
    ) -> DriveFileMetadata: ...


class GoogleDriveChangeService(Protocol):
    """Official Drive change-feed operation needed by incremental sync."""

    async def list_changes(
        self,
        connection: GoogleConnection,
        resource_scope_reference: OpaqueGoogleReference,
        page_token: str,
    ) -> DriveChangePage: ...


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
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    resource_scope_reference: UUID
    authorization_generation: int
    configuration_generation: int
    resource_object_reference: str | None
    lifecycle_query: object | None


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
    modified_time: datetime | None = None
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
        if self.modified_time is not None and (
            self.modified_time.tzinfo is None or self.modified_time.utcoffset() is None
        ):
            raise ValueError("Drive modified_time must be timezone-aware")
        return self

    def to_normalized_observation(self) -> NormalizedConnectorObservation:
        """Map Drive-specific evidence to the shared connector contract."""

        identity = google_source_identity(
            GoogleDriveFileKey(connection_id=self.connection_id, file_id=self.file_id)
        )
        event_identity = _event_identity(self)
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
        for attempt in range(self.profile.max_attempts):
            try:
                return await self._read_once(connection, command)
            except GoogleDriveConcurrentModificationError:
                if attempt + 1 == self.profile.max_attempts:
                    raise
            except GoogleDriveAccessOutcomeError:
                raise
            except Exception as error:
                translated = _access_outcome(error)
                if translated is not None:
                    raise translated from None
                raise
        raise AssertionError("Drive read attempts must return or raise")

    async def _read_once(
        self,
        connection: GoogleConnection,
        command: GoogleReadCommand,
    ) -> GoogleDriveObservation:
        metadata_before = await self.service.get_metadata(
            connection, command.resource_scope_reference
        ) if command.resource_object_reference is None else await self._metadata_for_file(
            connection, command
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

        metadata_after = (
            await self.service.get_metadata(connection, command.resource_scope_reference)
            if command.resource_object_reference is None
            else await self._metadata_for_file(connection, command)
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
            modified_time=metadata_before.modified_time,
            received_at=now,
            observed_at=now,
        )

    async def _metadata_for_file(
        self, connection: GoogleConnection, command: GoogleReadCommand
    ) -> DriveFileMetadata:
        file_id = command.resource_object_reference
        method = getattr(self.service, "get_metadata_for_file", None)
        if file_id is None or not callable(method):
            raise ValueError("Drive snapshot reads require a file-aware metadata adapter")
        return await method(connection, command.resource_scope_reference, file_id)


@dataclass(frozen=True, slots=True)
class GoogleDriveObservationReader:
    """Adapt admitted Drive observations to the shared connector handoff."""

    admission: GoogleReadAdmission[GoogleDriveObservation]

    async def read(
        self,
        command: _ConnectorReadCommand,
        context: TrustedPersistenceContext,
    ) -> NormalizedConnectorObservation:
        _validate_drive_lifecycle_query(command)
        observation = await self.admission.read(
            GoogleReadCommand(
                connection_id=command.connection_id,
                resource_scope_reference=OpaqueGoogleReference(
                    value=command.resource_scope_reference
                ),
                authorization_generation=command.authorization_generation,
                configuration_generation=command.configuration_generation,
                resource_object_reference=getattr(command, "resource_object_reference", None),
                lifecycle_query=getattr(command, "lifecycle_query", None),
            ),
            context,
        )
        return observation.to_normalized_observation()


def _validate_drive_lifecycle_query(command: object) -> None:
    query = getattr(command, "lifecycle_query", None)
    if query is None:
        return
    file_id = getattr(command, "resource_object_reference", None)
    if not isinstance(file_id, str):
        raise ValueError("Drive lifecycle query has no requested file identity")
    identity = google_source_identity(
        GoogleDriveFileKey(
            connection_id=getattr(command, "connection_id"),
            file_id=file_id,
        )
    )
    expected_source_id = connector_source_object_id(
        getattr(command, "workspace_id"),
        getattr(command, "environment_id"),
        getattr(command, "connection_id"),
        identity.external_namespace,
        identity.external_generation,
        identity.external_object_id,
    )
    if (
        query.provider_object_reference != file_id
        or query.source_object_id != expected_source_id
        or (
            query.source_external_namespace,
            query.source_external_generation,
            query.source_external_object_id,
        )
        != (
            identity.external_namespace,
            identity.external_generation,
            identity.external_object_id,
        )
    ):
        raise ValueError("Drive lifecycle query does not bind the requested file")


@dataclass(frozen=True, slots=True)
class GoogleDriveInitialSnapshotReader:
    """List an approved scope and read each file through the existing admission."""

    connection_repository: GoogleConnectionRepository
    service: GoogleDriveSnapshotService
    admission: GoogleReadAdmission[GoogleDriveObservation]

    async def get_start_page_token(self, command, context) -> str:
        try:
            async with self._admit(command, context) as connection:
                token = await self.service.get_start_page_token(
                    connection, OpaqueGoogleReference(value=command.resource_scope_reference)
                )
        except GoogleDriveAccessOutcomeError:
            raise
        except Exception as error:
            translated = _access_outcome(error)
            if translated is not None:
                raise translated from None
            raise
        if not token or len(token) > 4096:
            raise ValueError("Drive start page token is invalid")
        return token

    async def list_page(self, command, page_token, context) -> DriveSnapshotPage:
        try:
            async with self._admit(command, context) as connection:
                page = await self.service.list_page(
                    connection,
                    OpaqueGoogleReference(value=command.resource_scope_reference),
                    page_token,
                )
        except GoogleDriveAccessOutcomeError:
            raise
        except Exception as error:
            translated = _access_outcome(error)
            if translated is not None:
                raise translated from None
            raise
        return page

    async def read(self, command: _ConnectorReadCommand, context) -> NormalizedConnectorObservation:
        _validate_drive_lifecycle_query(command)
        observation = await self.admission.read(
            GoogleReadCommand(
                connection_id=command.connection_id,
                resource_scope_reference=OpaqueGoogleReference(
                    value=command.resource_scope_reference
                ),
                authorization_generation=command.authorization_generation,
                configuration_generation=command.configuration_generation,
                resource_object_reference=getattr(command, "resource_object_reference", None),
                lifecycle_query=getattr(command, "lifecycle_query", None),
            ),
            context,
        )
        return observation.to_normalized_observation()

    def _admit(self, command, context):
        return self.connection_repository.admit_read(
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


@dataclass(frozen=True, slots=True)
class GoogleDriveIncrementalReader:
    """List Drive changes only after the same trusted read admission."""

    connection_repository: GoogleConnectionRepository
    service: GoogleDriveChangeService

    async def list_changes(self, command, page_token, context) -> DriveChangePage:
        try:
            async with self._admit(command, context) as connection:
                page = await self.service.list_changes(
                    connection,
                    OpaqueGoogleReference(value=command.resource_scope_reference),
                    page_token,
                )
        except GoogleDriveAccessOutcomeError:
            raise
        except Exception as error:
            translated = _access_outcome(error)
            if translated is not None:
                raise translated from None
            raise
        return page

    def _admit(self, command, context):
        return self.connection_repository.admit_read(
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


# Short names keep the adapter discoverable without exposing provider SDK terms.
GoogleDriveProvider = GoogleDriveProviderAdapter
GoogleDriveReader = GoogleDriveObservationReader


def _event_payload(observation: GoogleDriveObservation) -> dict[str, object]:
    return {
        "file_id": observation.file_id,
        "file_name": observation.file_name,
        "provider_media_type": observation.provider_media_type,
        "media_type": observation.media_type,
        "representation_profile": observation.representation_profile,
        "provider_version": observation.provider_version_after,
        "content_sha256": hashlib.sha256(observation.original_bytes).hexdigest(),
        "current_url": observation.current_url,
        "drive_id": observation.drive_id,
        "parent_ids": sorted(observation.parent_ids),
        "modified_time": (
            observation.modified_time.isoformat()
            if observation.modified_time is not None
            else None
        ),
    }


def _event_digest(observation: GoogleDriveObservation) -> str:
    payload = _event_payload(observation)
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _event_identity(observation: GoogleDriveObservation) -> str:
    canonical = json.dumps(
        _event_payload(observation),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return f"drive:{digest[:56]}"


def _bounded_provider_version(value: str) -> str:
    """Encode numeric Drive versions into Spine's letter-prefixed identifiers."""

    if not value.isascii() or not value.isdecimal() or (
        len(value) > 1 and value.startswith("0")
    ):
        raise ValueError("Google Drive provider version must be canonical decimal")
    encoded = f"gdrive-v:{value}"
    if len(encoded) > 64:
        raise ValueError("Google Drive provider version exceeds the contract bound")
    return encoded


def _access_outcome(error: Exception) -> GoogleDriveAccessOutcomeError | None:
    status_code = getattr(error, "status_code", None)
    if not isinstance(status_code, int):
        status_code = getattr(error, "status", None)
    if isinstance(status_code, int) and status_code in {401, 403, 404}:
        return GoogleDriveAccessOutcomeError(drive_access_signal(status_code))
    return None


__all__ = [
    "DriveFileContent",
    "DriveFileMetadata",
    "DriveFileReadProfile",
    "DriveSnapshotItem",
    "DriveSnapshotPage",
    "DriveChange",
    "DriveChangePage",
    "GOOGLE_DRIVE_DOWNLOAD_PROFILE",
    "GOOGLE_DRIVE_EXPORT_PROFILE",
    "GOOGLE_DRIVE_FILE_LOCATOR",
    "GOOGLE_DRIVE_VERSION_SCHEME",
    "GoogleDriveConcurrentModificationError",
    "GoogleDriveTokenExpiredError",
    "GoogleDriveRetryExhaustedError",
    "GoogleDriveChangeGapError",
    "GoogleDriveAccessOutcomeError",
    "GoogleDriveObservation",
    "GoogleDriveObservationReader",
    "GoogleDriveProvider",
    "GoogleDriveProviderAdapter",
    "GoogleDriveReader",
    "GoogleDriveInitialSnapshotReader",
    "GoogleDriveService",
    "GoogleDriveSnapshotService",
    "GoogleDriveChangeService",
    "GoogleDriveIncrementalReader",
    "drive_access_signal",
]
