"""Governed Yandex Disk file reader for the provider-neutral source boundary."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import base64
import binascii
from datetime import datetime
import hashlib
import json
import random
from typing import Literal, Protocol, TypeVar
from uuid import UUID

from pydantic import ConfigDict, Field, StrictBytes, StrictFloat, StrictInt, model_validator

from spine.application.yandex_disk_scan import (
    YandexDiskAuthorizationRevokedError,
    YandexDiskConcurrentModificationError,
    YandexDiskContractError,
    YandexDiskFileTooLargeError,
    YandexDiskPartialDownloadError,
    YandexDiskPermissionLostError,
    YandexDiskReadError,
    YandexDiskResourceNotFoundError,
    YandexDiskScanItem,
    YandexDiskScanPage,
    YandexDiskScopeOutOfScopeError,
    YandexDiskThrottleError,
    YandexDiskTransientError,
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


_ResultT = TypeVar("_ResultT")


YANDEX_DISK_EXTERNAL_NAMESPACE = "yandex-disk"
YANDEX_DISK_EXTERNAL_GENERATION = "yandex-disk-path:v1"
YANDEX_DISK_EXTRACTION_PROFILE = "yandex-disk-binary:v1"
YANDEX_DISK_LOCATOR_CONTRACT = "locator:yandex-disk-path:v1"


class YandexDiskResource(DefinitionModel):
    """Official Resource response fields used by the immutable read guard."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    name: str = Field(min_length=1, max_length=1024)
    path: str = Field(min_length=1, max_length=2048)
    type: Literal["file", "dir"]
    mime_type: str | None = Field(default=None, min_length=1, max_length=255)
    size: StrictInt | None = Field(default=None, ge=0)
    md5: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    revision: str | None = Field(default=None, min_length=1, max_length=128)
    modified: datetime | None = None
    resource_id: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def validate_file_revision_evidence(self) -> "YandexDiskResource":
        if self.type == "file" and (
            self.mime_type is None
            or self.size is None
            or self.revision is None
            or self.modified is None
            or (self.sha256 is None and self.md5 is None)
        ):
            raise ValueError("Yandex Disk file metadata is incomplete")
        if (
            self.modified is not None
            and (self.modified.tzinfo is None or self.modified.utcoffset() is None)
        ):
            raise ValueError("Yandex Disk modified timestamp must be timezone-aware")
        return self


class YandexDiskEmbeddedResources(DefinitionModel):
    path: str = Field(min_length=1, max_length=2048)
    items: tuple[YandexDiskResource, ...]
    limit: StrictInt = Field(ge=1, le=1000)
    offset: StrictInt = Field(ge=0)
    total: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def validate_page_size(self) -> "YandexDiskEmbeddedResources":
        if len(self.items) > self.limit:
            raise ValueError("Yandex Disk page exceeds its declared limit")
        return self


class YandexDiskResourcePage(DefinitionModel):
    """Official folder response shape containing the `_embedded` page."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    embedded: YandexDiskEmbeddedResources = Field(alias="_embedded")


class YandexDiskFileContent(DefinitionModel):
    content: StrictBytes
    media_type: str = Field(min_length=1, max_length=255)


class YandexDiskReadProfile(DefinitionModel):
    page_size: StrictInt = Field(default=100, ge=1, le=1000)
    max_attempts: StrictInt = Field(default=2, ge=1, le=5)
    max_file_bytes: StrictInt = Field(default=32 * 1024 * 1024, ge=1)
    max_pages: StrictInt = Field(default=100, ge=1, le=1000)
    max_items: StrictInt = Field(default=10_000, ge=1, le=1_000_000)
    backoff_seconds: tuple[StrictFloat, ...] | None = None

    @model_validator(mode="after")
    def validate_backoff(self) -> "YandexDiskReadProfile":
        values = self.backoff_seconds
        if values is None:
            values = tuple(0.25 * (2**attempt) for attempt in range(self.max_attempts - 1))
            object.__setattr__(self, "backoff_seconds", values)
        if len(values) != self.max_attempts - 1:
            raise ValueError("backoff_seconds must cover every retry")
        if any(value < 0 for value in values):
            raise ValueError("backoff_seconds cannot be negative")
        return self


class YandexDiskService(Protocol):
    async def list_resources(
        self,
        connection: ProviderConnection,
        resource_scope_reference: OpaqueProviderReference,
        *,
        offset: int,
        limit: int,
    ) -> YandexDiskResourcePage: ...

    async def download(
        self,
        connection: ProviderConnection,
        resource_scope_reference: OpaqueProviderReference,
        *,
        path: str,
        revision: str,
        max_bytes: int,
    ) -> YandexDiskFileContent: ...

    async def get_resource(
        self,
        connection: ProviderConnection,
        resource_scope_reference: OpaqueProviderReference,
        *,
        path: str,
    ) -> YandexDiskResource: ...


class YandexDiskReadCommand(Protocol):
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    resource_scope_reference: OpaqueProviderReference
    authorization_generation: int
    configuration_generation: int
    resource_object_reference: str | None
    external_identity_occurrence: str | None


def yandex_disk_capabilities(
    *, qualification_reference: OpaqueProviderReference
) -> tuple[ProviderCapability, ...]:
    """Declare only the guarantees qualified by the bounded file reader."""

    supported = (
        ProviderCapabilityKind.ENUMERATION,
        ProviderCapabilityKind.METADATA_READ,
        ProviderCapabilityKind.BINARY_READ,
        ProviderCapabilityKind.FULL_RECONCILIATION,
    )
    unsupported = (
        ProviderCapabilityKind.TABULAR_READ,
        ProviderCapabilityKind.STRUCTURED_READ,
        ProviderCapabilityKind.STABLE_IDENTITY,
        ProviderCapabilityKind.INCREMENTAL_CHANGE_DISCOVERY,
        ProviderCapabilityKind.LIFECYCLE_EVIDENCE,
        ProviderCapabilityKind.CRM,
    )
    return tuple(
        ProviderCapability(
            kind=kind,
            status=ProviderCapabilityStatus.SUPPORTED,
            qualification_reference=qualification_reference,
        )
        for kind in supported
    ) + tuple(
        ProviderCapability(kind=kind, status=ProviderCapabilityStatus.UNSUPPORTED)
        for kind in unsupported
    ) + (
        ProviderCapability(
            kind=ProviderCapabilityKind.HEALTH_DIAGNOSTICS,
            status=ProviderCapabilityStatus.UNQUALIFIED,
        ),
    )


def _default_retry_jitter(backoff: float) -> float:
    return random.SystemRandom().uniform(0.0, backoff)


class YandexDiskProviderAdapter:
    """Read one path from a bounded listing and emit one immutable observation."""

    def __init__(
        self,
        *,
        service: YandexDiskService,
        profile: YandexDiskReadProfile,
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
        self, connection: ProviderConnection, command: YandexDiskReadCommand
    ) -> NormalizedExternalObservationV2:
        if connection.provider_kind is not ProviderKind.YANDEX_DISK:
            raise YandexDiskContractError()
        if command.resource_object_reference is None:
            raise YandexDiskResourceNotFoundError()
        return await self._retry(lambda: self._read_once(connection, command))

    async def scan_page(
        self,
        connection: ProviderConnection,
        resource_scope_reference: OpaqueProviderReference,
        *,
        checkpoint: str | None,
    ) -> YandexDiskScanPage:
        """Enumerate one bounded page while keeping offset state opaque to callers."""

        state = _decode_scan_checkpoint(checkpoint)
        page = await self._retry(
            lambda: self._service.list_resources(
                    connection,
                    resource_scope_reference,
                    offset=state["offset"],
                    limit=self._profile.page_size,
            )
        )

        embedded = page.embedded
        if (
            embedded.offset != state["offset"]
            or embedded.limit != self._profile.page_size
            or embedded.total > self._profile.max_items
            or (
                state["total"] is not None
                and embedded.total != state["total"]
            )
        ):
            raise YandexDiskContractError()
        page_number = state["pages"] + 1
        listed_items = state["items"] + len(embedded.items)
        if page_number > self._profile.max_pages or listed_items > self._profile.max_items:
            raise YandexDiskContractError()
        next_offset = embedded.offset + len(embedded.items)
        if next_offset < embedded.total and next_offset <= embedded.offset:
            raise YandexDiskContractError()
        unique_files: dict[str, YandexDiskScanItem] = {}
        coverage_entry_digests: list[str] = []
        generation = state["generation"]
        if not isinstance(generation, str):
            raise YandexDiskContractError()
        for item in embedded.items:
            coverage_entry_digests.append(
                hashlib.sha256(f"{item.type}\0{item.path}".encode()).hexdigest()
            )
            generation = _advance_scan_generation(generation, item)
            if item.type != "file" or item.revision is None:
                continue
            candidate = YandexDiskScanItem(
                provider_object_reference=item.path,
                provider_revision=item.revision,
            )
            previous = unique_files.get(item.path)
            if previous is not None and previous != candidate:
                raise YandexDiskContractError()
            unique_files[item.path] = candidate
        files = tuple(unique_files.values())
        terminal = next_offset >= embedded.total
        next_state = {
            "items": listed_items,
            "generation": generation,
            "offset": next_offset,
            "pages": page_number,
            "schema": "yandex-disk-scan-checkpoint:v2",
            "terminal": terminal,
            "total": embedded.total,
        }
        covered_checkpoint = _encode_scan_checkpoint(next_state)
        page_digest = hashlib.sha256(
            json.dumps(
                {
                    "checkpoint": covered_checkpoint,
                    "items": [item.model_dump(mode="json") for item in files],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        confirmed_generation = None
        if terminal:
            confirmed_generation = await self._confirm_scan_generation(
                connection,
                resource_scope_reference,
                expected_generation=generation,
                expected_total=embedded.total,
            )
        return YandexDiskScanPage(
            page_identity=f"yandex-page:{page_digest[:48]}",
            requested_checkpoint=checkpoint,
            checkpoint=covered_checkpoint,
            next_checkpoint=None if terminal else covered_checkpoint,
            items=files,
            coverage_entry_digests=tuple(coverage_entry_digests),
            expected_entry_count=embedded.total,
            confirmed_scan_generation=confirmed_generation,
        )

    async def _confirm_scan_generation(
        self,
        connection: ProviderConnection,
        resource_scope_reference: OpaqueProviderReference,
        *,
        expected_generation: str,
        expected_total: int,
    ) -> str:
        """Repeat the complete inventory and fence absence to one stable generation."""

        offset = 0
        pages = 0
        generation = "0" * 64
        while True:
            pages += 1
            if pages > self._profile.max_pages:
                raise YandexDiskContractError()
            page = await self._retry(
                lambda: self._service.list_resources(
                    connection,
                    resource_scope_reference,
                    offset=offset,
                    limit=self._profile.page_size,
                )
            )
            embedded = page.embedded
            if (
                embedded.offset != offset
                or embedded.limit != self._profile.page_size
                or embedded.total != expected_total
                or embedded.total > self._profile.max_items
            ):
                raise YandexDiskContractError()
            for item in embedded.items:
                generation = _advance_scan_generation(generation, item)
            next_offset = offset + len(embedded.items)
            if next_offset >= expected_total:
                break
            if next_offset <= offset:
                raise YandexDiskContractError()
            offset = next_offset
        if next_offset != expected_total or generation != expected_generation:
            raise YandexDiskContractError()
        return generation

    async def _retry(self, operation: Callable[[], Awaitable[_ResultT]]) -> _ResultT:
        for attempt in range(1, self._profile.max_attempts + 1):
            try:
                return await operation()
            except (YandexDiskThrottleError, YandexDiskTransientError) as exc:
                if attempt == self._profile.max_attempts:
                    raise
                if self._profile.backoff_seconds is None:
                    raise AssertionError("validated backoff schedule is missing")
                backoff = self._profile.backoff_seconds[attempt - 1]
                provider_delay = (
                    exc.retry_after_seconds
                    if isinstance(exc, YandexDiskThrottleError)
                    else 0.0
                )
                jitter = self._jitter(backoff)
                if jitter < 0:
                    raise ValueError("Yandex Disk retry jitter cannot be negative")
                await self._sleep(max(backoff, provider_delay) + jitter)
        raise AssertionError("bounded Yandex Disk retry loop did not terminate")

    async def _read_once(
        self, connection: ProviderConnection, command: YandexDiskReadCommand
    ) -> NormalizedExternalObservationV2:
        metadata = await self._find_resource(connection, command)
        if metadata.type != "file" or metadata.revision is None:
            raise YandexDiskResourceNotFoundError()
        if metadata.size is None or metadata.size > self._profile.max_file_bytes:
            raise YandexDiskFileTooLargeError()
        revision_evidence = _revision_evidence(metadata)
        content = await self._service.download(
            connection,
            command.resource_scope_reference,
            path=metadata.path,
            revision=metadata.revision,
            max_bytes=self._profile.max_file_bytes,
        )
        self._validate_content(metadata, content)
        after = await self._service.get_resource(
            connection,
            command.resource_scope_reference,
            path=metadata.path,
        )
        if _revision_evidence(after) != revision_evidence:
            raise YandexDiskConcurrentModificationError()
        return self._observation(
            connection, command, metadata, content, revision_evidence
        )

    async def _find_resource(
        self, connection: ProviderConnection, command: YandexDiskReadCommand
    ) -> YandexDiskResource:
        target = command.resource_object_reference
        offset = 0
        pages = 0
        listed_items = 0
        seen: dict[str, YandexDiskResource] = {}
        while True:
            pages += 1
            if pages > self._profile.max_pages:
                raise YandexDiskContractError()
            page = await self._service.list_resources(
                connection,
                command.resource_scope_reference,
                offset=offset,
                limit=self._profile.page_size,
            )
            embedded = page.embedded
            if embedded.offset != offset or embedded.limit != self._profile.page_size:
                raise YandexDiskContractError()
            listed_items += len(embedded.items)
            if (
                embedded.total > self._profile.max_items
                or listed_items > self._profile.max_items
            ):
                raise YandexDiskContractError()
            for item in embedded.items:
                previous = seen.get(item.path)
                if previous is not None and previous != item:
                    raise YandexDiskContractError()
                seen[item.path] = item
            next_offset = offset + len(embedded.items)
            if next_offset >= embedded.total:
                break
            if next_offset <= offset:
                raise YandexDiskContractError()
            offset = next_offset
        try:
            return seen[target]
        except KeyError:
            raise YandexDiskResourceNotFoundError() from None

    @staticmethod
    def _validate_content(
        metadata: YandexDiskResource, content: YandexDiskFileContent
    ) -> None:
        valid = (
            metadata.size == len(content.content)
            and metadata.mime_type == content.media_type
        )
        if metadata.sha256 is not None:
            valid = (
                valid
                and hashlib.sha256(content.content).hexdigest() == metadata.sha256
            )
        if metadata.md5 is not None:
            valid = valid and hashlib.md5(  # noqa: S324
                content.content, usedforsecurity=False
            ).hexdigest() == metadata.md5
        if not valid:
            raise YandexDiskPartialDownloadError()

    def _observation(
        self,
        connection: ProviderConnection,
        command: YandexDiskReadCommand,
        metadata: YandexDiskResource,
        content: YandexDiskFileContent,
        revision_evidence: ProviderFileRevisionEvidence,
    ) -> NormalizedExternalObservationV2:
        if metadata.revision is None or metadata.size is None or metadata.modified is None:
            raise YandexDiskContractError()
        observed_at = self._clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("Yandex Disk reader clock must be timezone-aware")
        identity_material = f"{connection.connection_id}|{metadata.path}"
        if command.external_identity_occurrence is not None:
            identity_material = (
                f"{identity_material}|occurrence:{command.external_identity_occurrence}"
            )
        identity_digest = hashlib.sha256(identity_material.encode()).hexdigest()
        event_digest = hashlib.sha256(
            json.dumps(
                revision_evidence.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return NormalizedExternalObservationV2(
            workspace_id=connection.workspace_id,
            environment_id=connection.environment_id,
            connection_id=connection.connection_id,
            connection_version_id=connection.connection_version_id,
            provider_kind=ProviderKind.YANDEX_DISK,
            resource_scope_reference=command.resource_scope_reference,
            authorization_generation=command.authorization_generation,
            configuration_generation=command.configuration_generation,
            source_kind="document",
            external_identity=ProviderExternalIdentity(
                external_namespace=YANDEX_DISK_EXTERNAL_NAMESPACE,
                external_generation=YANDEX_DISK_EXTERNAL_GENERATION,
                external_object_id=f"ydx:{identity_digest[:60]}",
                provider_key=ProviderFileKey(
                    connection_id=connection.connection_id,
                    provider_object_id=metadata.path,
                ),
                assurance=ExternalIdentityAssurance.PATH_SCOPED,
                assurance_evidence_reference=YANDEX_DISK_EXTERNAL_GENERATION,
            ),
            representation=BinaryObservationRepresentation(
                original_bytes=content.content,
                media_type=content.media_type,
                extraction_profile=YANDEX_DISK_EXTRACTION_PROFILE,
                locator_contract_version=YANDEX_DISK_LOCATOR_CONTRACT,
                revision_evidence=revision_evidence,
            ),
            coverage=ConnectorCoverage.COMPLETE,
            discovery=ProviderObservationDiscovery.SNAPSHOT,
            producer_reference="yandex-disk-reader:v1",
            event_identity=f"yandex-disk:{event_digest[:48]}",
            event_digest=event_digest,
            origin_locator_kind="yandex-disk-path",
            origin_locator_value=f"path:{hashlib.sha256(metadata.path.encode()).hexdigest()[:48]}",
            origin_locator_schema=YANDEX_DISK_LOCATOR_CONTRACT,
            origin_locator_digest=hashlib.sha256(metadata.path.encode()).hexdigest(),
            observer_service="connector-worker",
            received_at=observed_at,
            observed_at=observed_at,
            provider_media_type=metadata.mime_type,
            provider_version_before=metadata.revision,
            provider_version_after=metadata.revision,
        )


def _revision_evidence(resource: YandexDiskResource) -> ProviderFileRevisionEvidence:
    if resource.revision is None or resource.size is None or resource.modified is None:
        raise YandexDiskContractError()
    return ProviderFileRevisionEvidence(
        provider_revision=resource.revision,
        provider_resource_id=resource.resource_id,
        resource_name=resource.name,
        resource_path=resource.path,
        content_md5=resource.md5,
        content_sha256=resource.sha256,
        byte_length=resource.size,
        modified_at=resource.modified,
    )


def _advance_scan_generation(generation: str, item: YandexDiskResource) -> str:
    resource = json.dumps(
        item.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(f"{generation}\0{resource}".encode()).hexdigest()


def _encode_scan_checkpoint(state: dict[str, object]) -> str:
    payload = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    digest = hashlib.sha256(payload).hexdigest()[:32]
    return f"ydx1.{encoded}.{digest}"


def _decode_scan_checkpoint(checkpoint: str | None) -> dict[str, object]:
    if checkpoint is None:
        return {
            "generation": "0" * 64,
            "items": 0,
            "offset": 0,
            "pages": 0,
            "schema": "yandex-disk-scan-checkpoint:v2",
            "terminal": False,
            "total": None,
        }
    try:
        prefix, encoded, digest = checkpoint.split(".", 2)
        padding = "=" * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode(encoded + padding)
        value = json.loads(payload)
    except (ValueError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
        raise YandexDiskContractError() from exc
    expected_keys = {
        "generation",
        "items",
        "offset",
        "pages",
        "schema",
        "terminal",
        "total",
    }
    valid = (
        prefix == "ydx1"
        and hashlib.sha256(payload).hexdigest()[:32] == digest
        and isinstance(value, dict)
        and set(value) == expected_keys
        and value["schema"] == "yandex-disk-scan-checkpoint:v2"
        and value["terminal"] is False
        and type(value["items"]) is int
        and type(value["offset"]) is int
        and type(value["pages"]) is int
        and isinstance(value["generation"], str)
        and len(value["generation"]) == 64
        and (value["total"] is None or type(value["total"]) is int)
        and value["items"] >= 0
        and value["offset"] >= 0
        and value["pages"] >= 0
    )
    if not valid:
        raise YandexDiskContractError()
    return value


__all__ = [
    "YandexDiskConcurrentModificationError",
    "YandexDiskAuthorizationRevokedError",
    "YandexDiskContractError",
    "YandexDiskFileContent",
    "YandexDiskFileTooLargeError",
    "YandexDiskPartialDownloadError",
    "YandexDiskPermissionLostError",
    "YandexDiskProviderAdapter",
    "YandexDiskReadError",
    "YandexDiskReadProfile",
    "YandexDiskResource",
    "YandexDiskResourceNotFoundError",
    "YandexDiskResourcePage",
    "YandexDiskScopeOutOfScopeError",
    "YandexDiskScanItem",
    "YandexDiskScanPage",
    "YandexDiskService",
    "YandexDiskThrottleError",
    "YandexDiskTransientError",
    "yandex_disk_capabilities",
]
