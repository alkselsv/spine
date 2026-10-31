from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from spine.application.yandex_disk_scan import (
    YandexDiskAuthorizationRevokedError,
    YandexDiskContractError,
    YandexDiskFileTooLargeError,
    YandexDiskPermissionLostError,
    YandexDiskProviderUnavailableError,
    YandexDiskResourceNotFoundError,
    YandexDiskScopeOutOfScopeError,
    YandexDiskThrottleError,
    YandexDiskTransientError,
)
from spine.auth import ExternalSecretReference
from spine.connectors.yandex_disk import yandex_disk_capabilities
from spine.connectors.yandex_disk_api import (
    HttpxYandexDiskApiTransport,
    YandexDiskApiRequest,
    YandexDiskApiResponse,
    YandexDiskApiService,
    YandexQualificationApiBinding,
    YandexQualificationRequestGovernor,
    YandexQualificationRequestPolicy,
)
from spine.connectors.yandex_disk_composition import (
    compose_yandex_qualification_reader,
)
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderConnection,
    ProviderConnectionStatus,
    ProviderKind,
)
from spine.infrastructure.yandex_qualification_credentials import (
    YANDEX_QUALIFICATION_CLIENT_ID,
    YANDEX_QUALIFICATION_PROVIDER_REFERENCE,
    YANDEX_QUALIFICATION_ROOT,
    YANDEX_QUALIFICATION_SECRET_REFERENCE,
)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{value:012d}")


SCOPE = OpaqueProviderReference(value=_id(9))
TOKEN_REFERENCE = ExternalSecretReference(
    provider_reference=YANDEX_QUALIFICATION_PROVIDER_REFERENCE,
    secret_reference=YANDEX_QUALIFICATION_SECRET_REFERENCE,
)


def _connection(**overrides: object) -> ProviderConnection:
    values: dict[str, object] = {
        "connection_version_id": _id(1),
        "connection_id": _id(2),
        "approval_id": _id(3),
        "version": 1,
        "workspace_id": _id(4),
        "environment_id": _id(5),
        "service_principal_id": _id(6),
        "provider_kind": ProviderKind.YANDEX_DISK,
        "credential_reference": OpaqueProviderReference(value=_id(7)),
        "grant_reference": OpaqueProviderReference(value=_id(8)),
        "approved_resource_scope_references": (SCOPE,),
        "authorization_generation": 2,
        "configuration_generation": 3,
        "status": ProviderConnectionStatus.ACTIVE,
        "capabilities": yandex_disk_capabilities(
            qualification_reference=OpaqueProviderReference(value=_id(10))
        ),
    }
    values.update(overrides)
    return ProviderConnection.model_validate(values)


def _binding(**overrides: object) -> YandexQualificationApiBinding:
    connection = _connection()
    values: dict[str, object] = {
        "schema_version": "yandex-qualification-api-binding:v1",
        "deployment_mode": "qualification",
        "workspace_id": connection.workspace_id,
        "environment_id": connection.environment_id,
        "connection_id": connection.connection_id,
        "connection_version_id": connection.connection_version_id,
        "credential_reference": connection.credential_reference,
        "grant_reference": connection.grant_reference,
        "resource_scope_reference": SCOPE,
        "authorization_generation": connection.authorization_generation,
        "configuration_generation": connection.configuration_generation,
        "external_secret_reference": TOKEN_REFERENCE,
        "expected_user_id": SecretStr("dedicated-user-id"),
        "client_id": YANDEX_QUALIFICATION_CLIENT_ID,
        "approved_root": YANDEX_QUALIFICATION_ROOT,
    }
    values.update(overrides)
    return YandexQualificationApiBinding.model_validate(values)


class CredentialResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def resolve(self, reference, *, workspace_id, environment_id):
        self.calls.append((reference, workspace_id, environment_id))
        return SecretStr("synthetic-secret-token")


class Transport:
    def __init__(self, responses: list[YandexDiskApiResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[YandexDiskApiRequest] = []

    async def request(self, request: YandexDiskApiRequest) -> YandexDiskApiResponse:
        self.requests.append(request)
        return self.responses.pop(0)


async def _no_sleep() -> None:
    return None


def _identity(
    *,
    user_id: str = "dedicated-user-id",
    client_id: str = YANDEX_QUALIFICATION_CLIENT_ID,
) -> YandexDiskApiResponse:
    return YandexDiskApiResponse(
        status_code=200,
        json_body={"id": user_id, "client_id": client_id, "login": "qualification"},
    )


def _resource_payload(
    *, path: str = "disk:/Spine-Qualification-678-v1/brief.pdf"
) -> dict[str, Any]:
    return {
        "name": "brief.pdf",
        "path": path,
        "type": "file",
        "mime_type": "application/pdf",
        "size": 4,
        "md5": "8d777f385d3dfec8815d20f7496026dc",
        "sha256": "3a6eb0790f39ac87c94f3856b2dd2c5d110e6811602261a9a923d3bb23adc8b7",
        "revision": 1700000000000000,
        "modified": "2026-10-29T12:00:00+00:00",
        "resource_id": "1573541:abc123",
        "extra_provider_field": "ignored-at-mapping-boundary",
    }


def _listing_response() -> YandexDiskApiResponse:
    return YandexDiskApiResponse(
        status_code=200,
        json_body={
            "_embedded": {
                "path": "disk:/Spine-Qualification-678-v1",
                "items": [_resource_payload()],
                "limit": 32,
                "offset": 0,
                "total": 1,
            }
        },
    )


def _service(
    transport: Transport, resolver: CredentialResolver | None = None
) -> YandexDiskApiService:
    policy = YandexQualificationRequestPolicy()
    now = 0.0

    async def advance(delay: float) -> None:
        nonlocal now
        now += delay

    return YandexDiskApiService(
        binding=_binding(),
        credential_resolver=resolver or CredentialResolver(),
        transport=transport,
        request_policy=policy,
        request_governor=YandexQualificationRequestGovernor(
            policy=policy,
            monotonic=lambda: now,
            sleep=advance,
        ),
    )


@pytest.mark.asyncio
async def test_list_resources_verifies_identity_before_disk_and_maps_shape() -> None:
    transport = Transport(
        [
            _identity(),
            YandexDiskApiResponse(
                status_code=200,
                json_body={
                    "_embedded": {
                        "path": "disk:/Spine-Qualification-678-v1",
                        "items": [_resource_payload()],
                        "limit": 32,
                        "offset": 0,
                        "total": 1,
                    },
                    "name": "Spine-Qualification-678-v1",
                    "type": "dir",
                },
            ),
        ]
    )

    page = await _service(transport).list_resources(
        _connection(), SCOPE, offset=0, limit=32
    )

    assert [request.url for request in transport.requests] == [
        "https://login.yandex.ru/info",
        "https://cloud-api.yandex.net/v1/disk/resources",
    ]
    assert transport.requests[0].query == {"format": "json"}
    assert transport.requests[1].query == {
        "path": "disk:/Spine-Qualification-678-v1",
        "offset": "0",
        "limit": "32",
    }
    assert all(
        request.headers["Authorization"] == "OAuth synthetic-secret-token"
        for request in transport.requests
    )
    assert page.embedded.items[0].revision == "1700000000000000"
    assert page.embedded.path == "disk:/Spine-Qualification-678-v1"


@pytest.mark.asyncio
async def test_list_resources_allows_only_an_explicit_descendant_directory() -> None:
    directory = "disk:/Spine-Qualification-678-v1/nested"
    transport = Transport(
        [
            _identity(),
            YandexDiskApiResponse(
                status_code=200,
                json_body={
                    "_embedded": {
                        "path": directory,
                        "items": [_resource_payload(path=f"{directory}/brief.pdf")],
                        "limit": 32,
                        "offset": 0,
                        "total": 1,
                    }
                },
            ),
        ]
    )

    page = await _service(transport).list_resources(
        _connection(), SCOPE, path=directory, offset=0, limit=32
    )

    assert page.embedded.path == directory
    assert transport.requests[1].query["path"] == directory


@pytest.mark.asyncio
async def test_list_resources_rejects_sibling_directory_before_secret_resolution() -> None:
    resolver = CredentialResolver()
    transport = Transport([])

    with pytest.raises(YandexDiskScopeOutOfScopeError):
        await _service(transport, resolver).list_resources(
            _connection(),
            SCOPE,
            path="disk:/Spine-Qualification-678-other",
            offset=0,
            limit=32,
        )

    assert resolver.calls == []
    assert transport.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_id", "client_id"),
    [
        ("wrong-user", YANDEX_QUALIFICATION_CLIENT_ID),
        ("dedicated-user-id", "wrong-client"),
    ],
)
async def test_identity_mismatch_fails_closed_before_disk_request(
    user_id: str, client_id: str
) -> None:
    transport = Transport([_identity(user_id=user_id, client_id=client_id)])

    with pytest.raises(YandexDiskAuthorizationRevokedError):
        await _service(transport).get_resource(
            _connection(),
            SCOPE,
            path="disk:/Spine-Qualification-678-v1/brief.pdf",
        )

    assert [request.url for request in transport.requests] == [
        "https://login.yandex.ru/info"
    ]
    assert "dedicated-user-id" not in repr(_binding())


@pytest.mark.asyncio
async def test_out_of_scope_path_is_denied_before_token_resolution() -> None:
    resolver = CredentialResolver()
    transport = Transport([])

    with pytest.raises(YandexDiskScopeOutOfScopeError):
        await _service(transport, resolver).get_resource(
            _connection(), SCOPE, path="disk:/other/private.pdf"
        )

    assert resolver.calls == []
    assert transport.requests == []


@pytest.mark.asyncio
async def test_connection_mismatch_is_denied_before_token_resolution() -> None:
    resolver = CredentialResolver()
    transport = Transport([])
    different_connection = _connection(connection_id=_id(99))

    with pytest.raises(YandexDiskScopeOutOfScopeError):
        await _service(transport, resolver).list_resources(
            different_connection, SCOPE, offset=0, limit=32
        )

    assert resolver.calls == []
    assert transport.requests == []


@pytest.mark.asyncio
async def test_download_approved_https_host_enforces_byte_limit() -> None:
    transport = Transport(
        [
            _identity(),
            YandexDiskApiResponse(
                status_code=200,
                json_body={
                    "href": "https://downloader.disk.yandex.ru/disk/signed",
                    "method": "GET",
                    "templated": False,
                },
            ),
            YandexDiskApiResponse(
                status_code=200,
                content=b"data",
                headers={"content-type": "application/pdf"},
            ),
        ]
    )

    result = await _service(transport).download(
        _connection(),
        SCOPE,
        path="disk:/Spine-Qualification-678-v1/brief.pdf",
        revision="1700000000000000",
        max_bytes=4,
    )

    assert result.content == b"data"
    assert transport.requests[-1].url == "https://downloader.disk.yandex.ru/disk/signed"
    assert transport.requests[-1].headers == {}
    assert transport.requests[-1].max_response_bytes == 4


@pytest.mark.asyncio
async def test_download_rejects_unapproved_signed_url_without_fetching_it() -> None:
    transport = Transport(
        [
            _identity(),
            YandexDiskApiResponse(
                status_code=200,
                json_body={
                    "href": "https://attacker.example/collect",
                    "method": "GET",
                    "templated": False,
                },
            ),
        ]
    )

    with pytest.raises(YandexDiskContractError):
        await _service(transport).download(
            _connection(),
            SCOPE,
            path="disk:/Spine-Qualification-678-v1/brief.pdf",
            revision="1700000000000000",
            max_bytes=4,
        )

    assert len(transport.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, YandexDiskAuthorizationRevokedError),
        (403, YandexDiskPermissionLostError),
        (404, YandexDiskResourceNotFoundError),
        (429, YandexDiskThrottleError),
        (503, YandexDiskProviderUnavailableError),
        (400, YandexDiskContractError),
    ],
)
async def test_provider_status_maps_to_content_free_port_error(
    status: int, error_type: type[Exception]
) -> None:
    headers: Mapping[str, str] = {"retry-after": "2"} if status == 429 else {}
    transport = Transport(
        [
            _identity(),
            YandexDiskApiResponse(
                status_code=status,
                json_body={"message": "protected provider detail"},
                headers=headers,
            ),
        ]
    )

    with pytest.raises(error_type) as raised:
        await _service(transport).get_resource(
            _connection(),
            SCOPE,
            path="disk:/Spine-Qualification-678-v1/missing.pdf",
        )

    assert "protected provider detail" not in str(raised.value)
    if status == 429:
        assert raised.value.retry_after_seconds == 2


@pytest.mark.asyncio
async def test_httpx_transport_stops_stream_after_approved_response_budget() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"12345", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = HttpxYandexDiskApiTransport(client=client)
        request = YandexDiskApiRequest(
            method="GET",
            url="https://downloader.disk.yandex.ru/disk/signed",
            headers={},
            query={},
            timeout_seconds=10.0,
            max_response_bytes=4,
        )

        with pytest.raises(YandexDiskFileTooLargeError):
            await transport.request(request)


def test_qualification_request_policy_is_fixed_to_approved_ops_budget() -> None:
    policy = YandexQualificationRequestPolicy()

    assert policy.requests_per_second == 1.0
    assert policy.burst == 2
    assert policy.max_concurrency == 1
    assert policy.max_pages == 10
    assert policy.max_items == 32
    assert policy.max_file_bytes == 256 * 1024
    assert policy.max_scan_bytes == 2 * 1024 * 1024
    with pytest.raises(ValueError):
        YandexQualificationRequestPolicy.model_validate(
            {**policy.model_dump(), "max_concurrency": 2}
        )


@pytest.mark.asyncio
async def test_shared_request_governor_preserves_budget_across_service_instances() -> None:
    now = 0.0
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        nonlocal now
        delays.append(delay)
        now += delay

    policy = YandexQualificationRequestPolicy()
    governor = YandexQualificationRequestGovernor(
        policy=policy,
        monotonic=lambda: now,
        sleep=sleep,
    )
    first_transport = Transport([_identity(), _listing_response()])
    second_transport = Transport([_identity(), _listing_response()])
    first = YandexDiskApiService(
        binding=_binding(),
        credential_resolver=CredentialResolver(),
        transport=first_transport,
        request_policy=policy,
        request_governor=governor,
    )
    second = YandexDiskApiService(
        binding=_binding(),
        credential_resolver=CredentialResolver(),
        transport=second_transport,
        request_policy=policy,
        request_governor=governor,
    )

    await first.list_resources(_connection(), SCOPE, offset=0, limit=32)
    await second.list_resources(_connection(), SCOPE, offset=0, limit=32)

    assert delays == [1.0, 1.0]


@pytest.mark.asyncio
async def test_request_governor_fails_closed_when_budget_cannot_replenish() -> None:
    async def no_progress(_: float) -> None:
        return None

    governor = YandexQualificationRequestGovernor(
        policy=YandexQualificationRequestPolicy(),
        monotonic=lambda: 0.0,
        sleep=no_progress,
    )
    transport = Transport(
        [
            YandexDiskApiResponse(status_code=200),
            YandexDiskApiResponse(status_code=200),
            YandexDiskApiResponse(status_code=200),
        ]
    )
    request = YandexDiskApiRequest(
        method="GET",
        url="https://login.yandex.ru/info",
        headers={},
        query={},
        timeout_seconds=10.0,
        max_response_bytes=1024,
    )

    await governor.request(transport, request)
    await governor.request(transport, request)
    with pytest.raises(YandexDiskTransientError):
        await governor.request(transport, request)

    assert len(transport.requests) == 2


def test_qualification_composition_pins_reader_to_approved_ops_budget() -> None:
    class Repository:
        pass

    async def sleep(_: float) -> None:
        return None

    composition = compose_yandex_qualification_reader(
        binding=_binding(),
        credential_resolver=CredentialResolver(),
        transport=Transport([]),
        connection_repository=Repository(),  # type: ignore[arg-type]
        lifecycle_reader=object(),  # type: ignore[arg-type]
        clock=lambda: datetime.now(timezone.utc),
        sleep=sleep,
    )

    assert composition.read_profile.page_size == 32
    assert composition.read_profile.max_pages == 10
    assert composition.read_profile.max_items == 32
    assert composition.read_profile.max_file_bytes == 256 * 1024
    assert composition.read_profile.max_attempts == 3
    assert composition.read_profile.backoff_seconds == (1.0, 2.0)
