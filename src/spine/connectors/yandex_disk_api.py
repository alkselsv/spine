"""Qualification-only Yandex Disk REST service with fail-closed identity binding."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
import json
import time
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import Field, SecretStr, StrictFloat, StrictInt, field_validator

from spine.application.yandex_disk_scan import (
    YandexDiskAuthorizationRevokedError,
    YandexDiskContractError,
    YandexDiskFileTooLargeError,
    YandexDiskPermissionLostError,
    YandexDiskResourceNotFoundError,
    YandexDiskScopeOutOfScopeError,
    YandexDiskThrottleError,
    YandexDiskTransientError,
)
from spine.auth import ExternalSecretReference
from spine.connectors.yandex_disk import (
    YandexDiskCredentialResolver,
    YandexDiskFileContent,
    YandexDiskResource,
    YandexDiskResourcePage,
)
from spine.domain.common import DefinitionModel
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderConnection,
    ProviderConnectionStatus,
    ProviderKind,
)


_IDENTITY_URL = "https://login.yandex.ru/info"
_DISK_RESOURCES_URL = "https://cloud-api.yandex.net/v1/disk/resources"
_DISK_DOWNLOAD_URL = "https://cloud-api.yandex.net/v1/disk/resources/download"
_QUALIFICATION_OAUTH_SCOPE = "cloud_api:disk.read"
_QUALIFICATION_PROVIDER_REFERENCE = "provider:local-env:v1"
_QUALIFICATION_SECRET_REFERENCE = "local-env:yandex-disk-qa:v1"
_APPROVED_DOWNLOAD_HOSTS = (
    "downloader.disk.yandex.net",
    "downloader.disk.yandex.ru",
)
_JSON_RESPONSE_BYTES = 2 * 1024 * 1024


class YandexQualificationApiBinding(DefinitionModel):
    """Exact non-production authority and private expected identity for #678."""

    schema_version: Literal["yandex-qualification-api-binding:v1"]
    deployment_mode: Literal["qualification"]
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    credential_reference: OpaqueProviderReference
    grant_reference: OpaqueProviderReference
    resource_scope_reference: OpaqueProviderReference
    authorization_generation: StrictInt = Field(ge=1)
    configuration_generation: StrictInt = Field(ge=1)
    external_secret_reference: ExternalSecretReference
    expected_user_id: SecretStr
    client_id: Literal["9b7e91e61c894ad896570742978cf5ce"]
    oauth_scope: Literal["cloud_api:disk.read"] = _QUALIFICATION_OAUTH_SCOPE
    approved_root: Literal["/Spine-Qualification-678-v1"]

    @field_validator(
        "workspace_id",
        "environment_id",
        "connection_id",
        "connection_version_id",
    )
    @classmethod
    def validate_ids(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("qualification API binding IDs must be non-nil")
        return value

    @field_validator("expected_user_id")
    @classmethod
    def validate_expected_user_id(cls, value: SecretStr) -> SecretStr:
        identifier = value.get_secret_value()
        if not identifier or len(identifier) > 255:
            raise ValueError("expected user ID is invalid")
        return value

    @field_validator("external_secret_reference")
    @classmethod
    def validate_external_secret_reference(
        cls, value: ExternalSecretReference
    ) -> ExternalSecretReference:
        if (
            value.provider_reference != _QUALIFICATION_PROVIDER_REFERENCE
            or value.secret_reference != _QUALIFICATION_SECRET_REFERENCE
        ):
            raise ValueError("external secret reference is not approved")
        return value


class YandexQualificationRequestPolicy(DefinitionModel):
    """Fixed operational ceiling approved for the isolated qualification."""

    schema_version: Literal["spine-yandex-disk-qualification-ops:v1"] = (
        "spine-yandex-disk-qualification-ops:v1"
    )
    requests_per_second: Literal[1.0] = 1.0
    burst: Literal[2] = 2
    max_concurrency: Literal[1] = 1
    timeout_seconds: StrictFloat = Field(default=10.0, gt=0, le=30)
    max_pages: Literal[10] = 10
    max_items: Literal[32] = 32
    max_file_bytes: Literal[262144] = 256 * 1024
    max_scan_bytes: Literal[2097152] = 2 * 1024 * 1024
    max_retry_after_seconds: StrictFloat = Field(default=30.0, ge=0, le=60)


@dataclass(frozen=True, slots=True, repr=False)
class YandexDiskApiRequest:
    method: Literal["GET"]
    url: str
    headers: Mapping[str, str]
    query: Mapping[str, str]
    timeout_seconds: float
    max_response_bytes: int


@dataclass(frozen=True, slots=True, repr=False)
class YandexDiskApiResponse:
    status_code: int
    json_body: Mapping[str, Any] | None = None
    content: bytes = b""
    headers: Mapping[str, str] | None = None


class YandexDiskApiTransport(Protocol):
    async def request(self, request: YandexDiskApiRequest) -> YandexDiskApiResponse: ...


@dataclass(frozen=True, slots=True)
class HttpxYandexDiskApiTransport:
    """Bounded streaming transport that never surfaces provider response text."""

    client: httpx.AsyncClient

    async def request(self, request: YandexDiskApiRequest) -> YandexDiskApiResponse:
        content = bytearray()
        try:
            async with self.client.stream(
                request.method,
                request.url,
                headers=dict(request.headers),
                params=dict(request.query),
                timeout=request.timeout_seconds,
                follow_redirects=False,
            ) as response:
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > request.max_response_bytes:
                        raise YandexDiskFileTooLargeError()
                    content.extend(chunk)
                content_type = response.headers.get("content-type", "")
                response_headers: dict[str, str] = {}
                if content_type:
                    response_headers["content-type"] = content_type
                retry_after = response.headers.get("retry-after")
                if retry_after:
                    response_headers["retry-after"] = retry_after
                json_body: Mapping[str, Any] | None = None
                if "json" in content_type.lower() and content:
                    try:
                        candidate = json.loads(content)
                    except (TypeError, ValueError):
                        candidate = None
                    if isinstance(candidate, Mapping):
                        json_body = candidate
                return YandexDiskApiResponse(
                    status_code=response.status_code,
                    json_body=json_body,
                    content=bytes(content),
                    headers=response_headers,
                )
        except asyncio.CancelledError:
            raise
        except YandexDiskFileTooLargeError:
            raise
        except (httpx.TimeoutException, httpx.RequestError):
            raise YandexDiskTransientError() from None


class _RequestGovernor:
    """Single-flight token bucket for the approved 1 rps, burst-2 profile."""

    def __init__(
        self,
        *,
        transport: YandexDiskApiTransport,
        policy: YandexQualificationRequestPolicy,
        monotonic: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self._transport = transport
        self._rate = policy.requests_per_second
        self._capacity = float(policy.burst)
        self._tokens = self._capacity
        self._updated_at = monotonic()
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = asyncio.Lock()

    async def request(self, request: YandexDiskApiRequest) -> YandexDiskApiResponse:
        async with self._lock:
            now = self._monotonic()
            self._tokens = min(
                self._capacity,
                self._tokens + max(0.0, now - self._updated_at) * self._rate,
            )
            self._updated_at = now
            if self._tokens < 1.0:
                await self._sleep((1.0 - self._tokens) / self._rate)
                now = self._monotonic()
                self._tokens = min(
                    self._capacity,
                    self._tokens + max(0.0, now - self._updated_at) * self._rate,
                )
                self._updated_at = now
            self._tokens = max(0.0, self._tokens - 1.0)
            return await self._transport.request(request)


class YandexDiskApiService:
    """Concrete read-only service; every Disk operation re-verifies token identity."""

    def __init__(
        self,
        *,
        binding: YandexQualificationApiBinding,
        credential_resolver: YandexDiskCredentialResolver,
        transport: YandexDiskApiTransport,
        request_policy: YandexQualificationRequestPolicy,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._binding = binding
        self._resolver = credential_resolver
        self._policy = request_policy
        self._governor = _RequestGovernor(
            transport=transport,
            policy=request_policy,
            monotonic=monotonic,
            sleep=sleep,
        )

    async def list_resources(
        self,
        connection: ProviderConnection,
        resource_scope_reference: OpaqueProviderReference,
        *,
        offset: int,
        limit: int,
    ) -> YandexDiskResourcePage:
        self._require_connection(connection, resource_scope_reference)
        if (
            type(offset) is not int
            or type(limit) is not int
            or offset < 0
            or limit < 1
            or limit != self._policy.max_items
            or offset >= self._policy.max_items
        ):
            raise YandexDiskContractError()
        token = await self._verified_token(connection)
        payload = await self._disk_json(
            token,
            _DISK_RESOURCES_URL,
            {
                "path": _disk_path(self._binding.approved_root),
                "offset": str(offset),
                "limit": str(limit),
            },
        )
        embedded = payload.get("_embedded")
        if not isinstance(embedded, Mapping):
            raise YandexDiskContractError()
        raw_items = embedded.get("items")
        if not isinstance(raw_items, list):
            raise YandexDiskContractError()
        try:
            embedded_path = _required_string(embedded, "path")
            if embedded_path != _disk_path(self._binding.approved_root):
                raise YandexDiskContractError()
            items = tuple(
                _resource(item, approved_root=self._binding.approved_root)
                for item in raw_items
                if isinstance(item, Mapping)
            )
            if len(items) != len(raw_items):
                raise YandexDiskContractError()
            total = _required_int(embedded, "total")
            if total > self._policy.max_items:
                raise YandexDiskContractError()
            return YandexDiskResourcePage(
                _embedded={
                    "path": embedded_path,
                    "items": items,
                    "limit": _required_int(embedded, "limit"),
                    "offset": _required_int(embedded, "offset"),
                    "total": total,
                }
            )
        except (TypeError, ValueError):
            raise YandexDiskContractError() from None

    async def get_resource(
        self,
        connection: ProviderConnection,
        resource_scope_reference: OpaqueProviderReference,
        *,
        path: str,
    ) -> YandexDiskResource:
        self._require_connection(connection, resource_scope_reference)
        self._require_path(path)
        token = await self._verified_token(connection)
        payload = await self._disk_json(
            token, _DISK_RESOURCES_URL, {"path": path}
        )
        return _resource(payload, approved_root=self._binding.approved_root)

    async def download(
        self,
        connection: ProviderConnection,
        resource_scope_reference: OpaqueProviderReference,
        *,
        path: str,
        revision: str,
        max_bytes: int,
    ) -> YandexDiskFileContent:
        self._require_connection(connection, resource_scope_reference)
        self._require_path(path)
        if (
            not revision
            or type(max_bytes) is not int
            or max_bytes < 1
            or max_bytes > self._policy.max_file_bytes
        ):
            raise YandexDiskFileTooLargeError()
        token = await self._verified_token(connection)
        payload = await self._disk_json(
            token, _DISK_DOWNLOAD_URL, {"path": path}
        )
        href = _required_string(payload, "href")
        if payload.get("method") != "GET" or payload.get("templated") is not False:
            raise YandexDiskContractError()
        _require_signed_download_url(href)
        response = await self._request(
            YandexDiskApiRequest(
                method="GET",
                url=href,
                headers={},
                query={},
                timeout_seconds=self._policy.timeout_seconds,
                max_response_bytes=max_bytes,
            )
        )
        self._raise_for_status(response, identity=False)
        if len(response.content) > max_bytes:
            raise YandexDiskFileTooLargeError()
        content_type = (response.headers or {}).get("content-type", "")
        media_type = content_type.split(";", 1)[0].strip()
        if not media_type:
            raise YandexDiskContractError()
        return YandexDiskFileContent(content=response.content, media_type=media_type)

    def _require_connection(
        self,
        connection: ProviderConnection,
        resource_scope_reference: OpaqueProviderReference,
    ) -> None:
        binding = self._binding
        if (
            connection.status is not ProviderConnectionStatus.ACTIVE
            or connection.provider_kind is not ProviderKind.YANDEX_DISK
            or resource_scope_reference != binding.resource_scope_reference
            or resource_scope_reference
            not in connection.approved_resource_scope_references
            or (
                connection.workspace_id,
                connection.environment_id,
                connection.connection_id,
                connection.connection_version_id,
                connection.credential_reference,
                connection.grant_reference,
                connection.authorization_generation,
                connection.configuration_generation,
            )
            != (
                binding.workspace_id,
                binding.environment_id,
                binding.connection_id,
                binding.connection_version_id,
                binding.credential_reference,
                binding.grant_reference,
                binding.authorization_generation,
                binding.configuration_generation,
            )
        ):
            raise YandexDiskScopeOutOfScopeError()

    def _require_path(self, path: str) -> None:
        if not _path_is_in_root(path, self._binding.approved_root):
            raise YandexDiskScopeOutOfScopeError()

    async def _verified_token(self, connection: ProviderConnection) -> str:
        try:
            secret = await self._resolver.resolve(
                self._binding.external_secret_reference,
                workspace_id=connection.workspace_id,
                environment_id=connection.environment_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise YandexDiskAuthorizationRevokedError() from None
        token = secret.get_secret_value()
        if not token:
            raise YandexDiskAuthorizationRevokedError()
        response = await self._request(
            YandexDiskApiRequest(
                method="GET",
                url=_IDENTITY_URL,
                headers={"Authorization": f"OAuth {token}"},
                query={"format": "json"},
                timeout_seconds=self._policy.timeout_seconds,
                max_response_bytes=_JSON_RESPONSE_BYTES,
            )
        )
        self._raise_for_status(response, identity=True)
        identity = response.json_body
        if (
            identity is None
            or identity.get("id") != self._binding.expected_user_id.get_secret_value()
            or identity.get("client_id") != self._binding.client_id
        ):
            raise YandexDiskAuthorizationRevokedError()
        return token

    async def _disk_json(
        self, token: str, url: str, query: Mapping[str, str]
    ) -> Mapping[str, Any]:
        response = await self._request(
            YandexDiskApiRequest(
                method="GET",
                url=url,
                headers={"Authorization": f"OAuth {token}"},
                query=query,
                timeout_seconds=self._policy.timeout_seconds,
                max_response_bytes=_JSON_RESPONSE_BYTES,
            )
        )
        self._raise_for_status(response, identity=False)
        if response.json_body is None:
            raise YandexDiskContractError()
        return response.json_body

    async def _request(self, request: YandexDiskApiRequest) -> YandexDiskApiResponse:
        try:
            return await self._governor.request(request)
        except asyncio.CancelledError:
            raise
        except (
            YandexDiskAuthorizationRevokedError,
            YandexDiskContractError,
            YandexDiskFileTooLargeError,
            YandexDiskPermissionLostError,
            YandexDiskResourceNotFoundError,
            YandexDiskThrottleError,
            YandexDiskTransientError,
        ):
            raise
        except Exception:
            raise YandexDiskTransientError() from None

    def _raise_for_status(
        self, response: YandexDiskApiResponse, *, identity: bool
    ) -> None:
        if 200 <= response.status_code < 300:
            return
        if response.status_code == 401 or (identity and response.status_code == 403):
            raise YandexDiskAuthorizationRevokedError()
        if response.status_code == 403:
            raise YandexDiskPermissionLostError()
        if response.status_code == 404:
            raise YandexDiskResourceNotFoundError()
        if response.status_code == 429:
            retry_after = _retry_after(response.headers)
            raise YandexDiskThrottleError(
                retry_after_seconds=min(
                    retry_after, self._policy.max_retry_after_seconds
                )
            )
        if response.status_code >= 500:
            raise YandexDiskTransientError()
        raise YandexDiskContractError()


def _disk_path(approved_root: str) -> str:
    return f"disk:{approved_root}"


def _path_is_in_root(path: str, approved_root: str) -> bool:
    root = _disk_path(approved_root)
    if not isinstance(path, str) or not path or len(path) > 2048:
        return False
    if "\\" in path or "\x00" in path or "//" in path:
        return False
    if path != root and not path.startswith(f"{root}/"):
        return False
    suffix = path[len("disk:/") :]
    return all(segment not in {"", ".", ".."} for segment in suffix.split("/"))


def _require_signed_download_url(value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _APPROVED_DOWNLOAD_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.fragment
    ):
        raise YandexDiskContractError()


def _resource(
    payload: Mapping[str, Any], *, approved_root: str
) -> YandexDiskResource:
    path = _required_string(payload, "path")
    if not _path_is_in_root(path, approved_root):
        raise YandexDiskScopeOutOfScopeError()
    revision = payload.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, (str, int)):
        revision_value = None
    else:
        revision_value = str(revision)
    try:
        return YandexDiskResource(
            name=_required_string(payload, "name"),
            path=path,
            type=payload.get("type"),
            mime_type=_optional_string(payload, "mime_type"),
            size=_optional_int(payload, "size"),
            md5=_optional_string(payload, "md5"),
            sha256=_optional_string(payload, "sha256"),
            revision=revision_value,
            modified=_optional_datetime(payload, "modified"),
            resource_id=_optional_string(payload, "resource_id"),
        )
    except (TypeError, ValueError):
        raise YandexDiskContractError() from None


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise YandexDiskContractError()
    return value


def _optional_string(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise YandexDiskContractError()
    return value


def _required_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise YandexDiskContractError()
    return value


def _optional_int(payload: Mapping[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    return _required_int(payload, key)


def _optional_datetime(
    payload: Mapping[str, Any], key: str
) -> datetime | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise YandexDiskContractError()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise YandexDiskContractError() from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise YandexDiskContractError()
    return parsed


def _retry_after(headers: Mapping[str, str] | None) -> float:
    if headers is None:
        return 1.0
    raw = headers.get("retry-after")
    if raw is None:
        return 1.0
    try:
        value = float(raw)
    except ValueError:
        return 1.0
    return value if value >= 0 else 1.0


__all__ = [
    "HttpxYandexDiskApiTransport",
    "YandexDiskApiRequest",
    "YandexDiskApiResponse",
    "YandexDiskApiService",
    "YandexDiskApiTransport",
    "YandexQualificationApiBinding",
    "YandexQualificationRequestPolicy",
]
