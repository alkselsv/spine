"""Concrete Google Drive and Sheets REST adapter behind governed reader ports.

The public interface is the existing Drive/Sheets service protocols.  OAuth,
HTTP, retry policy and provider payload mapping remain implementation details in
this connector module and never cross into canonical Spine records.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import random
import re
from contextvars import ContextVar, Token
from typing import Any, Literal, Protocol
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
from pydantic import (
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from spine.application.connections.google import (
    GoogleProviderAuthorizationError,
    GoogleProviderContractError,
    GoogleProviderNotFoundError,
    GoogleProviderPolicyError,
    GoogleProviderQuotaError,
    GoogleProviderReadError,
)
from spine.connectors.google_drive import (
    DriveChange,
    DriveChangePage,
    DriveFileContent,
    DriveFileMetadata,
    DriveSnapshotItem,
    DriveSnapshotPage,
    GoogleDriveAccessOutcomeError,
)
from spine.connectors.google_sheets import (
    GoogleSheetsCellData,
    GoogleSheetsConfiguredScopeError,
    GoogleSheetsDeveloperMetadata,
    GoogleSheetsRangePage,
    GoogleSheetsReadProfile,
    GoogleSheetsRowData,
    GoogleSheetsSnapshot,
)
from spine.domain.common import DefinitionModel
from spine.domain.connections.google import GoogleConnection, OpaqueGoogleReference
from spine.domain.sources.google import (
    GoogleDriveLifecycleSignal,
    GoogleSheetCellValue,
    GoogleSheetNumberFormat,
)


class GoogleApiEndpoints(DefinitionModel):
    """Approved provider endpoints and version evidence for one composition."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    drive_base_url: StrictStr = Field(min_length=1, max_length=2048)
    sheets_base_url: StrictStr = Field(min_length=1, max_length=2048)
    oauth_token_url: StrictStr = Field(min_length=1, max_length=2048)
    approved_oauth_scopes: tuple[StrictStr, ...] = Field(min_length=1)
    provider_api_version: StrictStr = Field(min_length=1, max_length=128)
    configuration_version: StrictStr = Field(min_length=1, max_length=128)

    @field_validator("drive_base_url", "sheets_base_url", "oauth_token_url")
    @classmethod
    def validate_https_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Google provider endpoints must be absolute HTTPS URLs")
        return value.rstrip("/")

    @field_validator("approved_oauth_scopes")
    @classmethod
    def validate_oauth_scopes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("approved OAuth scopes must be unique")
        if any(
            urlsplit(value).scheme != "https" or not urlsplit(value).hostname
            for value in values
        ):
            raise ValueError("approved OAuth scopes must be absolute HTTPS identifiers")
        return tuple(sorted(values))


class GoogleApiRequestPolicy(DefinitionModel):
    """Explicit approved timeout and bounded-retry configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    timeout_seconds: StrictFloat = Field(gt=0, le=30)
    max_attempts: StrictInt = Field(ge=1, le=4)
    max_requests_per_run: StrictInt = Field(ge=1, le=500)
    max_concurrent_requests: StrictInt = Field(ge=1, le=2)
    backoff_ceiling_seconds: tuple[StrictFloat, ...]
    drive_page_size: StrictInt = Field(ge=1, le=1000)
    sheets_page_size: StrictInt = Field(ge=1, le=4096)

    @model_validator(mode="after")
    def validate_backoff(self) -> "GoogleApiRequestPolicy":
        if not math.isfinite(self.timeout_seconds):
            raise ValueError("timeout_seconds must be finite")
        if len(self.backoff_ceiling_seconds) != self.max_attempts - 1:
            raise ValueError(
                "backoff_ceiling_seconds must define every bounded retry delay"
            )
        approved_ceilings = (1.0, 2.0, 4.0)
        if any(
            not math.isfinite(value)
            or value < 0
            or value > approved_ceilings[index]
            for index, value in enumerate(self.backoff_ceiling_seconds)
        ):
            raise ValueError("retry backoff ceilings exceed the approved policy")
        return self


class GoogleApiRunRequestBudget:
    """Count every physical request attempt in one qualified run."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.requests = 0

    def reserve(self) -> None:
        if self.requests >= self.limit:
            raise GoogleProviderQuotaError()
        self.requests += 1


class GoogleResourceScope(DefinitionModel):
    """Resolved, exact provider resource allowlist for one opaque scope."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    reference: OpaqueGoogleReference
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    authorization_generation: StrictInt = Field(ge=1)
    configuration_generation: StrictInt = Field(ge=1)
    allowed_file_ids: tuple[StrictStr, ...] = ()
    allowed_spreadsheet_ids: tuple[StrictStr, ...] = ()
    drive_id: StrictStr | None = None
    folder_id: StrictStr | None = None
    supports_all_drives: bool = False

    @model_validator(mode="after")
    def validate_allowlist(self) -> "GoogleResourceScope":
        if not self.allowed_file_ids and not self.allowed_spreadsheet_ids:
            raise ValueError(
                "Google resource scope requires an exact resource allowlist"
            )
        if len(set(self.allowed_file_ids)) != len(self.allowed_file_ids):
            raise ValueError("allowed_file_ids must be unique")
        if len(set(self.allowed_spreadsheet_ids)) != len(self.allowed_spreadsheet_ids):
            raise ValueError("allowed_spreadsheet_ids must be unique")
        return self


@dataclass(frozen=True, slots=True, repr=False)
class GoogleDesktopOAuthMaterial:
    """Secret material resolved from opaque references after read admission."""

    client_id: str
    client_secret: str
    refresh_token: str

    def __post_init__(self) -> None:
        if not self.client_id or not self.client_secret or not self.refresh_token:
            raise ValueError("resolved Desktop OAuth material is incomplete")


class GoogleCredentialResolver(Protocol):
    async def resolve(
        self,
        credential_reference: OpaqueGoogleReference,
        grant_reference: OpaqueGoogleReference,
    ) -> GoogleDesktopOAuthMaterial: ...


@dataclass(frozen=True, slots=True, repr=False)
class GoogleOAuthClientCredentials:
    client_id: str
    client_secret: str

    def __post_init__(self) -> None:
        if not self.client_id or not self.client_secret:
            raise ValueError("resolved Google OAuth client credentials are incomplete")


@dataclass(frozen=True, slots=True, repr=False)
class GoogleOAuthRefreshGrant:
    refresh_token: str

    def __post_init__(self) -> None:
        if not self.refresh_token:
            raise ValueError("resolved Google OAuth refresh grant is incomplete")


class GoogleCredentialStore(Protocol):
    async def resolve_client(
        self, credential_reference: OpaqueGoogleReference
    ) -> GoogleOAuthClientCredentials: ...

    async def resolve_grant(
        self, grant_reference: OpaqueGoogleReference
    ) -> GoogleOAuthRefreshGrant: ...


@dataclass(frozen=True, slots=True)
class ReferenceGoogleCredentialResolver:
    """Resolve Desktop OAuth material through two independent opaque references."""

    store: GoogleCredentialStore

    async def resolve(
        self,
        credential_reference: OpaqueGoogleReference,
        grant_reference: OpaqueGoogleReference,
    ) -> GoogleDesktopOAuthMaterial:
        client = await self.store.resolve_client(credential_reference)
        grant = await self.store.resolve_grant(grant_reference)
        return GoogleDesktopOAuthMaterial(
            client_id=client.client_id,
            client_secret=client.client_secret,
            refresh_token=grant.refresh_token,
        )


@dataclass(frozen=True, slots=True, repr=False)
class GoogleApiRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    query: Mapping[str, str]
    form: Mapping[str, str] | None
    timeout_seconds: float
    body_observer: Callable[[bytes], None] | None = None

    def with_body_observer(
        self, observer: Callable[[bytes], None], timeout_seconds: float
    ) -> "GoogleApiRequest":
        from dataclasses import replace

        return replace(
            self,
            body_observer=observer,
            timeout_seconds=min(self.timeout_seconds, timeout_seconds),
        )


@dataclass(frozen=True, slots=True)
class GoogleApiResponse:
    status_code: int
    json_body: Mapping[str, Any] | None = None
    content: bytes = b""
    headers: Mapping[str, str] | None = None
    stream_observed: bool = False


class GoogleApiTransport(Protocol):
    async def request(self, request: GoogleApiRequest) -> GoogleApiResponse: ...


@dataclass(frozen=True, slots=True)
class GoogleApiRetryEvent:
    """Sanitized tenant-scoped evidence for one bounded HTTP retry."""

    schema_version: Literal["google-api-retry:v1"]
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    operation: Literal["oauth", "provider"]
    reason: Literal["quota", "reauthorize", "transient"]
    attempt: int
    max_attempts: int


class GoogleApiRetryObserver(Protocol):
    async def record_retry(self, event: GoogleApiRetryEvent) -> None: ...


@dataclass(frozen=True, slots=True)
class HttpxGoogleApiTransport:
    """Sanitizing transport for the selected, already-declared HTTP client."""

    client: httpx.AsyncClient

    async def request(self, request: GoogleApiRequest) -> GoogleApiResponse:
        try:
            if request.body_observer is None:
                response = await self.client.request(
                    request.method,
                    request.url,
                    headers=dict(request.headers),
                    params=dict(request.query),
                    data=dict(request.form) if request.form is not None else None,
                    timeout=request.timeout_seconds,
                )
                content = response.content
                stream_observed = False
            else:
                chunks: list[bytes] = []
                async with self.client.stream(
                    request.method,
                    request.url,
                    headers=dict(request.headers),
                    params=dict(request.query),
                    data=dict(request.form) if request.form is not None else None,
                    timeout=request.timeout_seconds,
                ) as response:
                    async for chunk in response.aiter_bytes():
                        request.body_observer(chunk)
                        chunks.append(chunk)
                    content = b"".join(chunks)
                stream_observed = True
        except httpx.TimeoutException:
            raise TimeoutError("Google provider request timed out.") from None
        except httpx.RequestError:
            raise GoogleProviderReadError() from None
        content_type = response.headers.get("content-type", "")
        json_body: Mapping[str, Any] | None = None
        if "json" in content_type.lower():
            try:
                candidate = json.loads(content)
            except ValueError:
                candidate = None
            if isinstance(candidate, Mapping):
                json_body = candidate
        return GoogleApiResponse(
            status_code=response.status_code,
            json_body=json_body,
            content=content,
            headers={
                key: response.headers[key]
                for key in ("content-type", "retry-after")
                if key in response.headers
            },
            stream_observed=stream_observed,
        )


class GoogleResourceScopeError(GoogleProviderContractError):
    """A request does not belong to its approved exact resource allowlist."""


@dataclass(slots=True)
class _AccessToken:
    value: str
    expires_at: datetime


def google_api_configuration_fingerprint(
    *,
    endpoints: GoogleApiEndpoints,
    request_policy: GoogleApiRequestPolicy,
    resource_scopes: tuple[GoogleResourceScope, ...],
    sheet_profiles: tuple[GoogleSheetsReadProfile, ...],
) -> str:
    """Digest the complete non-secret effective provider configuration."""

    scopes = sorted(
        (scope.model_dump(mode="json") for scope in resource_scopes),
        key=lambda item: str(item["reference"]),
    )
    profiles = sorted(
        (profile.model_dump(mode="json") for profile in sheet_profiles),
        key=lambda item: (str(item["spreadsheet_id"]), int(item["sheet_id"])),
    )
    encoded = json.dumps(
        {
            "schema": "google-api-configuration:v1",
            "endpoints": endpoints.model_dump(mode="json"),
            "request_policy": request_policy.model_dump(mode="json"),
            "resource_scopes": scopes,
            "sheet_profiles": profiles,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class GoogleApiService:
    """Deep adapter satisfying the existing Drive and Sheets service ports."""

    def __init__(
        self,
        *,
        endpoints: GoogleApiEndpoints,
        request_policy: GoogleApiRequestPolicy,
        resource_scopes: tuple[GoogleResourceScope, ...],
        credential_resolver: GoogleCredentialResolver,
        transport: GoogleApiTransport,
        retry_observer: GoogleApiRetryObserver,
        sheet_profiles: tuple[GoogleSheetsReadProfile, ...] = (),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], Awaitable[None]],
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        references = tuple(scope.reference.value for scope in resource_scopes)
        if len(set(references)) != len(references):
            raise ValueError("Google resource scope references must be unique")
        self._endpoints = endpoints
        self._policy = request_policy
        self._scopes = {scope.reference.value: scope for scope in resource_scopes}
        self._resolver = credential_resolver
        self._base_transport = transport
        self._transport_override: ContextVar[GoogleApiTransport | None] = ContextVar(
            f"google-api-transport-{id(self)}", default=None
        )
        self._retry_observer = retry_observer
        profile_keys = tuple(
            (profile.spreadsheet_id, profile.sheet_id) for profile in sheet_profiles
        )
        if len(set(profile_keys)) != len(profile_keys):
            raise ValueError(
                "Google Sheets profiles must be unique by spreadsheet/sheet"
            )
        self._sheet_profiles = {
            (profile.spreadsheet_id, profile.sheet_id): profile
            for profile in sheet_profiles
        }
        self._configuration_fingerprint = google_api_configuration_fingerprint(
            endpoints=endpoints,
            request_policy=request_policy,
            resource_scopes=resource_scopes,
            sheet_profiles=sheet_profiles,
        )
        self._clock = clock
        self._base_sleep = sleep
        self._jitter = jitter
        self._request_semaphore = asyncio.Semaphore(
            request_policy.max_concurrent_requests
        )
        self._refresh_locks: dict[UUID, asyncio.Lock] = {}
        self._run_request_budget: ContextVar[GoogleApiRunRequestBudget | None] = (
            ContextVar(f"google-api-request-budget-{id(self)}", default=None)
        )
        self._sleep_override: ContextVar[Callable[[float], Awaitable[None]] | None] = (
            ContextVar(f"google-api-sleep-{id(self)}", default=None)
        )
        self._tokens: dict[tuple[object, ...], _AccessToken] = {}
        self._discovered_files: dict[tuple[object, ...], set[str]] = {}

    @property
    def configuration_fingerprint(self) -> str:
        return self._configuration_fingerprint

    @contextmanager
    def using_transport(
        self,
        transport: GoogleApiTransport,
        *,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> Iterator[None]:
        """Install a per-run transport decorator for the duration of one probe."""
        transport_token = self._transport_override.set(transport)
        sleep_token: Token[Callable[[float], Awaitable[None]] | None] | None = None
        if sleep is not None:
            sleep_token = self._sleep_override.set(sleep)
        try:
            yield
        finally:
            self._transport_override.reset(transport_token)
            if sleep_token is not None:
                self._sleep_override.reset(sleep_token)

    @property
    def transport(self) -> GoogleApiTransport:
        return self._transport_override.get() or self._base_transport

    @property
    def sleep(self) -> Callable[[float], Awaitable[None]]:
        return self._sleep_override.get() or self._base_sleep

    async def get_metadata(
        self,
        connection: GoogleConnection,
        resource_scope_reference: OpaqueGoogleReference,
    ) -> DriveFileMetadata:
        scope = self._scope(connection, resource_scope_reference)
        if len(scope.allowed_file_ids) != 1:
            raise GoogleResourceScopeError(
                "Drive metadata scope must select exactly one approved file"
            )
        return await self.get_metadata_for_file(
            connection, resource_scope_reference, scope.allowed_file_ids[0]
        )

    async def get_metadata_for_file(
        self,
        connection: GoogleConnection,
        resource_scope_reference: OpaqueGoogleReference,
        file_id: str,
    ) -> DriveFileMetadata:
        scope = self._scope(connection, resource_scope_reference)
        self._require_file(connection, scope, file_id)
        response = await self._authorized_request(
            connection,
            method="GET",
            url=f"{self._endpoints.drive_base_url.rstrip('/')}/files/{quote(file_id, safe='')}",
            query={
                "fields": (
                    "id,name,mimeType,version,webViewLink,driveId,parents,"
                    "modifiedTime,trashed"
                ),
                "supportsAllDrives": str(scope.supports_all_drives).lower(),
            },
            drive_not_found=True,
        )
        payload = self._json_success(response)
        if payload.get("id") != file_id:
            raise GoogleProviderContractError(
                "Google Drive metadata response identity is invalid."
            )
        self._validate_current_file_scope(connection, scope, file_id, payload)
        return DriveFileMetadata(
            file_id=_required_string(payload, "id"),
            name=_required_string(payload, "name"),
            mime_type=_required_string(payload, "mimeType"),
            version=_required_string(payload, "version"),
            current_url=_optional_string(payload, "webViewLink"),
            drive_id=_optional_string(payload, "driveId"),
            parent_ids=tuple(_string_list(payload, "parents")),
            modified_time=_optional_datetime(payload, "modifiedTime"),
        )

    async def download(
        self,
        connection: GoogleConnection,
        resource_scope_reference: OpaqueGoogleReference,
        file_id: str,
        version: str,
    ) -> DriveFileContent:
        del version  # The guarded reader verifies the provider version around this call.
        scope = self._scope(connection, resource_scope_reference)
        self._require_file(connection, scope, file_id)
        await self._revalidate_current_file_scope(connection, scope, file_id)
        response = await self._authorized_request(
            connection,
            method="GET",
            url=f"{self._endpoints.drive_base_url.rstrip('/')}/files/{quote(file_id, safe='')}",
            query={
                "alt": "media",
                "supportsAllDrives": str(scope.supports_all_drives).lower(),
            },
            json_expected=False,
            drive_not_found=True,
        )
        media_type = _content_type(response)
        return DriveFileContent(content=response.content, media_type=media_type)

    async def export(
        self,
        connection: GoogleConnection,
        resource_scope_reference: OpaqueGoogleReference,
        file_id: str,
        version: str,
        export_mime_type: str,
    ) -> DriveFileContent:
        del version
        scope = self._scope(connection, resource_scope_reference)
        self._require_file(connection, scope, file_id)
        await self._revalidate_current_file_scope(connection, scope, file_id)
        response = await self._authorized_request(
            connection,
            method="GET",
            url=(
                f"{self._endpoints.drive_base_url.rstrip('/')}/files/"
                f"{quote(file_id, safe='')}/export"
            ),
            query={"mimeType": export_mime_type},
            json_expected=False,
            drive_not_found=True,
        )
        return DriveFileContent(
            content=response.content, media_type=_content_type(response)
        )

    async def get_start_page_token(
        self,
        connection: GoogleConnection,
        resource_scope_reference: OpaqueGoogleReference,
    ) -> str:
        scope = self._scope(connection, resource_scope_reference)
        query = {
            "supportsAllDrives": str(scope.supports_all_drives).lower(),
        }
        if scope.drive_id is not None:
            query["driveId"] = scope.drive_id
        response = await self._authorized_request(
            connection,
            method="GET",
            url=f"{self._endpoints.drive_base_url.rstrip('/')}/changes/startPageToken",
            query=query,
            drive_not_found=True,
        )
        return _required_string(self._json_success(response), "startPageToken")

    async def list_page(
        self,
        connection: GoogleConnection,
        resource_scope_reference: OpaqueGoogleReference,
        page_token: str | None,
    ) -> DriveSnapshotPage:
        scope = self._scope(connection, resource_scope_reference)
        if scope.folder_id is None and scope.drive_id is None:
            raise GoogleResourceScopeError(
                "Drive enumeration requires an approved folder or Shared Drive."
            )
        query = {
            "fields": "nextPageToken,incompleteSearch,files(id,driveId)",
            "pageSize": str(self._policy.drive_page_size),
            "supportsAllDrives": str(scope.supports_all_drives).lower(),
            "includeItemsFromAllDrives": str(scope.supports_all_drives).lower(),
        }
        if page_token is not None:
            query["pageToken"] = page_token
        if scope.folder_id is not None:
            query["q"] = (
                f"'{_drive_query_literal(scope.folder_id)}' in parents and trashed = false"
            )
        if scope.drive_id is not None:
            query.update({"corpora": "drive", "driveId": scope.drive_id})
        payload = self._json_success(
            await self._authorized_request(
                connection,
                method="GET",
                url=f"{self._endpoints.drive_base_url.rstrip('/')}/files",
                query=query,
                drive_not_found=True,
            )
        )
        raw_files = payload.get("files", [])
        if not isinstance(raw_files, list):
            raise GoogleProviderContractError("Google provider response is invalid.")
        items = tuple(
            DriveSnapshotItem(
                file_id=_required_string(item, "id"),
                drive_id=_optional_string(item, "driveId"),
            )
            for item in raw_files
            if isinstance(item, Mapping)
        )
        if len(items) != len(raw_files):
            raise GoogleProviderContractError("Google provider response is invalid.")
        if scope.drive_id is not None and any(
            item.drive_id != scope.drive_id for item in items
        ):
            raise GoogleResourceScopeError(
                "Google request is outside the approved Google resource scope."
            )
        if scope.drive_id is not None:
            items = tuple(
                item for item in items if item.file_id in scope.allowed_file_ids
            )
        else:
            self._discovered_files.setdefault(
                self._scope_key(connection, scope), set()
            ).update(item.file_id for item in items)
        return DriveSnapshotPage(
            page_token=page_token,
            next_page_token=_optional_string(payload, "nextPageToken"),
            incomplete_search=_required_bool(
                payload, "incompleteSearch", default=False
            ),
            items=items,
        )

    async def list_changes(
        self,
        connection: GoogleConnection,
        resource_scope_reference: OpaqueGoogleReference,
        page_token: str,
    ) -> DriveChangePage:
        scope = self._scope(connection, resource_scope_reference)
        query = {
            "pageToken": page_token,
            "pageSize": str(self._policy.drive_page_size),
            "includeRemoved": "true",
            "supportsAllDrives": str(scope.supports_all_drives).lower(),
            "includeItemsFromAllDrives": str(scope.supports_all_drives).lower(),
            "fields": (
                "nextPageToken,newStartPageToken,incompleteSearch,"
                "changes(changeId,fileId,removed,file(id,driveId,version,trashed,parents))"
            ),
        }
        if scope.drive_id is not None:
            query.update({"driveId": scope.drive_id, "restrictToMyDrive": "false"})
        payload = self._json_success(
            await self._authorized_request(
                connection,
                method="GET",
                url=f"{self._endpoints.drive_base_url.rstrip('/')}/changes",
                query=query,
                drive_not_found=True,
            )
        )
        raw_changes = payload.get("changes", [])
        if not isinstance(raw_changes, list):
            raise GoogleProviderContractError("Google provider response is invalid.")
        changes, ambiguous_membership = self._scoped_changes(scope, raw_changes)
        return DriveChangePage(
            page_token=page_token,
            next_page_token=_optional_string(payload, "nextPageToken"),
            new_start_page_token=_optional_string(payload, "newStartPageToken"),
            incomplete_search=(
                _required_bool(payload, "incompleteSearch", default=False)
                or ambiguous_membership
            ),
            changes=changes,
        )

    def _scoped_changes(
        self,
        scope: GoogleResourceScope,
        raw_changes: list[Any],
    ) -> tuple[tuple[DriveChange, ...], bool]:
        scoped: list[DriveChange] = []
        ambiguous_membership = False
        for raw_change in raw_changes:
            change = _drive_change(raw_change)
            explicitly_allowed = (
                change.file_id in scope.allowed_file_ids
                and (
                    scope.drive_id is None
                    or change.drive_id == scope.drive_id
                )
            )
            in_folder = (
                scope.folder_id is not None
                and scope.folder_id in change.parent_ids
                and not change.removed
                and not change.trashed
            )
            if scope.drive_id is not None:
                if (
                    change.file_id in scope.allowed_file_ids
                    and change.drive_id is not None
                    and change.drive_id != scope.drive_id
                ):
                    ambiguous_membership = True
                    continue
                if (
                    change.file_id in scope.allowed_file_ids
                    and change.drive_id is None
                    and not change.removed
                ):
                    ambiguous_membership = True
                drive_identity_matches = (
                    change.drive_id == scope.drive_id
                    or (change.removed and change.drive_id is None)
                )
                currently_in_scope = (
                    change.file_id in scope.allowed_file_ids
                    and drive_identity_matches
                    and (scope.folder_id is None or in_folder or change.removed)
                )
                if (
                    change.file_id in scope.allowed_file_ids
                    and drive_identity_matches
                    and scope.folder_id is not None
                    and not in_folder
                    and not change.removed
                ):
                    ambiguous_membership = True
            else:
                currently_in_scope = explicitly_allowed or in_folder
            if not currently_in_scope:
                if scope.folder_id is not None and scope.drive_id is None:
                    ambiguous_membership = True
                continue
            scoped.append(change.model_copy(update={"in_scope": True}))
        return tuple(scoped), ambiguous_membership

    async def get_provider_version(
        self,
        connection: GoogleConnection,
        resource_scope_reference: OpaqueGoogleReference,
        spreadsheet_id: str,
    ) -> str:
        scope = self._scope(connection, resource_scope_reference)
        self._require_spreadsheet(scope, spreadsheet_id)
        payload = self._json_success(
            await self._authorized_request(
                connection,
                method="GET",
                url=(
                    f"{self._endpoints.drive_base_url.rstrip('/')}/files/"
                    f"{quote(spreadsheet_id, safe='')}"
                ),
                query={
                    "fields": "id,version",
                    "supportsAllDrives": str(scope.supports_all_drives).lower(),
                },
            )
        )
        if payload.get("id") != spreadsheet_id:
            raise GoogleProviderContractError("Google provider response is invalid.")
        return _required_string(payload, "version")

    async def get_spreadsheet(
        self,
        connection: GoogleConnection,
        resource_scope_reference: OpaqueGoogleReference,
        spreadsheet_id: str,
        sheet_id: int,
    ) -> GoogleSheetsSnapshot:
        scope = self._scope(connection, resource_scope_reference)
        self._require_spreadsheet(scope, spreadsheet_id)
        profile = self._sheet_profile(spreadsheet_id, sheet_id)
        response = await self._authorized_request(
            connection,
            method="GET",
            url=(
                f"{self._endpoints.sheets_base_url.rstrip('/')}/spreadsheets/"
                f"{quote(spreadsheet_id, safe='')}"
            ),
            query={
                "includeGridData": "true",
                "fields": _SHEETS_FIELDS,
            },
        )
        return _sheets_snapshot(self._json_success(response), profile)

    async def get_spreadsheet_range_page(
        self,
        connection: GoogleConnection,
        resource_scope_reference: OpaqueGoogleReference,
        spreadsheet_id: str,
        sheet_id: int,
        a1_range: str,
        page_token: str | None,
    ) -> GoogleSheetsRangePage:
        scope = self._scope(connection, resource_scope_reference)
        self._require_spreadsheet(scope, spreadsheet_id)
        profile = self._sheet_profile(spreadsheet_id, sheet_id)
        provider_range, next_page_token, range_complete = _paged_a1_range(
            a1_range,
            page_token,
            page_size=self._policy.sheets_page_size,
        )
        _require_profile_columns_in_range(profile, provider_range)
        version_before = await self.get_provider_version(
            connection, resource_scope_reference, spreadsheet_id
        )
        response = await self._authorized_request(
            connection,
            method="GET",
            url=(
                f"{self._endpoints.sheets_base_url.rstrip('/')}/spreadsheets/"
                f"{quote(spreadsheet_id, safe='')}"
            ),
            query={
                "includeGridData": "true",
                "ranges": provider_range,
                "fields": _SHEETS_FIELDS,
            },
        )
        snapshot = _sheets_snapshot(
            self._json_success(response),
            profile,
            expected_a1_range=provider_range,
        )
        version_after = await self.get_provider_version(
            connection, resource_scope_reference, spreadsheet_id
        )
        # Sparse pages are valid Sheets responses. Only the configured A1 page
        # boundary, never returned row count, determines completion.
        complete = range_complete
        return GoogleSheetsRangePage(
            spreadsheet_id=spreadsheet_id,
            sheet_id=sheet_id,
            a1_range=a1_range,
            page_token=page_token,
            next_page_token=None if complete else next_page_token,
            complete=complete,
            provider_version_before=version_before,
            provider_version_after=version_after,
            snapshot=snapshot,
        )

    def _scope(
        self,
        connection: GoogleConnection,
        reference: OpaqueGoogleReference,
    ) -> GoogleResourceScope:
        if reference not in connection.approved_resource_scope_references:
            raise GoogleResourceScopeError(
                "Google request is outside the approved Google resource scope."
            )
        scope = self._scopes.get(reference.value)
        if scope is None:
            raise GoogleResourceScopeError(
                "Google request is outside the approved Google resource scope."
            )
        if (
            scope.workspace_id != connection.workspace_id
            or scope.environment_id != connection.environment_id
            or scope.connection_id != connection.connection_id
            or scope.authorization_generation != connection.authorization_generation
            or scope.configuration_generation != connection.configuration_generation
        ):
            raise GoogleResourceScopeError(
                "Google request is outside the approved Google resource scope."
            )
        return scope

    def _require_file(
        self,
        connection: GoogleConnection,
        scope: GoogleResourceScope,
        file_id: str,
    ) -> None:
        if scope.drive_id is not None:
            is_allowed = file_id in scope.allowed_file_ids
        else:
            is_allowed = file_id in scope.allowed_file_ids or file_id in (
                self._discovered_files.get(self._scope_key(connection, scope), set())
            )
        if not is_allowed:
            raise GoogleResourceScopeError(
                "Google request is outside the approved Google resource scope."
            )

    async def _revalidate_current_file_scope(
        self,
        connection: GoogleConnection,
        scope: GoogleResourceScope,
        file_id: str,
    ) -> None:
        if file_id in scope.allowed_file_ids and scope.drive_id is None:
            return
        payload = self._json_success(
            await self._authorized_request(
                connection,
                method="GET",
                url=(
                    f"{self._endpoints.drive_base_url.rstrip('/')}/files/"
                    f"{quote(file_id, safe='')}"
                ),
                query={
                    "fields": "id,driveId,parents,trashed",
                    "supportsAllDrives": str(scope.supports_all_drives).lower(),
                },
                drive_not_found=True,
            )
        )
        if payload.get("id") != file_id:
            raise GoogleProviderContractError(
                "Google Drive metadata response identity is invalid."
            )
        self._validate_current_file_scope(connection, scope, file_id, payload)

    def _validate_current_file_scope(
        self,
        connection: GoogleConnection,
        scope: GoogleResourceScope,
        file_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        parents = _string_list(payload, "parents")
        in_folder = scope.folder_id is not None and scope.folder_id in parents
        in_drive = (
            scope.drive_id is not None and payload.get("driveId") == scope.drive_id
        )
        trashed = _required_bool(payload, "trashed", default=False)
        if file_id in scope.allowed_file_ids and scope.drive_id is None:
            return
        in_scope = (
            in_drive and (scope.folder_id is None or in_folder)
            if scope.drive_id is not None
            else (in_folder or file_id in scope.allowed_file_ids)
        )
        if trashed or not in_scope:
            self._discovered_files.setdefault(
                self._scope_key(connection, scope), set()
            ).discard(file_id)
            raise GoogleResourceScopeError(
                "Google request is outside the approved Google resource scope."
            )

    @staticmethod
    def _scope_key(
        connection: GoogleConnection, scope: GoogleResourceScope
    ) -> tuple[object, ...]:
        return (
            connection.workspace_id,
            connection.environment_id,
            connection.connection_id,
            connection.authorization_generation,
            connection.configuration_generation,
            scope.reference.value,
        )

    @staticmethod
    def _require_spreadsheet(scope: GoogleResourceScope, spreadsheet_id: str) -> None:
        if spreadsheet_id not in scope.allowed_spreadsheet_ids:
            raise GoogleResourceScopeError(
                "Google request is outside the approved Google resource scope."
            )

    def _sheet_profile(
        self, spreadsheet_id: str, sheet_id: int
    ) -> GoogleSheetsReadProfile:
        profile = self._sheet_profiles.get((spreadsheet_id, sheet_id))
        if profile is None:
            raise GoogleSheetsConfiguredScopeError(
                "Sheets request is outside the configured spreadsheet/sheet scope."
            )
        return profile

    async def _authorized_request(
        self,
        connection: GoogleConnection,
        *,
        method: str,
        url: str,
        query: Mapping[str, str],
        json_expected: bool = True,
        drive_not_found: bool = False,
    ) -> GoogleApiResponse:
        if method.upper() != "GET":
            raise GoogleProviderPolicyError()
        refreshed_after_unauthorized = False
        for attempt in range(self._policy.max_attempts):
            token = await self._access_token(connection)
            try:
                response = await self._send_request(
                    GoogleApiRequest(
                        method=method,
                        url=url,
                        headers={"Authorization": f"Bearer {token}"},
                        query=query,
                        form=None,
                        timeout_seconds=self._policy.timeout_seconds,
                    )
                )
            except asyncio.CancelledError:
                raise
            except GoogleProviderQuotaError:
                raise
            except (TimeoutError, asyncio.TimeoutError, GoogleProviderReadError):
                if attempt + 1 == self._policy.max_attempts:
                    raise GoogleProviderReadError() from None
                delay = self._retry_delay(attempt, None)
                await self._record_retry(connection, "provider", "transient", attempt)
                await self.sleep(delay)
                continue
            if 200 <= response.status_code < 300:
                if json_expected and response.json_body is None:
                    raise GoogleProviderContractError(
                        "Google provider response is invalid."
                    )
                return response
            if response.status_code == 401:
                self._invalidate_token(connection)
                if (
                    refreshed_after_unauthorized
                    or attempt + 1 == self._policy.max_attempts
                ):
                    raise GoogleProviderAuthorizationError()
                refreshed_after_unauthorized = True
                delay = self._retry_delay(attempt, response)
                await self._record_retry(connection, "provider", "reauthorize", attempt)
                await self.sleep(delay)
                continue
            if response.status_code == 404:
                if drive_not_found:
                    raise GoogleDriveAccessOutcomeError(
                        GoogleDriveLifecycleSignal.HTTP_404
                    )
                raise GoogleProviderNotFoundError()
            if response.status_code == 403:
                reason = _provider_reason(response)
                if reason in _TERMINAL_PROVIDER_QUOTA_REASONS:
                    raise GoogleProviderQuotaError()
                if reason in {
                    "ratelimitexceeded",
                    "userratelimitexceeded",
                }:
                    failure: Exception = GoogleProviderQuotaError()
                else:
                    failure = GoogleProviderPolicyError()
                if not isinstance(failure, GoogleProviderQuotaError):
                    raise failure
            elif response.status_code == 429:
                if _provider_reason(response) in _TERMINAL_PROVIDER_QUOTA_REASONS:
                    raise GoogleProviderQuotaError()
                failure = GoogleProviderQuotaError()
            elif response.status_code >= 500:
                failure = GoogleProviderReadError()
            else:
                raise GoogleProviderContractError("Google provider request failed.")
            if attempt + 1 == self._policy.max_attempts:
                raise failure
            delay = self._retry_delay(attempt, response)
            await self._record_retry(
                connection,
                "provider",
                (
                    "quota"
                    if isinstance(failure, GoogleProviderQuotaError)
                    else "transient"
                ),
                attempt,
            )
            await self.sleep(delay)
        raise AssertionError("bounded Google request must return or raise")

    def _token_key(self, connection: GoogleConnection) -> tuple[object, ...]:
        return (
            connection.connection_id,
            connection.authorization_generation,
            connection.credential_reference.value,
            connection.grant_reference.value,
        )

    def _invalidate_token(self, connection: GoogleConnection) -> None:
        self._tokens.pop(self._token_key(connection), None)

    async def _oauth_request(
        self, connection: GoogleConnection, request: GoogleApiRequest
    ) -> GoogleApiResponse:
        for attempt in range(self._policy.max_attempts):
            try:
                response = await self._send_request(request)
            except asyncio.CancelledError:
                raise
            except GoogleProviderQuotaError:
                raise
            except (
                TimeoutError,
                asyncio.TimeoutError,
                GoogleProviderReadError,
            ):
                # A timeout or transport error leaves the POST outcome unknown.
                # Retrying could repeat a refresh whose response was lost.
                raise GoogleProviderReadError() from None
            if response is not None and 200 <= response.status_code < 300:
                return response
            if response.status_code < 500:
                reason = _provider_reason(response)
                if response.status_code == 401 or reason in {
                    "invalid_grant",
                    "token_revoked",
                    "access_revoked",
                    "grant_revoked",
                    "invalid_client",
                    "unauthorized_client",
                }:
                    raise GoogleProviderAuthorizationError()
                if response.status_code == 403 or reason == "invalid_scope":
                    raise GoogleProviderPolicyError()
                if response.status_code != 429:
                    raise GoogleProviderContractError(
                        "Google OAuth request was rejected."
                    )
            if response.status_code != 429:
                raise GoogleProviderReadError()
            if attempt + 1 == self._policy.max_attempts:
                if response.status_code == 429:
                    raise GoogleProviderQuotaError()
                raise GoogleProviderReadError()
            delay = self._retry_delay(attempt, response)
            await self._record_retry(connection, "oauth", "quota", attempt)
            await self.sleep(delay)
        raise AssertionError("bounded OAuth request must return or raise")

    async def _record_retry(
        self,
        connection: GoogleConnection,
        operation: Literal["oauth", "provider"],
        reason: Literal["quota", "reauthorize", "transient"],
        zero_based_attempt: int,
    ) -> None:
        try:
            await self._retry_observer.record_retry(
                GoogleApiRetryEvent(
                    schema_version="google-api-retry:v1",
                    workspace_id=connection.workspace_id,
                    environment_id=connection.environment_id,
                    connection_id=connection.connection_id,
                    operation=operation,
                    reason=reason,
                    attempt=zero_based_attempt + 1,
                    max_attempts=self._policy.max_attempts,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Low-level attempt diagnostics are best effort. Durable business retry
            # outcomes remain owned by the canonical sync coordinator.
            return

    async def _access_token(self, connection: GoogleConnection) -> str:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Google API clock must be timezone-aware")
        key = self._token_key(connection)
        cached = self._tokens.get(key)
        if cached is not None and cached.expires_at > now + timedelta(seconds=30):
            return cached.value
        lock = self._refresh_locks.setdefault(
            connection.grant_reference.value, asyncio.Lock()
        )
        async with lock:
            cached = self._tokens.get(key)
            now = self._clock()
            if cached is not None and cached.expires_at > now + timedelta(seconds=30):
                return cached.value
            return await self._refresh_access_token(connection, key, now)

    async def _refresh_access_token(
        self,
        connection: GoogleConnection,
        key: tuple[object, ...],
        now: datetime,
    ) -> str:
        try:
            material = await self._resolver.resolve(
                connection.credential_reference, connection.grant_reference
            )
        except asyncio.CancelledError:
            raise
        except GoogleProviderReadError:
            raise
        except Exception:
            raise GoogleProviderReadError() from None
        response = await self._oauth_request(
            connection,
            GoogleApiRequest(
                method="POST",
                url=self._endpoints.oauth_token_url,
                headers={},
                query={},
                form={
                    "client_id": material.client_id,
                    "client_secret": material.client_secret,
                    "refresh_token": material.refresh_token,
                    "grant_type": "refresh_token",
                    "scope": " ".join(self._endpoints.approved_oauth_scopes),
                },
                timeout_seconds=self._policy.timeout_seconds,
            ),
        )
        payload = self._json_success(response)
        access_token = _required_string(payload, "access_token")
        expires_in = payload.get("expires_in")
        if (
            not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
            or expires_in <= 0
        ):
            raise GoogleProviderContractError(
                "Google OAuth response expiry is invalid."
            )
        self._tokens[key] = _AccessToken(
            value=access_token, expires_at=now + timedelta(seconds=expires_in)
        )
        return access_token

    async def _send_request(self, request: GoogleApiRequest) -> GoogleApiResponse:
        async with self._request_semaphore:
            budget = self._run_request_budget.get()
            if budget is not None:
                budget.reserve()
            return await self.transport.request(request)

    @contextmanager
    def using_run_request_budget(self) -> Iterator[GoogleApiRunRequestBudget]:
        """Share one request counter across every nested call in a live run."""

        existing = self._run_request_budget.get()
        if existing is not None:
            yield existing
            return
        budget = GoogleApiRunRequestBudget(self._policy.max_requests_per_run)
        token = self._run_request_budget.set(budget)
        try:
            yield budget
        finally:
            self._run_request_budget.reset(token)

    def _retry_delay(
        self, zero_based_attempt: int, response: GoogleApiResponse | None
    ) -> float:
        ceiling = self._policy.backoff_ceiling_seconds[zero_based_attempt]
        jitter_delay = self._jitter(0.0, ceiling)
        if not 0 <= jitter_delay <= ceiling:
            raise ValueError("Google retry jitter must be within its approved ceiling")
        retry_after = _retry_after_seconds(response, self._clock())
        if retry_after is not None and retry_after > _MAX_RETRY_AFTER_SECONDS:
            raise GoogleProviderQuotaError()
        return max(jitter_delay, retry_after or 0.0)

    @staticmethod
    def _json_success(response: GoogleApiResponse) -> Mapping[str, Any]:
        if response.status_code < 200 or response.status_code >= 300:
            raise GoogleProviderContractError("Google provider request failed.")
        if response.json_body is None:
            raise GoogleProviderContractError("Google provider response is invalid.")
        return response.json_body


def _retry_after_seconds(
    response: GoogleApiResponse | None, now: datetime
) -> float | None:
    if response is None or not response.headers:
        return None
    value = next(
        (
            item
            for key, item in response.headers.items()
            if key.lower() == "retry-after"
        ),
        None,
    )
    if value is None:
        return None
    stripped = value.strip()
    if stripped.isdecimal():
        return float(min(int(stripped), int(_MAX_RETRY_AFTER_SECONDS) + 1))
    try:
        retry_at = parsedate_to_datetime(stripped)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None or retry_at.utcoffset() is None:
        return None
    return max(0.0, (retry_at - now).total_seconds())


_MAX_RETRY_AFTER_SECONDS = 30.0
_TERMINAL_PROVIDER_QUOTA_REASONS = frozenset(
    {"quotaexceeded", "dailylimitexceeded", "dailylimitexceededunreg"}
)

def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise GoogleProviderContractError("Google provider response is invalid.")
    return value


def _optional_string(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise GoogleProviderContractError("Google provider response is invalid.")
    return value


def _string_list(payload: Mapping[str, Any], key: str) -> list[str]:
    value = payload.get(key, [])
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise GoogleProviderContractError("Google provider response is invalid.")
    return value


def _optional_datetime(payload: Mapping[str, Any], key: str) -> datetime | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise GoogleProviderContractError("Google provider response is invalid.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise GoogleProviderContractError(
            "Google provider response is invalid."
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GoogleProviderContractError("Google provider response is invalid.")
    return parsed


def _required_bool(
    payload: Mapping[str, Any], key: str, *, default: bool | None = None
) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise GoogleProviderContractError("Google provider response is invalid.")
    return value


def _content_type(response: GoogleApiResponse) -> str:
    headers = response.headers or {}
    value = next(
        (item for key, item in headers.items() if key.lower() == "content-type"), None
    )
    if not isinstance(value, str) or not value:
        raise GoogleProviderContractError("Google provider content type is missing.")
    return value.split(";", 1)[0].strip()


def _drive_query_literal(value: str) -> str:
    if "'" in value or "\\" in value:
        raise GoogleResourceScopeError("Approved Google folder ID is invalid.")
    return value


def _drive_change(value: Any) -> DriveChange:
    if not isinstance(value, Mapping):
        raise GoogleProviderContractError("Google provider response is invalid.")
    file_id = _required_string(value, "fileId")
    removed = _required_bool(value, "removed", default=False)
    raw_file = value.get("file", {})
    if not isinstance(raw_file, Mapping):
        raise GoogleProviderContractError("Google provider response is invalid.")
    if raw_file and raw_file.get("id") not in {None, file_id}:
        raise GoogleProviderContractError("Google provider response is invalid.")
    return DriveChange(
        file_id=file_id,
        drive_id=_optional_string(raw_file, "driveId"),
        change_id=_optional_string(value, "changeId"),
        provider_version=_optional_string(raw_file, "version"),
        removed=removed,
        trashed=_required_bool(raw_file, "trashed", default=False),
        parent_ids=tuple(_string_list(raw_file, "parents")),
    )


def _provider_reason(response: GoogleApiResponse) -> str:
    payload = response.json_body
    if not isinstance(payload, Mapping):
        return ""
    error = payload.get("error")
    if isinstance(error, str):
        return error.lower()
    if not isinstance(error, Mapping):
        return ""
    errors = error.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], Mapping):
        reason = errors[0].get("reason")
        if isinstance(reason, str):
            return reason.lower()
    status = error.get("status")
    if isinstance(status, str):
        return status.lower()
    return ""


_SHEETS_FIELDS = (
    "spreadsheetId,properties(locale,timeZone),"
    "developerMetadata(metadataId,metadataKey,metadataValue,location),"
    "sheets(properties(sheetId,title),developerMetadata(metadataId,metadataKey,"
    "metadataValue,location),data(startRow,startColumn,rowData(values("
    "userEnteredValue,effectiveValue,formattedValue,effectiveFormat(numberFormat)))))"
)


def _sheets_snapshot(
    payload: Mapping[str, Any],
    profile: GoogleSheetsReadProfile,
    *,
    expected_a1_range: str | None = None,
) -> GoogleSheetsSnapshot:
    if payload.get("spreadsheetId") != profile.spreadsheet_id:
        raise GoogleSheetsConfiguredScopeError(
            "Sheets response is outside the configured spreadsheet."
        )
    properties = payload.get("properties")
    sheets = payload.get("sheets")
    if not isinstance(properties, Mapping) or not isinstance(sheets, list):
        raise GoogleProviderContractError("Google provider response is invalid.")
    matches = [
        sheet
        for sheet in sheets
        if isinstance(sheet, Mapping)
        and isinstance(sheet.get("properties"), Mapping)
        and sheet["properties"].get("sheetId") == profile.sheet_id
    ]
    if len(matches) != 1:
        raise GoogleSheetsConfiguredScopeError(
            "Sheets response does not contain exactly one configured numeric sheet ID."
        )
    sheet = matches[0]
    sheet_properties = sheet["properties"]
    title = _required_string(sheet_properties, "title")
    metadata = _developer_metadata_by_row(
        tuple(_metadata_items(payload)) + tuple(_metadata_items(sheet)),
        sheet_id=profile.sheet_id,
    )
    expected_bounds = (
        _a1_bounds(expected_a1_range) if expected_a1_range is not None else None
    )
    rows: list[GoogleSheetsRowData] = []
    raw_grids = sheet.get("data", [])
    if not isinstance(raw_grids, list):
        raise GoogleProviderContractError("Google provider response is invalid.")
    for grid in raw_grids:
        if not isinstance(grid, Mapping):
            raise GoogleProviderContractError("Google provider response is invalid.")
        start_row = _non_negative_integer(grid.get("startRow", 0))
        start_column = _non_negative_integer(grid.get("startColumn", 0))
        row_data = grid.get("rowData", [])
        if not isinstance(row_data, list):
            raise GoogleProviderContractError("Google provider response is invalid.")
        if expected_bounds is not None:
            first_row, last_row, first_column, last_column = expected_bounds
            observed_first_row = start_row + 1
            if (
                observed_first_row < first_row
                or observed_first_row > last_row
                or start_column < first_column
                or start_column > last_column
                or len(row_data) > last_row - observed_first_row + 1
            ):
                raise GoogleSheetsConfiguredScopeError(
                    "Sheets grid data is outside the requested page range."
                )
        for offset, raw_row in enumerate(row_data):
            if not isinstance(raw_row, Mapping):
                raise GoogleProviderContractError(
                    "Google provider response is invalid."
                )
            row_number = start_row + offset + 1
            raw_cells = raw_row.get("values", [])
            if not isinstance(raw_cells, list):
                raise GoogleProviderContractError(
                    "Google provider response is invalid."
                )
            if (
                expected_bounds is not None
                and len(raw_cells) > last_column - start_column + 1
            ):
                raise GoogleSheetsConfiguredScopeError(
                    "Sheets row data is outside the requested page range."
                )
            by_column = {
                _column_name(start_column + index): value
                for index, value in enumerate(raw_cells)
                if isinstance(value, Mapping)
            }
            cells = tuple(
                _sheet_cell(
                    column.column_key,
                    column.coordinate,
                    row_number,
                    by_column.get(column.coordinate, {}),
                )
                for column in profile.columns
            )
            coordinates = [column.coordinate for column in profile.columns]
            rows.append(
                GoogleSheetsRowData(
                    row_number=row_number,
                    a1_range=(
                        f"{min(coordinates, key=_column_index)}{row_number}:"
                        f"{max(coordinates, key=_column_index)}{row_number}"
                    ),
                    cells=cells,
                    developer_metadata=tuple(metadata.get(row_number, ())),
                )
            )
    return GoogleSheetsSnapshot(
        spreadsheet_id=profile.spreadsheet_id,
        sheet_id=profile.sheet_id,
        sheet_title=title,
        locale=_required_string(properties, "locale"),
        time_zone=_required_string(properties, "timeZone"),
        rows=tuple(rows),
    )


def _sheet_cell(
    column_key: str,
    column: str,
    row_number: int,
    payload: Mapping[str, Any],
) -> GoogleSheetsCellData:
    effective_format = payload.get("effectiveFormat", {})
    if not isinstance(effective_format, Mapping):
        raise GoogleProviderContractError("Google provider response is invalid.")
    number_format = effective_format.get("numberFormat", {})
    if not isinstance(number_format, Mapping):
        raise GoogleProviderContractError("Google provider response is invalid.")
    formatted = payload.get("formattedValue", "")
    if not isinstance(formatted, str):
        raise GoogleProviderContractError("Google provider response is invalid.")
    return GoogleSheetsCellData(
        column_key=column_key,
        coordinate=f"{column}{row_number}",
        entered=_sheet_value(payload.get("userEnteredValue"), entered=True),
        effective=_sheet_value(payload.get("effectiveValue"), entered=False),
        formatted=formatted,
        number_format=GoogleSheetNumberFormat(
            type=(
                _required_string(number_format, "type")
                if number_format
                else "UNSPECIFIED"
            ),
            pattern=_optional_string(number_format, "pattern"),
        ),
    )


def _sheet_value(value: Any, *, entered: bool) -> GoogleSheetCellValue:
    if value is None or value == {}:
        return GoogleSheetCellValue(kind="blank", value=None)
    if not isinstance(value, Mapping) or len(value) != 1:
        raise GoogleProviderContractError("Google provider response is invalid.")
    key, raw = next(iter(value.items()))
    if key == "stringValue" and isinstance(raw, str):
        return GoogleSheetCellValue(kind="string", value=raw)
    if (
        key == "numberValue"
        and isinstance(raw, (int, float))
        and not isinstance(raw, bool)
    ):
        return GoogleSheetCellValue(kind="number", value=str(Decimal(str(raw))))
    if key == "boolValue" and isinstance(raw, bool):
        return GoogleSheetCellValue(kind="boolean", value=raw)
    if key == "formulaValue" and entered and isinstance(raw, str):
        return GoogleSheetCellValue(kind="formula", value=raw)
    if key == "errorValue" and not entered and isinstance(raw, Mapping):
        error_type = raw.get("type")
        if isinstance(error_type, str) and error_type:
            return GoogleSheetCellValue(kind="error", value=error_type)
    raise GoogleProviderContractError("Google provider response is invalid.")


def _metadata_items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = payload.get("developerMetadata", [])
    if not isinstance(value, list) or any(
        not isinstance(item, Mapping) for item in value
    ):
        raise GoogleProviderContractError("Google provider response is invalid.")
    return value


def _developer_metadata_by_row(
    values: tuple[Mapping[str, Any], ...],
    *,
    sheet_id: int,
) -> dict[int, list[GoogleSheetsDeveloperMetadata]]:
    result: dict[int, list[GoogleSheetsDeveloperMetadata]] = {}
    for value in values:
        location = value.get("location")
        if not isinstance(location, Mapping):
            continue
        dimension = location.get("dimensionRange")
        if not isinstance(dimension, Mapping) or dimension.get("dimension") != "ROWS":
            continue
        if dimension.get("sheetId") != sheet_id:
            continue
        start = _non_negative_integer(dimension.get("startIndex"))
        end = _non_negative_integer(dimension.get("endIndex"))
        if end != start + 1:
            raise GoogleProviderContractError("Google provider response is invalid.")
        metadata = GoogleSheetsDeveloperMetadata(
            metadata_id=_non_negative_integer(value.get("metadataId")),
            key=_required_string(value, "metadataKey"),
            value=_optional_string(value, "metadataValue"),
            row_number=start + 1,
        )
        result.setdefault(start + 1, []).append(metadata)
    return result


def _paged_a1_range(
    value: str, page_token: str | None, *, page_size: int
) -> tuple[str, str | None, bool]:
    match = re.fullmatch(
        r"(?P<title>'(?:''|[^'])+'|[^!]+)!(?P<start>[A-Z]+)(?P<first>[1-9][0-9]*):"
        r"(?P<end>[A-Z]+)(?P<last>[1-9][0-9]*)",
        value,
    )
    if match is None:
        raise GoogleSheetsConfiguredScopeError(
            "Sheets pagination requires a bounded sheet-qualified A1 range."
        )
    first = int(match.group("first"))
    last = int(match.group("last"))
    if last < first:
        raise GoogleSheetsConfiguredScopeError("Sheets A1 range is reversed.")
    if page_token is None:
        page_first = first
    elif page_token.isdigit() and first <= int(page_token) <= last:
        page_first = int(page_token)
    else:
        raise GoogleSheetsConfiguredScopeError("Sheets page token is invalid.")
    page_last = min(last, page_first + page_size - 1)
    complete = page_last == last
    next_token = None if complete else str(page_last + 1)
    provider_range = (
        f"{match.group('title')}!{match.group('start')}{page_first}:"
        f"{match.group('end')}{page_last}"
    )
    return provider_range, next_token, complete


def _a1_bounds(value: str) -> tuple[int, int, int, int]:
    match = re.fullmatch(
        r"(?:'(?:''|[^'])+'|[^!]+)!(?P<start>[A-Z]+)(?P<first>[1-9][0-9]*):"
        r"(?P<end>[A-Z]+)(?P<last>[1-9][0-9]*)",
        value,
    )
    if match is None:
        raise GoogleSheetsConfiguredScopeError("Sheets A1 page range is invalid.")
    return (
        int(match.group("first")),
        int(match.group("last")),
        _column_index(match.group("start")) - 1,
        _column_index(match.group("end")) - 1,
    )


def _require_profile_columns_in_range(
    profile: GoogleSheetsReadProfile, a1_range: str
) -> None:
    _, _, first_column, last_column = _a1_bounds(a1_range)
    if any(
        not first_column <= _column_index(column.coordinate) - 1 <= last_column
        for column in profile.columns
    ):
        raise GoogleSheetsConfiguredScopeError(
            "Sheets page range does not cover every configured column."
        )


def _column_name(index: int) -> str:
    result = ""
    value = index + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _column_index(value: str) -> int:
    result = 0
    for character in value:
        result = result * 26 + ord(character) - 64
    return result


def _non_negative_integer(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise GoogleProviderContractError("Google provider response is invalid.")
    return value


__all__ = [
    "GoogleApiEndpoints",
    "GoogleApiRequest",
    "GoogleApiRequestPolicy",
    "GoogleApiResponse",
    "GoogleApiRetryEvent",
    "GoogleApiRetryObserver",
    "GoogleApiService",
    "GoogleApiTransport",
    "GoogleCredentialStore",
    "GoogleCredentialResolver",
    "GoogleDesktopOAuthMaterial",
    "GoogleOAuthClientCredentials",
    "GoogleOAuthRefreshGrant",
    "GoogleResourceScope",
    "GoogleResourceScopeError",
    "HttpxGoogleApiTransport",
    "ReferenceGoogleCredentialResolver",
    "google_api_configuration_fingerprint",
]
