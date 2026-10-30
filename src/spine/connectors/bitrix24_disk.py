"""Governed Bitrix24 Disk file reader for the provider-neutral source boundary."""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import (
    ConfigDict,
    Field,
    StrictBytes,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

from spine.connectors.observations import ConnectorCoverage
from spine.connectors.observations_v2 import (
    BinaryObservationRepresentation,
    ExternalIdentityAssurance,
    NormalizedExternalObservationV2,
    ProviderExternalIdentity,
    ProviderFileKey,
    ProviderFileRevisionEvidence,
    ProviderObservationDiscovery,
)
from spine.domain.common import DefinitionModel
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapability,
    ProviderCapabilityKind,
    ProviderCapabilityStatus,
    ProviderConnection,
    ProviderKind,
)

BITRIX24_DISK_EXTERNAL_NAMESPACE = "bitrix24-disk"
BITRIX24_DISK_EXTERNAL_GENERATION = "bitrix24-disk-object:v1"
BITRIX24_DISK_EXTRACTION_PROFILE = "bitrix24-disk-binary:v1"
BITRIX24_DISK_LOCATOR_CONTRACT = "locator:bitrix24-disk-object:v1"
BITRIX24_DISK_REQUIRED_CAPABILITIES = (
    ProviderCapabilityKind.ENUMERATION,
    ProviderCapabilityKind.METADATA_READ,
    ProviderCapabilityKind.BINARY_READ,
    ProviderCapabilityKind.STABLE_IDENTITY,
)


class Bitrix24DiskReadError(RuntimeError):
    """Content-free base error for one bounded Bitrix24 Disk read."""


class Bitrix24DiskThrottleError(Bitrix24DiskReadError):
    def __init__(self, *, retry_after_seconds: float) -> None:
        if retry_after_seconds < 0:
            raise ValueError("retry_after_seconds cannot be negative")
        self.retry_after_seconds = retry_after_seconds
        super().__init__("Bitrix24 Disk request was throttled.")


class Bitrix24DiskOverloadError(Bitrix24DiskReadError):
    def __init__(self, *, retry_after_seconds: float = 1.0) -> None:
        if retry_after_seconds < 0:
            raise ValueError("retry_after_seconds cannot be negative")
        self.retry_after_seconds = retry_after_seconds
        super().__init__("Bitrix24 Disk is overloaded.")


class Bitrix24DiskTransientError(Bitrix24DiskReadError):
    """Sanitized retryable provider or transport outage."""

    def __init__(self) -> None:
        super().__init__("Bitrix24 Disk is temporarily unavailable.")


class Bitrix24DiskAuthorizationRevokedError(Bitrix24DiskReadError):
    def __init__(self) -> None:
        super().__init__("Bitrix24 authorization was revoked.")


class Bitrix24DiskPermissionLostError(Bitrix24DiskReadError):
    def __init__(self) -> None:
        super().__init__("Bitrix24 Disk permission was lost.")


class Bitrix24DiskDownloadLinkExpiredError(Bitrix24DiskReadError):
    def __init__(self) -> None:
        super().__init__("Bitrix24 Disk download link expired.")


class Bitrix24DiskFileTooLargeError(Bitrix24DiskReadError):
    def __init__(self) -> None:
        super().__init__("Bitrix24 Disk file exceeds the approved read limit.")


class Bitrix24DiskPartialDownloadError(Bitrix24DiskReadError):
    def __init__(self) -> None:
        super().__init__("Bitrix24 Disk download did not match provider metadata.")


class Bitrix24DiskConcurrentModificationError(Bitrix24DiskReadError):
    def __init__(self) -> None:
        super().__init__("Bitrix24 Disk file changed during the guarded read.")


class Bitrix24DiskResourceNotFoundError(Bitrix24DiskReadError):
    def __init__(self) -> None:
        super().__init__("Bitrix24 Disk resource was not found in the approved scope.")


class Bitrix24DiskContractError(Bitrix24DiskReadError):
    def __init__(self) -> None:
        super().__init__("Bitrix24 Disk returned an invalid resource response.")


class Bitrix24DiskFile(DefinitionModel):
    """Provider response fields needed for a guarded Disk file read."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, populate_by_name=True, repr=False
    )

    id: str = Field(alias="ID", min_length=1, max_length=128)
    name: str = Field(alias="NAME", min_length=1, max_length=1024)
    type: Literal["file", "folder"] = Field(alias="TYPE")
    storage_id: str = Field(alias="STORAGE_ID", min_length=1, max_length=128)
    parent_id: str = Field(alias="PARENT_ID", min_length=1, max_length=128)
    global_content_version: str | None = Field(
        default=None, alias="GLOBAL_CONTENT_VERSION", max_length=128
    )
    file_id: str | None = Field(default=None, alias="FILE_ID", max_length=128)
    size: StrictInt | None = Field(default=None, alias="SIZE", ge=0)
    create_time: datetime | None = Field(default=None, alias="CREATE_TIME")
    update_time: datetime | None = Field(default=None, alias="UPDATE_TIME")
    download_url: str | None = Field(
        default=None, alias="DOWNLOAD_URL", min_length=1, max_length=4096
    )

    @field_validator(
        "id",
        "storage_id",
        "parent_id",
        "global_content_version",
        "file_id",
        mode="before",
    )
    @classmethod
    def _normalize_provider_identifier(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError(
                "Bitrix24 provider identifiers must be strings or integers"
            )
        return str(value)

    @model_validator(mode="after")
    def validate_file_metadata(self) -> "Bitrix24DiskFile":
        for value in (self.create_time, self.update_time):
            if value is not None and (
                value.tzinfo is None or value.utcoffset() is None
            ):
                raise ValueError("Bitrix24 Disk timestamps must be timezone-aware")
        if self.type == "file" and (
            self.global_content_version is None
            or self.size is None
            or self.update_time is None
        ):
            raise ValueError("Bitrix24 Disk file metadata is incomplete")
        return self


class Bitrix24DiskFilePage(DefinitionModel):
    """Official ``disk.folder.getChildren`` result page."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    items: tuple[Bitrix24DiskFile, ...] = Field(alias="result")
    total: StrictInt = Field(ge=0)


class Bitrix24DiskFileContent(DefinitionModel):
    content: StrictBytes
    media_type: str = Field(min_length=1, max_length=255)


class Bitrix24DiskReadProfile(DefinitionModel):
    page_size: StrictInt = Field(default=50, ge=1, le=50)
    max_attempts: StrictInt = Field(default=2, ge=1, le=5)
    max_download_refreshes: StrictInt = Field(default=1, ge=0, le=3)
    max_file_bytes: StrictInt = Field(default=32 * 1024 * 1024, ge=1)
    max_pages: StrictInt = Field(default=100, ge=1, le=1000)
    max_items: StrictInt = Field(default=10_000, ge=1, le=1_000_000)
    backoff_seconds: tuple[StrictFloat, ...] | None = None

    @model_validator(mode="after")
    def validate_backoff(self) -> "Bitrix24DiskReadProfile":
        values = self.backoff_seconds
        if values is None:
            values = tuple(
                0.25 * (2**attempt) for attempt in range(self.max_attempts - 1)
            )
            object.__setattr__(self, "backoff_seconds", values)
        if len(values) != self.max_attempts - 1:
            raise ValueError("backoff_seconds must cover every retry")
        if any(value < 0 for value in values):
            raise ValueError("backoff_seconds cannot be negative")
        return self


class Bitrix24DiskService(Protocol):
    async def authorize_read(
        self,
        connection: ProviderConnection,
        resource_scope_reference: OpaqueProviderReference,
        *,
        file_id: str,
        acting_principal_id: UUID,
    ) -> None: ...

    async def list_children(
        self,
        connection: ProviderConnection,
        resource_scope_reference: OpaqueProviderReference,
        *,
        start: int,
        page_size: int,
        acting_principal_id: UUID,
    ) -> Bitrix24DiskFilePage: ...

    async def get_file(
        self,
        connection: ProviderConnection,
        resource_scope_reference: OpaqueProviderReference,
        *,
        file_id: str,
        acting_principal_id: UUID,
    ) -> Bitrix24DiskFile: ...

    async def download(
        self,
        connection: ProviderConnection,
        resource_scope_reference: OpaqueProviderReference,
        *,
        file_id: str,
        download_url: str,
        max_bytes: int,
        acting_principal_id: UUID,
    ) -> Bitrix24DiskFileContent: ...


class Bitrix24DiskReadCommand(Protocol):
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    resource_scope_reference: OpaqueProviderReference
    authorization_generation: int
    configuration_generation: int
    resource_object_reference: str | None
    required_capabilities: tuple[ProviderCapabilityKind, ...]
    acting_subject_id: UUID | None


def bitrix24_disk_capabilities(
    *,
    qualification_reference: OpaqueProviderReference,
    crm_qualification_reference: OpaqueProviderReference | None = None,
) -> tuple[ProviderCapability, ...]:
    """Declare only the guarantees qualified by the bounded Disk reader."""

    supported = (
        ProviderCapabilityKind.ENUMERATION,
        ProviderCapabilityKind.METADATA_READ,
        ProviderCapabilityKind.BINARY_READ,
        ProviderCapabilityKind.STABLE_IDENTITY,
    )
    unsupported = (
        ProviderCapabilityKind.TABULAR_READ,
        ProviderCapabilityKind.INCREMENTAL_CHANGE_DISCOVERY,
        ProviderCapabilityKind.FULL_RECONCILIATION,
        ProviderCapabilityKind.LIFECYCLE_EVIDENCE,
        ProviderCapabilityKind.CRM,
    )
    crm = (
        (
            ProviderCapability(
                kind=ProviderCapabilityKind.CRM,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=crm_qualification_reference,
            ),
            ProviderCapability(
                kind=ProviderCapabilityKind.STRUCTURED_READ,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=crm_qualification_reference,
            ),
        )
        if crm_qualification_reference is not None
        else (
            ProviderCapability(
                kind=ProviderCapabilityKind.CRM,
                status=ProviderCapabilityStatus.UNQUALIFIED,
            ),
            ProviderCapability(
                kind=ProviderCapabilityKind.STRUCTURED_READ,
                status=ProviderCapabilityStatus.UNSUPPORTED,
            ),
        )
    )
    return (
        tuple(
            ProviderCapability(
                kind=kind,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=qualification_reference,
            )
            for kind in supported
        )
        + tuple(
            ProviderCapability(kind=kind, status=ProviderCapabilityStatus.UNSUPPORTED)
            for kind in unsupported
            if kind is not ProviderCapabilityKind.CRM
        )
        + (
            ProviderCapability(
                kind=ProviderCapabilityKind.HEALTH_DIAGNOSTICS,
                status=ProviderCapabilityStatus.UNQUALIFIED,
            ),
        )
        + crm
    )


def _default_retry_jitter(backoff: float) -> float:
    return random.SystemRandom().uniform(0.0, backoff)


class Bitrix24DiskProviderAdapter:
    """Read one file from an approved Disk folder and emit one exact observation."""

    def __init__(
        self,
        *,
        service: Bitrix24DiskService,
        profile: Bitrix24DiskReadProfile,
        clock: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]],
        jitter: Callable[[float], float] = _default_retry_jitter,
    ) -> None:
        self._service = service
        self._profile = profile
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter

    async def read(
        self, connection: ProviderConnection, command: Bitrix24DiskReadCommand
    ) -> NormalizedExternalObservationV2:
        if connection.provider_kind is not ProviderKind.BITRIX24:
            raise Bitrix24DiskContractError()
        if command.resource_object_reference is None:
            raise Bitrix24DiskResourceNotFoundError()
        if set(command.required_capabilities) != set(
            BITRIX24_DISK_REQUIRED_CAPABILITIES
        ):
            raise Bitrix24DiskContractError()
        if any(
            not connection.supports(capability)
            for capability in BITRIX24_DISK_REQUIRED_CAPABILITIES
        ):
            raise Bitrix24DiskContractError()
        acting_principal_id = (
            command.acting_subject_id or connection.service_principal_id
        )
        if acting_principal_id.int == 0:
            raise Bitrix24DiskContractError()
        for attempt in range(1, self._profile.max_attempts + 1):
            try:
                await self._service.authorize_read(
                    connection,
                    command.resource_scope_reference,
                    file_id=command.resource_object_reference,
                    acting_principal_id=acting_principal_id,
                )
                return await self._read_once(connection, command, acting_principal_id)
            except (
                Bitrix24DiskThrottleError,
                Bitrix24DiskOverloadError,
                Bitrix24DiskTransientError,
            ) as exc:
                if attempt == self._profile.max_attempts:
                    raise
                if self._profile.backoff_seconds is None:
                    raise AssertionError("validated backoff schedule is missing")
                backoff = self._profile.backoff_seconds[attempt - 1]
                provider_delay = getattr(exc, "retry_after_seconds", 0.0)
                jitter = self._jitter(backoff)
                if jitter < 0:
                    raise ValueError("Bitrix24 Disk retry jitter cannot be negative")
                await self._sleep(max(backoff, provider_delay) + jitter)
        raise AssertionError("bounded Bitrix24 Disk retry loop did not terminate")

    async def _read_once(
        self,
        connection: ProviderConnection,
        command: Bitrix24DiskReadCommand,
        acting_principal_id: UUID,
    ) -> NormalizedExternalObservationV2:
        target = command.resource_object_reference
        if target is None:
            raise Bitrix24DiskResourceNotFoundError()
        listed = await self._find_file(connection, command, target, acting_principal_id)
        metadata = await self._service.get_file(
            connection,
            command.resource_scope_reference,
            file_id=target,
            acting_principal_id=acting_principal_id,
        )
        if metadata.id != target or metadata.type != "file":
            raise Bitrix24DiskResourceNotFoundError()
        if metadata.parent_id != listed.parent_id:
            raise Bitrix24DiskContractError()
        if metadata.size is None or metadata.size > self._profile.max_file_bytes:
            raise Bitrix24DiskFileTooLargeError()
        revision_evidence = _revision_evidence(metadata)
        content, refreshed_metadata = await self._download_with_refresh(
            connection, command, metadata, acting_principal_id
        )
        self._validate_content(refreshed_metadata, content)
        after = await self._service.get_file(
            connection,
            command.resource_scope_reference,
            file_id=target,
            acting_principal_id=acting_principal_id,
        )
        if _revision_evidence(after) != revision_evidence:
            raise Bitrix24DiskConcurrentModificationError()
        observation_evidence = revision_evidence.model_copy(
            update={"content_sha256": hashlib.sha256(content.content).hexdigest()}
        )
        return self._observation(
            connection, command, refreshed_metadata, content, observation_evidence
        )

    async def _find_file(
        self,
        connection: ProviderConnection,
        command: Bitrix24DiskReadCommand,
        target: str,
        acting_principal_id: UUID,
    ) -> Bitrix24DiskFile:
        start = 0
        pages = 0
        listed_items = 0
        seen: dict[str, Bitrix24DiskFile] = {}
        while True:
            pages += 1
            if pages > self._profile.max_pages:
                raise Bitrix24DiskContractError()
            page = await self._service.list_children(
                connection,
                command.resource_scope_reference,
                start=start,
                page_size=self._profile.page_size,
                acting_principal_id=acting_principal_id,
            )
            if (
                len(page.items) > self._profile.page_size
                or page.total > self._profile.max_items
            ):
                raise Bitrix24DiskContractError()
            listed_items += len(page.items)
            if listed_items > self._profile.max_items:
                raise Bitrix24DiskContractError()
            for item in page.items:
                previous = seen.get(item.id)
                if previous is not None and previous != item:
                    raise Bitrix24DiskContractError()
                seen[item.id] = item
            next_start = start + self._profile.page_size
            if next_start >= page.total:
                break
            if not page.items or next_start <= start:
                raise Bitrix24DiskContractError()
            start = next_start
        try:
            item = seen[target]
        except KeyError:
            raise Bitrix24DiskResourceNotFoundError() from None
        if item.type != "file":
            raise Bitrix24DiskResourceNotFoundError()
        return item

    async def _download_with_refresh(
        self,
        connection: ProviderConnection,
        command: Bitrix24DiskReadCommand,
        metadata: Bitrix24DiskFile,
        acting_principal_id: UUID,
    ) -> tuple[Bitrix24DiskFileContent, Bitrix24DiskFile]:
        current = metadata
        for refresh in range(self._profile.max_download_refreshes + 1):
            if current.download_url is None:
                raise Bitrix24DiskContractError()
            try:
                content = await self._service.download(
                    connection,
                    command.resource_scope_reference,
                    file_id=current.id,
                    download_url=current.download_url,
                    max_bytes=self._profile.max_file_bytes,
                    acting_principal_id=acting_principal_id,
                )
                return content, current
            except Bitrix24DiskDownloadLinkExpiredError:
                if refresh == self._profile.max_download_refreshes:
                    raise
                current = await self._service.get_file(
                    connection,
                    command.resource_scope_reference,
                    file_id=current.id,
                    acting_principal_id=acting_principal_id,
                )
                if current.id != metadata.id or current.type != "file":
                    raise Bitrix24DiskResourceNotFoundError()
        raise AssertionError("bounded Bitrix24 Disk download refresh did not terminate")

    def _observation(
        self,
        connection: ProviderConnection,
        command: Bitrix24DiskReadCommand,
        metadata: Bitrix24DiskFile,
        content: Bitrix24DiskFileContent,
        revision_evidence: ProviderFileRevisionEvidence,
    ) -> NormalizedExternalObservationV2:
        observed_at = self._clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("Bitrix24 Disk reader clock must be timezone-aware")
        event_digest = hashlib.sha256(
            json.dumps(
                revision_evidence.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        identity_digest = hashlib.sha256(
            f"{connection.connection_id}|{metadata.id}".encode()
        ).hexdigest()
        object_digest = hashlib.sha256(metadata.id.encode()).hexdigest()
        return NormalizedExternalObservationV2(
            workspace_id=connection.workspace_id,
            environment_id=connection.environment_id,
            connection_id=connection.connection_id,
            connection_version_id=connection.connection_version_id,
            provider_kind=ProviderKind.BITRIX24,
            resource_scope_reference=command.resource_scope_reference,
            authorization_generation=command.authorization_generation,
            configuration_generation=command.configuration_generation,
            source_kind="document",
            external_identity=ProviderExternalIdentity(
                external_namespace=BITRIX24_DISK_EXTERNAL_NAMESPACE,
                external_generation=BITRIX24_DISK_EXTERNAL_GENERATION,
                external_object_id=f"b24:{identity_digest[:60]}",
                provider_key=ProviderFileKey(
                    connection_id=connection.connection_id,
                    provider_object_id=metadata.id,
                ),
                assurance=ExternalIdentityAssurance.PROVIDER_STABLE,
                assurance_evidence_reference=BITRIX24_DISK_EXTERNAL_GENERATION,
            ),
            representation=BinaryObservationRepresentation(
                original_bytes=content.content,
                media_type=content.media_type,
                extraction_profile=BITRIX24_DISK_EXTRACTION_PROFILE,
                locator_contract_version=BITRIX24_DISK_LOCATOR_CONTRACT,
                revision_evidence=revision_evidence,
            ),
            coverage=ConnectorCoverage.COMPLETE,
            discovery=ProviderObservationDiscovery.SNAPSHOT,
            producer_reference="bitrix24-disk-reader:v1",
            event_identity=f"bitrix24-disk:{event_digest[:47]}",
            event_digest=event_digest,
            origin_locator_kind="bitrix24-disk-file-id",
            origin_locator_value=f"file:{object_digest[:48]}",
            origin_locator_schema=BITRIX24_DISK_LOCATOR_CONTRACT,
            origin_locator_digest=object_digest,
            observer_service="connector-worker",
            received_at=observed_at,
            observed_at=observed_at,
            provider_media_type=content.media_type,
            provider_version_before=metadata.global_content_version,
            provider_version_after=metadata.global_content_version,
        )

    @staticmethod
    def _validate_content(
        metadata: Bitrix24DiskFile, content: Bitrix24DiskFileContent
    ) -> None:
        if metadata.size != len(content.content):
            raise Bitrix24DiskPartialDownloadError()


def _revision_evidence(file: Bitrix24DiskFile) -> ProviderFileRevisionEvidence:
    if (
        file.type != "file"
        or file.global_content_version is None
        or file.size is None
        or file.update_time is None
    ):
        raise Bitrix24DiskContractError()
    return ProviderFileRevisionEvidence(
        provider_revision=file.global_content_version,
        provider_resource_id=file.id,
        resource_name=file.name,
        resource_path=None,
        content_sha256=None,
        provider_payload_digest=_provider_payload_digest(file),
        provider_payload=_provider_payload(file),
        byte_length=file.size,
        modified_at=file.update_time,
    )


def _provider_payload_digest(file: Bitrix24DiskFile) -> str:
    """Digest safe provider metadata while excluding the ephemeral signed URL."""

    payload = _provider_payload(file)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _provider_payload(file: Bitrix24DiskFile) -> dict[str, str | int | None]:
    """Retain a bounded raw metadata snapshot without credentials or signed URLs."""

    payload = file.model_dump(mode="json", by_alias=True, exclude={"download_url"})
    return payload


__all__ = [
    "Bitrix24DiskAuthorizationRevokedError",
    "Bitrix24DiskConcurrentModificationError",
    "Bitrix24DiskContractError",
    "Bitrix24DiskDownloadLinkExpiredError",
    "Bitrix24DiskFile",
    "Bitrix24DiskFileContent",
    "Bitrix24DiskFilePage",
    "Bitrix24DiskFileTooLargeError",
    "Bitrix24DiskOverloadError",
    "Bitrix24DiskPartialDownloadError",
    "Bitrix24DiskPermissionLostError",
    "Bitrix24DiskProviderAdapter",
    "Bitrix24DiskReadError",
    "Bitrix24DiskReadProfile",
    "Bitrix24DiskResourceNotFoundError",
    "Bitrix24DiskService",
    "Bitrix24DiskThrottleError",
    "Bitrix24DiskTransientError",
    "BITRIX24_DISK_REQUIRED_CAPABILITIES",
    "bitrix24_disk_capabilities",
]
