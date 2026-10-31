from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID

import pytest
import httpx

from spine.connectors.google_api import (
    GoogleApiEndpoints,
    GoogleApiRequest,
    GoogleApiRequestPolicy,
    GoogleApiResponse,
    GoogleApiService,
    GoogleDesktopOAuthMaterial,
    GoogleOAuthClientCredentials,
    GoogleOAuthRefreshGrant,
    GoogleResourceScope,
    HttpxGoogleApiTransport,
    ReferenceGoogleCredentialResolver,
)
from spine.application.connections.google import (
    GoogleProviderAuthorizationError,
    GoogleProviderContractError,
    GoogleProviderPolicyError,
    GoogleProviderQuotaError,
    GoogleProviderReadError,
)
from spine.connectors.google_api import GoogleResourceScopeError
from spine.connectors.google_drive import GoogleDriveAccessOutcomeError
from spine.connectors.google_sheets import GoogleSheetsColumn, GoogleSheetsReadProfile
from spine.domain.connections.google import (
    GoogleConnection,
    GoogleConnectionStatus,
    OpaqueGoogleReference,
)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


RESOURCE_SCOPE = OpaqueGoogleReference(value=_id(10))
NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)


def _connection() -> GoogleConnection:
    return GoogleConnection(
        connection_version_id=_id(1),
        connection_id=_id(2),
        approval_id=_id(3),
        version=1,
        workspace_id=_id(4),
        environment_id=_id(5),
        service_principal_id=_id(6),
        credential_reference=OpaqueGoogleReference(value=_id(7)),
        grant_reference=OpaqueGoogleReference(value=_id(8)),
        approved_resource_scope_references=(RESOURCE_SCOPE,),
        authorization_generation=9,
        configuration_generation=11,
        status=GoogleConnectionStatus.ACTIVE,
    )


class RecordingCredentialResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[OpaqueGoogleReference, OpaqueGoogleReference]] = []

    async def resolve(self, credential_reference, grant_reference):
        self.calls.append((credential_reference, grant_reference))
        return GoogleDesktopOAuthMaterial(
            client_id="desktop-client-id",
            client_secret="desktop-client-secret",
            refresh_token="desktop-refresh-token",
        )


class RecordingTransport:
    def __init__(self, responses: list[GoogleApiResponse]) -> None:
        self.responses = responses
        self.requests = []

    async def request(self, request):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class RecordingRetryObserver:
    def __init__(self) -> None:
        self.events = []

    async def record_retry(self, event):
        self.events.append(event)


def _service(
    resolver,
    transport,
    *,
    sleep=None,
    sheet_profiles: tuple[GoogleSheetsReadProfile, ...] = (),
    sheets_page_size: int = 200,
    retry_observer=None,
    drive_id: str | None = None,
    max_requests_per_run: int = 500,
    jitter=None,
) -> GoogleApiService:
    return GoogleApiService(
        endpoints=GoogleApiEndpoints(
            drive_base_url="https://www.googleapis.test/drive/v3",
            sheets_base_url="https://sheets.googleapis.test/v4",
            oauth_token_url="https://oauth2.googleapis.test/token",
            approved_oauth_scopes=(
                "https://www.googleapis.com/auth/drive.readonly",
                "https://www.googleapis.com/auth/spreadsheets.readonly",
            ),
            provider_api_version="google-drive:v3+google-sheets:v4",
            configuration_version="approved-google-runtime:v7",
        ),
        request_policy=GoogleApiRequestPolicy(
            timeout_seconds=7.5,
            max_attempts=2,
            max_requests_per_run=max_requests_per_run,
            max_concurrent_requests=2,
            backoff_ceiling_seconds=(1.0,),
            drive_page_size=100,
            sheets_page_size=sheets_page_size,
        ),
        resource_scopes=(
            GoogleResourceScope(
                reference=RESOURCE_SCOPE,
                workspace_id=_connection().workspace_id,
                environment_id=_connection().environment_id,
                connection_id=_connection().connection_id,
                authorization_generation=9,
                configuration_generation=11,
                allowed_file_ids=("file-1",),
                allowed_spreadsheet_ids=("sheet-1",),
                folder_id="folder-1",
                drive_id=drive_id,
                supports_all_drives=drive_id is not None,
            ),
        ),
        credential_resolver=resolver,
        transport=transport,
        retry_observer=retry_observer or RecordingRetryObserver(),
        sheet_profiles=sheet_profiles,
        clock=lambda: NOW,
        sleep=sleep or (lambda _delay: _completed_sleep()),
        jitter=jitter or (lambda _low, high: high),
    )


async def _completed_sleep() -> None:
    return None


@pytest.mark.asyncio
async def test_drive_metadata_resolves_opaque_credentials_and_maps_scoped_request() -> (
    None
):
    resolver = RecordingCredentialResolver()
    transport = RecordingTransport(
        [
            GoogleApiResponse(
                status_code=200,
                json_body={"access_token": "access-secret", "expires_in": 3600},
            ),
            GoogleApiResponse(
                status_code=200,
                json_body={
                    "id": "file-1",
                    "name": "Synthetic proposal.docx",
                    "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "version": "42",
                    "webViewLink": "https://drive.test/file-1",
                    "parents": ["folder-1"],
                    "modifiedTime": "2026-10-29T11:59:00Z",
                },
            ),
        ]
    )

    result = await _service(resolver, transport).get_metadata_for_file(
        _connection(), RESOURCE_SCOPE, "file-1"
    )

    assert result.file_id == "file-1"
    assert result.version == "42"
    assert resolver.calls == [
        (_connection().credential_reference, _connection().grant_reference)
    ]
    oauth_request, drive_request = transport.requests
    assert oauth_request.url == "https://oauth2.googleapis.test/token"
    assert oauth_request.form == {
        "client_id": "desktop-client-id",
        "client_secret": "desktop-client-secret",
        "refresh_token": "desktop-refresh-token",
        "grant_type": "refresh_token",
        "scope": (
            "https://www.googleapis.com/auth/drive.readonly "
            "https://www.googleapis.com/auth/spreadsheets.readonly"
        ),
    }
    assert oauth_request.timeout_seconds == 7.5
    assert drive_request.url == "https://www.googleapis.test/drive/v3/files/file-1"
    assert drive_request.query == {
        "fields": (
            "id,name,mimeType,version,webViewLink,driveId,parents,modifiedTime,trashed"
        ),
        "supportsAllDrives": "false",
    }
    assert drive_request.headers == {"Authorization": "Bearer access-secret"}
    assert drive_request.timeout_seconds == 7.5


@pytest.mark.asyncio
async def test_out_of_scope_file_fails_before_credentials_or_transport() -> None:
    resolver = RecordingCredentialResolver()
    transport = RecordingTransport([])

    with pytest.raises(Exception, match="outside the approved Google resource scope"):
        await _service(resolver, transport).get_metadata_for_file(
            _connection(), RESOURCE_SCOPE, "not-allowlisted"
        )

    assert resolver.calls == []
    assert transport.requests == []


@pytest.mark.asyncio
async def test_stale_runtime_scope_generation_fails_before_credentials_or_transport() -> (
    None
):
    resolver = RecordingCredentialResolver()
    transport = RecordingTransport([])
    rotated = _connection().model_copy(update={"configuration_generation": 12})

    with pytest.raises(Exception, match="outside the approved Google resource scope"):
        await _service(resolver, transport).get_metadata_for_file(
            rotated, RESOURCE_SCOPE, "file-1"
        )

    assert resolver.calls == []
    assert transport.requests == []


@pytest.mark.asyncio
async def test_drive_snapshot_and_changes_map_every_provider_page() -> None:
    resolver = RecordingCredentialResolver()
    transport = RecordingTransport(
        [
            GoogleApiResponse(
                status_code=200,
                json_body={"access_token": "token", "expires_in": 3600},
            ),
            GoogleApiResponse(
                status_code=200,
                json_body={
                    "nextPageToken": "page-2",
                    "incompleteSearch": False,
                    "files": [{"id": "file-2"}],
                },
            ),
            GoogleApiResponse(
                status_code=200,
                json_body={
                    "newStartPageToken": "start-9",
                    "changes": [
                        {
                            "changeId": "change-1",
                            "fileId": "file-2",
                            "removed": False,
                            "file": {
                                "id": "file-2",
                                "version": "7",
                                "trashed": False,
                                "parents": ["folder-1"],
                            },
                        }
                    ],
                },
            ),
        ]
    )
    service = _service(resolver, transport)

    snapshot = await service.list_page(_connection(), RESOURCE_SCOPE, None)
    changes = await service.list_changes(
        _connection(), RESOURCE_SCOPE, snapshot.next_page_token
    )

    assert [item.file_id for item in snapshot.items] == ["file-2"]
    assert snapshot.next_page_token == "page-2"
    assert changes.new_start_page_token == "start-9"
    assert changes.changes[0].change_key == "id:change-1"
    list_request, change_request = transport.requests[1:]
    assert list_request.query["q"] == "'folder-1' in parents and trashed = false"
    assert list_request.query["pageSize"] == "100"
    assert change_request.query["pageToken"] == "page-2"
    assert change_request.query["includeRemoved"] == "true"


@pytest.mark.asyncio
async def test_shared_drive_requests_bind_all_pages_to_exact_drive_identity() -> None:
    transport = RecordingTransport(
        [
            GoogleApiResponse(
                status_code=200,
                json_body={"access_token": "token", "expires_in": 3600},
            ),
            GoogleApiResponse(status_code=200, json_body={"startPageToken": "s0"}),
            GoogleApiResponse(
                status_code=200,
                json_body={
                    "incompleteSearch": False,
                    "files": [
                        {"id": "file-1", "driveId": "drive-1"},
                        {"id": "unlisted-file", "driveId": "drive-1"},
                    ],
                },
            ),
            GoogleApiResponse(
                status_code=200,
                json_body={
                    "newStartPageToken": "s1",
                    "changes": [
                        {
                            "changeId": "c1",
                            "fileId": "file-1",
                            "file": {
                                "id": "file-1",
                                "driveId": "drive-1",
                                "version": "3",
                                "parents": ["folder-1"],
                            },
                        }
                    ],
                },
            ),
        ]
    )
    service = _service(RecordingCredentialResolver(), transport, drive_id="drive-1")

    assert await service.get_start_page_token(_connection(), RESOURCE_SCOPE) == "s0"
    snapshot = await service.list_page(_connection(), RESOURCE_SCOPE, None)
    changes = await service.list_changes(_connection(), RESOURCE_SCOPE, "s0")

    assert tuple((item.file_id, item.drive_id) for item in snapshot.items) == (
        ("file-1", "drive-1"),
    )
    assert tuple(change.file_id for change in changes.changes) == ("file-1",)
    assert changes.new_start_page_token == "s1"
    start_query, list_query, changes_query = [
        request.query for request in transport.requests[1:]
    ]
    assert start_query == {"supportsAllDrives": "true", "driveId": "drive-1"}
    assert list_query["corpora"] == "drive"
    assert list_query["driveId"] == "drive-1"
    assert list_query["supportsAllDrives"] == "true"
    assert list_query["includeItemsFromAllDrives"] == "true"
    assert list_query["fields"] == "nextPageToken,incompleteSearch,files(id,driveId)"
    assert changes_query["driveId"] == "drive-1"
    assert changes_query["restrictToMyDrive"] == "false"
    assert changes_query["supportsAllDrives"] == "true"
    assert changes_query["includeItemsFromAllDrives"] == "true"


@pytest.mark.asyncio
async def test_shared_drive_snapshot_rejects_cross_drive_item() -> None:
    transport = RecordingTransport(
        [
            GoogleApiResponse(
                status_code=200,
                json_body={"access_token": "token", "expires_in": 3600},
            ),
            GoogleApiResponse(
                status_code=200,
                json_body={
                    "incompleteSearch": False,
                    "files": [{"id": "foreign-file", "driveId": "drive-2"}],
                },
            ),
        ]
    )
    service = _service(RecordingCredentialResolver(), transport, drive_id="drive-1")

    with pytest.raises(GoogleResourceScopeError):
        await service.list_page(_connection(), RESOURCE_SCOPE, None)


@pytest.mark.asyncio
async def test_shared_drive_metadata_rejects_allowlisted_id_from_another_drive() -> None:
    transport = RecordingTransport(
        [
            GoogleApiResponse(
                status_code=200,
                json_body={"access_token": "token", "expires_in": 3600},
            ),
            GoogleApiResponse(
                status_code=200,
                json_body={
                    "id": "file-1",
                    "name": "Synthetic file",
                    "mimeType": "text/plain",
                    "version": "1",
                    "driveId": "drive-2",
                    "parents": ["folder-1"],
                },
            ),
        ]
    )
    service = _service(RecordingCredentialResolver(), transport, drive_id="drive-1")

    with pytest.raises(GoogleResourceScopeError):
        await service.get_metadata_for_file(_connection(), RESOURCE_SCOPE, "file-1")


@pytest.mark.asyncio
async def test_shared_drive_unlisted_file_fails_before_credentials_or_transport() -> None:
    resolver = RecordingCredentialResolver()
    transport = RecordingTransport([])
    service = _service(resolver, transport, drive_id="drive-1")

    with pytest.raises(GoogleResourceScopeError):
        await service.get_metadata_for_file(
            _connection(), RESOURCE_SCOPE, "unlisted-file"
        )

    assert resolver.calls == []
    assert transport.requests == []


@pytest.mark.asyncio
async def test_shared_drive_permission_denial_is_typed_and_sanitized() -> None:
    transport = RecordingTransport(
        [
            GoogleApiResponse(
                status_code=200,
                json_body={"access_token": "token", "expires_in": 3600},
            ),
            GoogleApiResponse(
                status_code=403,
                json_body={
                    "error": {
                        "status": "PERMISSION_DENIED",
                        "message": "private drive and document contents",
                    }
                },
            ),
        ]
    )
    service = _service(RecordingCredentialResolver(), transport, drive_id="drive-1")

    with pytest.raises(GoogleProviderPolicyError) as raised:
        await service.get_metadata_for_file(_connection(), RESOURCE_SCOPE, "file-1")

    assert "private drive" not in str(raised.value)
    assert "document contents" not in str(raised.value)
    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_shared_drive_changes_with_unknown_or_foreign_identity_are_incomplete() -> None:
    transport = RecordingTransport(
        [
            GoogleApiResponse(
                status_code=200,
                json_body={"access_token": "token", "expires_in": 3600},
            ),
            GoogleApiResponse(
                status_code=200,
                json_body={
                    "newStartPageToken": "s2",
                    "changes": [
                        {
                            "changeId": "foreign",
                            "fileId": "unlisted-foreign-file",
                            "file": {
                                "id": "unlisted-foreign-file",
                                "driveId": "drive-2",
                            },
                        },
                        {
                            "changeId": "unlisted-same-drive",
                            "fileId": "unlisted-shared-file",
                            "file": {
                                "id": "unlisted-shared-file",
                                "driveId": "drive-1",
                            },
                        },
                        {
                            "changeId": "allowlisted-wrong-drive",
                            "fileId": "file-1",
                            "file": {"id": "file-1", "driveId": "drive-2"},
                        },
                        {"changeId": "unknown", "fileId": "file-1", "removed": True},
                        {
                            "changeId": "outside-folder",
                            "fileId": "file-1",
                            "file": {
                                "id": "file-1",
                                "driveId": "drive-1",
                                "parents": ["another-folder"],
                            },
                        },
                    ],
                },
            ),
        ]
    )
    service = _service(RecordingCredentialResolver(), transport, drive_id="drive-1")

    page = await service.list_changes(_connection(), RESOURCE_SCOPE, "s1")

    assert tuple(change.file_id for change in page.changes) == ("file-1",)
    assert page.new_start_page_token == "s2"
    assert page.incomplete_search is True


@pytest.mark.asyncio
async def test_shared_drive_change_outside_configured_folder_is_incomplete() -> None:
    transport = RecordingTransport(
        [
            GoogleApiResponse(
                status_code=200,
                json_body={"access_token": "token", "expires_in": 3600},
            ),
            GoogleApiResponse(
                status_code=200,
                json_body={
                    "newStartPageToken": "s3",
                    "changes": [
                        {
                            "changeId": "moved",
                            "fileId": "file-1",
                            "file": {
                                "id": "file-1",
                                "driveId": "drive-1",
                                "parents": ["another-folder"],
                            },
                        }
                    ],
                },
            ),
        ]
    )
    service = _service(RecordingCredentialResolver(), transport, drive_id="drive-1")

    page = await service.list_changes(_connection(), RESOURCE_SCOPE, "s2")

    assert page.changes == ()
    assert page.new_start_page_token == "s3"
    assert page.incomplete_search is True


@pytest.mark.asyncio
async def test_shared_drive_change_without_identity_is_incomplete() -> None:
    transport = RecordingTransport(
        [
            GoogleApiResponse(
                status_code=200,
                json_body={"access_token": "token", "expires_in": 3600},
            ),
            GoogleApiResponse(
                status_code=200,
                json_body={
                    "newStartPageToken": "s4",
                    "changes": [
                        {
                            "changeId": "identity-missing",
                            "fileId": "file-1",
                            "file": {"id": "file-1", "version": "9"},
                        }
                    ],
                },
            ),
        ]
    )
    service = _service(RecordingCredentialResolver(), transport, drive_id="drive-1")

    page = await service.list_changes(_connection(), RESOURCE_SCOPE, "s3")

    assert page.changes == ()
    assert page.new_start_page_token == "s4"
    assert page.incomplete_search is True


@pytest.mark.asyncio
async def test_shared_drive_scope_does_not_expand_spreadsheet_allowlist() -> None:
    resolver = RecordingCredentialResolver()
    transport = RecordingTransport([])
    service = _service(
        resolver,
        transport,
        drive_id="drive-1",
        sheet_profiles=(_sheet_profile(),),
    )

    with pytest.raises(GoogleResourceScopeError):
        await service.get_spreadsheet(
            _connection(), RESOURCE_SCOPE, "other-spreadsheet", 123
        )

    assert resolver.calls == []
    assert transport.requests == []


@pytest.mark.asyncio
async def test_drive_changes_exclude_unrelated_folder_items_from_read_authority() -> (
    None
):
    resolver = RecordingCredentialResolver()
    transport = RecordingTransport(
        [
            GoogleApiResponse(
                status_code=200,
                json_body={"access_token": "token", "expires_in": 3600},
            ),
            GoogleApiResponse(
                status_code=200,
                json_body={
                    "newStartPageToken": "start-10",
                    "changes": [
                        {
                            "changeId": "unrelated-change",
                            "fileId": "unrelated-file",
                            "removed": False,
                            "file": {
                                "id": "unrelated-file",
                                "version": "1",
                                "trashed": False,
                                "parents": ["different-folder"],
                            },
                        }
                    ],
                },
            ),
        ]
    )
    service = _service(resolver, transport)

    page = await service.list_changes(
        _connection(), RESOURCE_SCOPE, "approved-change-token"
    )

    assert page.changes == ()
    assert page.incomplete_search is True
    with pytest.raises(GoogleResourceScopeError):
        await service.get_metadata_for_file(
            _connection(), RESOURCE_SCOPE, "unrelated-file"
        )
    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_discovered_file_moved_out_of_folder_fails_current_membership_check() -> (
    None
):
    transport = RecordingTransport(
        [
            GoogleApiResponse(
                status_code=200,
                json_body={"access_token": "token", "expires_in": 3600},
            ),
            GoogleApiResponse(
                status_code=200,
                json_body={
                    "incompleteSearch": False,
                    "files": [{"id": "file-2"}],
                },
            ),
            GoogleApiResponse(
                status_code=200,
                json_body={
                    "id": "file-2",
                    "name": "Moved",
                    "mimeType": "text/plain",
                    "version": "2",
                    "parents": ["different-folder"],
                    "trashed": False,
                },
            ),
        ]
    )
    service = _service(RecordingCredentialResolver(), transport)
    await service.list_page(_connection(), RESOURCE_SCOPE, None)

    with pytest.raises(GoogleResourceScopeError):
        await service.get_metadata_for_file(_connection(), RESOURCE_SCOPE, "file-2")
    with pytest.raises(GoogleResourceScopeError):
        await service.get_metadata_for_file(_connection(), RESOURCE_SCOPE, "file-2")

    assert len(transport.requests) == 3


@pytest.mark.asyncio
async def test_folder_change_mapping_is_stable_after_worker_restart() -> None:
    removed_change = {
        "newStartPageToken": "start-11",
        "changes": [
            {
                "changeId": "removed-change",
                "fileId": "file-2",
                "removed": True,
            }
        ],
    }
    cached_service = _service(
        RecordingCredentialResolver(),
        RecordingTransport(
            [
                GoogleApiResponse(
                    status_code=200,
                    json_body={"access_token": "token", "expires_in": 3600},
                ),
                GoogleApiResponse(
                    status_code=200,
                    json_body={
                        "incompleteSearch": False,
                        "files": [{"id": "file-2"}],
                    },
                ),
                GoogleApiResponse(status_code=200, json_body=removed_change),
            ]
        ),
    )
    fresh_service = _service(
        RecordingCredentialResolver(),
        RecordingTransport(
            [
                GoogleApiResponse(
                    status_code=200,
                    json_body={"access_token": "token", "expires_in": 3600},
                ),
                GoogleApiResponse(status_code=200, json_body=removed_change),
            ]
        ),
    )

    await cached_service.list_page(_connection(), RESOURCE_SCOPE, None)
    cached = await cached_service.list_changes(
        _connection(), RESOURCE_SCOPE, "change-token"
    )
    fresh = await fresh_service.list_changes(
        _connection(), RESOURCE_SCOPE, "change-token"
    )

    assert cached == fresh
    assert fresh.changes == ()
    assert fresh.incomplete_search is True


@pytest.mark.asyncio
async def test_drive_download_and_export_preserve_bytes_and_media_type() -> None:
    resolver = RecordingCredentialResolver()
    transport = RecordingTransport(
        [
            GoogleApiResponse(
                status_code=200,
                json_body={"access_token": "token", "expires_in": 3600},
            ),
            GoogleApiResponse(
                status_code=200,
                content=b"docx-bytes",
                headers={
                    "Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                },
            ),
            GoogleApiResponse(
                status_code=200,
                content=b"pdf-bytes",
                headers={"content-type": "application/pdf; charset=binary"},
            ),
        ]
    )
    service = _service(resolver, transport)

    downloaded = await service.download(_connection(), RESOURCE_SCOPE, "file-1", "42")
    exported = await service.export(
        _connection(), RESOURCE_SCOPE, "file-1", "42", "application/pdf"
    )

    assert downloaded.content == b"docx-bytes"
    assert downloaded.media_type.endswith("wordprocessingml.document")
    assert exported.content == b"pdf-bytes"
    assert exported.media_type == "application/pdf"
    assert transport.requests[1].query["alt"] == "media"
    assert transport.requests[2].query == {"mimeType": "application/pdf"}


@pytest.mark.asyncio
async def test_transient_provider_failure_retries_with_configured_backoff() -> None:
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    resolver = RecordingCredentialResolver()
    transport = RecordingTransport(
        [
            GoogleApiResponse(
                status_code=200,
                json_body={"access_token": "token", "expires_in": 3600},
            ),
            GoogleApiResponse(status_code=503, json_body={}),
            GoogleApiResponse(
                status_code=200,
                json_body={
                    "id": "file-1",
                    "name": "file.txt",
                    "mimeType": "text/plain",
                    "version": "1",
                },
            ),
        ]
    )

    result = await _service(resolver, transport, sleep=sleep).get_metadata_for_file(
        _connection(), RESOURCE_SCOPE, "file-1"
    )

    assert result.version == "1"
    assert delays == [1.0]


@pytest.mark.asyncio
async def test_retry_after_is_a_minimum_wait_and_over_ceiling_hard_stops() -> None:
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    within_transport = RecordingTransport(
        [
            GoogleApiResponse(
                status_code=200,
                json_body={"access_token": "token", "expires_in": 3600},
            ),
            GoogleApiResponse(
                status_code=429,
                json_body={"error": {}},
                headers={"Retry-After": "2"},
            ),
            GoogleApiResponse(
                status_code=200,
                json_body={"id": "file-1", "name": "ok", "mimeType": "text/plain", "version": "1"},
            ),
        ]
    )
    await _service(
        RecordingCredentialResolver(), within_transport, sleep=sleep
    ).get_metadata_for_file(_connection(), RESOURCE_SCOPE, "file-1")
    assert delays == [2.0]

    over_transport = RecordingTransport(
        [
            GoogleApiResponse(
                status_code=200,
                json_body={"access_token": "token", "expires_in": 3600},
            ),
            GoogleApiResponse(
                status_code=429,
                json_body={"error": {}},
                headers={"Retry-After": "31"},
            ),
        ]
    )
    with pytest.raises(GoogleProviderQuotaError):
        await _service(RecordingCredentialResolver(), over_transport).get_metadata_for_file(
            _connection(), RESOURCE_SCOPE, "file-1"
        )
    assert len(over_transport.requests) == 2


@pytest.mark.asyncio
async def test_run_request_budget_counts_oauth_and_retries_before_sending() -> None:
    transport = RecordingTransport(
        [
            GoogleApiResponse(
                status_code=200,
                json_body={"access_token": "token", "expires_in": 3600},
            ),
            GoogleApiResponse(status_code=503, json_body={}),
        ]
    )
    service = _service(
        RecordingCredentialResolver(), transport, max_requests_per_run=2
    )

    with service.using_run_request_budget() as budget:
        with pytest.raises(GoogleProviderQuotaError):
            await service.get_metadata_for_file(_connection(), RESOURCE_SCOPE, "file-1")

    assert budget.requests == 2
    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_aggregate_concurrency_and_grant_refresh_are_shared() -> None:
    class ConcurrentTransport:
        def __init__(self) -> None:
            self.active = 0
            self.maximum_active = 0
            self.oauth_requests = 0
            self.provider_requests = 0
            self.two_provider_requests_active = asyncio.Event()
            self.release_provider_requests = asyncio.Event()

        async def request(self, request):
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            try:
                if request.method == "POST":
                    self.oauth_requests += 1
                    await asyncio.sleep(0)
                    return GoogleApiResponse(
                        status_code=200,
                        json_body={"access_token": "token", "expires_in": 3600},
                    )
                self.provider_requests += 1
                if self.provider_requests >= 2:
                    self.two_provider_requests_active.set()
                await self.release_provider_requests.wait()
                return GoogleApiResponse(
                    status_code=200,
                    json_body={
                        "id": "file-1",
                        "name": "ok",
                        "mimeType": "text/plain",
                        "version": "1",
                    },
                )
            finally:
                self.active -= 1

    transport = ConcurrentTransport()
    service = _service(RecordingCredentialResolver(), transport)
    with service.using_run_request_budget() as budget:
        calls = [
            asyncio.create_task(
                service.get_metadata_for_file(
                    _connection(), RESOURCE_SCOPE, "file-1"
                )
            )
            for _ in range(3)
        ]
        await asyncio.wait_for(transport.two_provider_requests_active.wait(), 1)
        assert transport.maximum_active == 2
        transport.release_provider_requests.set()
        await asyncio.gather(*calls)

    assert transport.oauth_requests == 1
    assert transport.provider_requests == 3
    assert transport.maximum_active == 2
    assert budget.requests == 4


@pytest.mark.asyncio
async def test_cancellation_during_provider_request_starts_no_retry() -> None:
    started = asyncio.Event()

    class BlockingTransport:
        def __init__(self) -> None:
            self.requests = []

        async def request(self, request):
            self.requests.append(request)
            if request.method == "POST":
                return GoogleApiResponse(
                    status_code=200,
                    json_body={"access_token": "token", "expires_in": 3600},
                )
            started.set()
            await asyncio.Event().wait()

    transport = BlockingTransport()
    service = _service(RecordingCredentialResolver(), transport)
    with service.using_run_request_budget():
        task = asyncio.create_task(
            service.get_metadata_for_file(_connection(), RESOURCE_SCOPE, "file-1")
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_full_jitter_source_controls_retry_delay() -> None:
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    transport = RecordingTransport(
        [
            GoogleApiResponse(
                status_code=200,
                json_body={"access_token": "token", "expires_in": 3600},
            ),
            GoogleApiResponse(status_code=503, json_body={}),
            GoogleApiResponse(
                status_code=200,
                json_body={"id": "file-1", "name": "ok", "mimeType": "text/plain", "version": "1"},
            ),
        ]
    )
    await _service(
        RecordingCredentialResolver(),
        transport,
        sleep=sleep,
        jitter=lambda low, high: low + (high - low) / 2,
    ).get_metadata_for_file(_connection(), RESOURCE_SCOPE, "file-1")

    assert delays == [0.5]


@pytest.mark.asyncio
async def test_revoked_refresh_grant_maps_to_sanitized_authorization_outcome() -> None:
    resolver = RecordingCredentialResolver()
    transport = RecordingTransport(
        [
            GoogleApiResponse(
                status_code=400,
                json_body={
                    "error": "invalid_grant",
                    "error_description": "desktop-refresh-token is revoked",
                },
            )
        ]
    )

    with pytest.raises(GoogleProviderAuthorizationError) as raised:
        await _service(resolver, transport).get_metadata_for_file(
            _connection(), RESOURCE_SCOPE, "file-1"
        )

    assert "desktop-refresh-token" not in str(raised.value)
    assert "invalid_grant" not in str(raised.value)
    assert len(transport.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "error_type"),
    (
        ("invalid_client", GoogleProviderAuthorizationError),
        ("invalid_scope", GoogleProviderPolicyError),
        ("unsupported_grant_type", GoogleProviderContractError),
    ),
)
async def test_nonretryable_oauth_failures_are_typed_without_retry(
    reason, error_type
) -> None:
    transport = RecordingTransport(
        [GoogleApiResponse(status_code=400, json_body={"error": reason})]
    )

    with pytest.raises(error_type):
        await _service(RecordingCredentialResolver(), transport).get_metadata_for_file(
            _connection(), RESOURCE_SCOPE, "file-1"
        )

    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_ambiguous_oauth_transport_failure_is_not_retried() -> None:
    observer = RecordingRetryObserver()
    transport = RecordingTransport([GoogleProviderReadError()])

    with pytest.raises(GoogleProviderReadError):
        await _service(
            RecordingCredentialResolver(), transport, retry_observer=observer
        ).get_metadata_for_file(_connection(), RESOURCE_SCOPE, "file-1")

    assert len(transport.requests) == 1
    assert observer.events == []


@pytest.mark.asyncio
async def test_terminal_oauth_quota_is_not_retried() -> None:
    transport = RecordingTransport(
        [
            GoogleApiResponse(
                status_code=429,
                json_body={
                    "error": {"errors": [{"reason": "dailyLimitExceeded"}]}
                },
            ),
            GoogleApiResponse(
                status_code=200,
                json_body={"access_token": "unexpected retry", "expires_in": 3600},
            ),
        ]
    )

    with pytest.raises(GoogleProviderQuotaError):
        await _service(RecordingCredentialResolver(), transport).get_metadata_for_file(
            _connection(), RESOURCE_SCOPE, "file-1"
        )

    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_provider_unauthorized_invalidates_token_and_refreshes_once() -> None:
    resolver = RecordingCredentialResolver()
    transport = RecordingTransport(
        [
            GoogleApiResponse(
                status_code=200,
                json_body={"access_token": "first-token", "expires_in": 3600},
            ),
            GoogleApiResponse(status_code=401, json_body={}),
            GoogleApiResponse(
                status_code=200,
                json_body={"access_token": "second-token", "expires_in": 3600},
            ),
            GoogleApiResponse(
                status_code=200,
                json_body={
                    "id": "file-1",
                    "name": "file.txt",
                    "mimeType": "text/plain",
                    "version": "2",
                },
            ),
        ]
    )

    result = await _service(resolver, transport).get_metadata_for_file(
        _connection(), RESOURCE_SCOPE, "file-1"
    )

    assert result.version == "2"
    assert len(resolver.calls) == 2
    assert transport.requests[-1].headers == {"Authorization": "Bearer second-token"}


@pytest.mark.asyncio
async def test_provider_timeout_retries_only_to_explicit_attempt_limit() -> None:
    resolver = RecordingCredentialResolver()
    transport = RecordingTransport(
        [
            GoogleApiResponse(
                status_code=200,
                json_body={"access_token": "token", "expires_in": 3600},
            ),
            TimeoutError("raw socket detail"),
            TimeoutError("different raw socket detail"),
        ]
    )

    with pytest.raises(Exception) as raised:
        await _service(resolver, transport).get_metadata_for_file(
            _connection(), RESOURCE_SCOPE, "file-1"
        )

    assert str(raised.value) == "Google provider read failed."
    assert len(transport.requests) == 3


def _sheet_profile() -> GoogleSheetsReadProfile:
    return GoogleSheetsReadProfile(
        spreadsheet_id="sheet-1",
        sheet_id=123,
        business_id_column="business_id",
        columns=(
            GoogleSheetsColumn(column_key="business_id", coordinate="A"),
            GoogleSheetsColumn(column_key="amount", coordinate="B"),
            GoogleSheetsColumn(column_key="calculated", coordinate="C"),
            GoogleSheetsColumn(column_key="optional", coordinate="D"),
        ),
        developer_metadata_key="spine_business_id",
    )


def _spreadsheet_payload(*, spreadsheet_id: str = "sheet-1") -> dict:
    return {
        "spreadsheetId": spreadsheet_id,
        "properties": {"locale": "ru_RU", "timeZone": "Europe/Moscow"},
        "sheets": [
            {
                "properties": {"sheetId": 123, "title": "Estimate Register"},
                "developerMetadata": [
                    {
                        "metadataId": 501,
                        "metadataKey": "spine_business_id",
                        "metadataValue": "EST-001",
                        "location": {
                            "dimensionRange": {
                                "sheetId": 123,
                                "dimension": "ROWS",
                                "startIndex": 1,
                                "endIndex": 2,
                            }
                        },
                    },
                    {
                        "metadataId": 502,
                        "metadataKey": "spine_business_id",
                        "metadataValue": "EST-002",
                        "location": {
                            "dimensionRange": {
                                "sheetId": 123,
                                "dimension": "ROWS",
                                "startIndex": 2,
                                "endIndex": 3,
                            }
                        },
                    },
                ],
                "data": [
                    {
                        "startRow": 1,
                        "startColumn": 0,
                        "rowData": [
                            {
                                "values": [
                                    {
                                        "userEnteredValue": {"stringValue": "EST-001"},
                                        "effectiveValue": {"stringValue": "EST-001"},
                                        "formattedValue": "EST-001",
                                        "effectiveFormat": {
                                            "numberFormat": {"type": "TEXT"}
                                        },
                                    },
                                    {
                                        "userEnteredValue": {"numberValue": 1250.5},
                                        "effectiveValue": {"numberValue": 1250.5},
                                        "formattedValue": "1 250,50 ₽",
                                        "effectiveFormat": {
                                            "numberFormat": {
                                                "type": "CURRENCY",
                                                "pattern": "#,##0.00 [$₽-ru-RU]",
                                            }
                                        },
                                    },
                                    {
                                        "userEnteredValue": {"formulaValue": "=B2*2"},
                                        "effectiveValue": {"numberValue": 2501},
                                        "formattedValue": "2 501,00 ₽",
                                        "effectiveFormat": {
                                            "numberFormat": {"type": "CURRENCY"}
                                        },
                                    },
                                    {},
                                ]
                            },
                            {
                                "values": [
                                    {
                                        "userEnteredValue": {"stringValue": "EST-002"},
                                        "effectiveValue": {"stringValue": "EST-002"},
                                        "formattedValue": "EST-002",
                                        "effectiveFormat": {
                                            "numberFormat": {"type": "TEXT"}
                                        },
                                    },
                                    {
                                        "userEnteredValue": {"numberValue": 10},
                                        "effectiveValue": {"numberValue": 10},
                                        "formattedValue": "10",
                                        "effectiveFormat": {
                                            "numberFormat": {"type": "NUMBER"}
                                        },
                                    },
                                    {
                                        "userEnteredValue": {"formulaValue": "=B3*2"},
                                        "effectiveValue": {
                                            "errorValue": {"type": "DIVIDE_BY_ZERO"}
                                        },
                                        "formattedValue": "#DIV/0!",
                                        "effectiveFormat": {
                                            "numberFormat": {"type": "NUMBER"}
                                        },
                                    },
                                    {},
                                ]
                            },
                        ],
                    }
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_sheets_range_page_preserves_representations_metadata_and_pagination() -> (
    None
):
    resolver = RecordingCredentialResolver()
    transport = RecordingTransport(
        [
            GoogleApiResponse(
                status_code=200,
                json_body={"access_token": "token", "expires_in": 3600},
            ),
            GoogleApiResponse(
                status_code=200, json_body={"id": "sheet-1", "version": "77"}
            ),
            GoogleApiResponse(status_code=200, json_body=_spreadsheet_payload()),
            GoogleApiResponse(
                status_code=200, json_body={"id": "sheet-1", "version": "77"}
            ),
        ]
    )

    result = await _service(
        resolver,
        transport,
        sheet_profiles=(_sheet_profile(),),
        sheets_page_size=2,
    ).get_spreadsheet_range_page(
        _connection(), RESOURCE_SCOPE, "sheet-1", 123, "'Estimate Register'!A2:D5", None
    )

    assert result.complete is False
    assert result.next_page_token == "4"
    assert result.provider_version_before == result.provider_version_after == "77"
    assert result.snapshot.locale == "ru_RU"
    assert result.snapshot.time_zone == "Europe/Moscow"
    first = result.snapshot.rows[0]
    assert first.a1_range == "A2:D2"
    assert first.developer_metadata[0].metadata_id == 501
    assert first.cells[0].entered.model_dump() == {
        "kind": "string",
        "value": "EST-001",
    }
    assert first.cells[1].effective.value == "1250.5"
    assert first.cells[1].formatted == "1 250,50 ₽"
    assert first.cells[1].number_format.pattern == "#,##0.00 [$₽-ru-RU]"
    assert first.cells[2].entered.kind == "formula"
    assert first.cells[2].effective.value == "2501"
    assert first.cells[3].entered.kind == "blank"
    assert result.snapshot.rows[1].cells[2].effective.kind == "error"
    assert transport.requests[2].query["ranges"] == "'Estimate Register'!A2:D3"


@pytest.mark.asyncio
async def test_sparse_sheets_page_uses_configured_range_for_continuation() -> None:
    payload = _spreadsheet_payload()
    payload["sheets"][0]["data"][0]["rowData"] = []
    transport = RecordingTransport(
        [
            GoogleApiResponse(
                status_code=200,
                json_body={"access_token": "token", "expires_in": 3600},
            ),
            GoogleApiResponse(
                status_code=200, json_body={"id": "sheet-1", "version": "77"}
            ),
            GoogleApiResponse(status_code=200, json_body=payload),
            GoogleApiResponse(
                status_code=200, json_body={"id": "sheet-1", "version": "77"}
            ),
        ]
    )

    result = await _service(
        RecordingCredentialResolver(),
        transport,
        sheet_profiles=(_sheet_profile(),),
        sheets_page_size=2,
    ).get_spreadsheet_range_page(
        _connection(), RESOURCE_SCOPE, "sheet-1", 123, "'Estimate Register'!A2:D5", None
    )

    assert result.complete is False
    assert result.next_page_token == "4"
    assert result.snapshot.rows == ()


@pytest.mark.asyncio
async def test_sheets_grid_start_outside_requested_page_fails_closed() -> None:
    payload = _spreadsheet_payload()
    payload["sheets"][0]["data"][0]["startRow"] = 0
    transport = RecordingTransport(
        [
            GoogleApiResponse(
                status_code=200,
                json_body={"access_token": "token", "expires_in": 3600},
            ),
            GoogleApiResponse(
                status_code=200, json_body={"id": "sheet-1", "version": "77"}
            ),
            GoogleApiResponse(status_code=200, json_body=payload),
        ]
    )

    with pytest.raises(Exception, match="outside the requested page range"):
        await _service(
            RecordingCredentialResolver(),
            transport,
            sheet_profiles=(_sheet_profile(),),
            sheets_page_size=2,
        ).get_spreadsheet_range_page(
            _connection(),
            RESOURCE_SCOPE,
            "sheet-1",
            123,
            "'Estimate Register'!A2:D5",
            None,
        )


@pytest.mark.asyncio
async def test_sheets_range_must_cover_every_configured_profile_column() -> None:
    resolver = RecordingCredentialResolver()
    transport = RecordingTransport([])

    with pytest.raises(Exception, match="cover every configured column"):
        await _service(
            resolver,
            transport,
            sheet_profiles=(_sheet_profile(),),
        ).get_spreadsheet_range_page(
            _connection(),
            RESOURCE_SCOPE,
            "sheet-1",
            123,
            "'Estimate Register'!A2:A5",
            None,
        )

    assert resolver.calls == []
    assert transport.requests == []


@pytest.mark.asyncio
async def test_sheets_misscoped_response_fails_closed_without_returning_rows() -> None:
    resolver = RecordingCredentialResolver()
    transport = RecordingTransport(
        [
            GoogleApiResponse(
                status_code=200,
                json_body={"access_token": "token", "expires_in": 3600},
            ),
            GoogleApiResponse(
                status_code=200,
                json_body=_spreadsheet_payload(spreadsheet_id="different-sheet"),
            ),
        ]
    )

    with pytest.raises(Exception, match="outside the configured spreadsheet"):
        await _service(
            resolver, transport, sheet_profiles=(_sheet_profile(),)
        ).get_spreadsheet(_connection(), RESOURCE_SCOPE, "sheet-1", 123)


@pytest.mark.asyncio
async def test_reference_resolver_keeps_client_and_grant_behind_opaque_references() -> (
    None
):
    class Store:
        def __init__(self) -> None:
            self.calls = []

        async def resolve_client(self, reference):
            self.calls.append(("client", reference))
            return GoogleOAuthClientCredentials("client", "client-secret")

        async def resolve_grant(self, reference):
            self.calls.append(("grant", reference))
            return GoogleOAuthRefreshGrant("refresh-secret")

    store = Store()

    material = await ReferenceGoogleCredentialResolver(store).resolve(
        _connection().credential_reference, _connection().grant_reference
    )

    assert store.calls == [
        ("client", _connection().credential_reference),
        ("grant", _connection().grant_reference),
    ]
    assert "client-secret" not in repr(material)
    assert "refresh-secret" not in repr(material)


@pytest.mark.asyncio
async def test_httpx_transport_maps_request_without_exposing_raw_request_errors() -> (
    None
):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"id": "file-1"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await HttpxGoogleApiTransport(client).request(
            GoogleApiRequest(
                method="GET",
                url="https://google.test/files/file-1",
                headers={"Authorization": "Bearer secret"},
                query={"fields": "id"},
                form=None,
                timeout_seconds=3.0,
            )
        )

    assert response.json_body == {"id": "file-1"}
    assert seen[0].url == "https://google.test/files/file-1?fields=id"


def test_google_endpoints_reject_non_https_provider_origins() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        GoogleApiEndpoints(
            drive_base_url="http://www.googleapis.test/drive/v3",
            sheets_base_url="https://sheets.googleapis.test/v4",
            oauth_token_url="https://oauth2.googleapis.test/token",
            approved_oauth_scopes=("https://example.test/scope",),
            provider_api_version="google:v1",
            configuration_version="approved:v1",
        )


def test_google_request_policy_requires_explicit_bounded_controls() -> None:
    with pytest.raises(ValueError):
        GoogleApiRequestPolicy(
            timeout_seconds=30.1,
            max_attempts=1,
            max_requests_per_run=500,
            max_concurrent_requests=2,
            backoff_ceiling_seconds=(),
            drive_page_size=100,
            sheets_page_size=200,
        )

    with pytest.raises(ValueError):
        GoogleApiRequestPolicy(
            timeout_seconds=30.0,
            max_attempts=2,
            max_requests_per_run=500,
            max_concurrent_requests=2,
            backoff_ceiling_seconds=(1.1,),
            drive_page_size=100,
            sheets_page_size=200,
        )

    with pytest.raises(ValueError):
        GoogleApiRequestPolicy(
            timeout_seconds=30.0,
            max_attempts=5,
            max_requests_per_run=500,
            max_concurrent_requests=2,
            backoff_ceiling_seconds=(1.0, 2.0, 4.0, 4.0),
            drive_page_size=100,
            sheets_page_size=200,
        )

    with pytest.raises(ValueError):
        GoogleApiRequestPolicy(
            timeout_seconds=30.0,
            max_attempts=4,
            max_requests_per_run=500,
            max_concurrent_requests=2,
            backoff_ceiling_seconds=(1.0, 0.5, 4.0),
            drive_page_size=100,
            sheets_page_size=200,
        )


@pytest.mark.asyncio
async def test_cross_workspace_connection_is_denied_before_credentials_or_http() -> (
    None
):
    resolver = RecordingCredentialResolver()
    transport = RecordingTransport([])
    other_workspace = _connection().model_copy(update={"workspace_id": _id(999)})

    with pytest.raises(GoogleResourceScopeError):
        await _service(resolver, transport).get_metadata_for_file(
            other_workspace, RESOURCE_SCOPE, "file-1"
        )

    assert resolver.calls == []
    assert transport.requests == []


@pytest.mark.asyncio
async def test_credential_resolver_failure_is_sanitized_before_transport() -> None:
    class FailingResolver:
        async def resolve(self, credential_reference, grant_reference):
            del credential_reference, grant_reference
            raise RuntimeError("raw-secret-from-store")

    transport = RecordingTransport([])

    with pytest.raises(GoogleProviderReadError) as captured:
        await _service(FailingResolver(), transport).get_metadata_for_file(
            _connection(), RESOURCE_SCOPE, "file-1"
        )

    assert "raw-secret-from-store" not in str(captured.value)
    assert transport.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("responses", "error_type", "provider_attempts"),
    (
        (
            [GoogleApiResponse(status_code=403, json_body={"error": {}})],
            GoogleProviderPolicyError,
            1,
        ),
        (
            [
                GoogleApiResponse(status_code=429, json_body={"error": {}}),
                GoogleApiResponse(status_code=429, json_body={"error": {}}),
            ],
            GoogleProviderQuotaError,
            2,
        ),
        (
            [
                GoogleApiResponse(
                    status_code=403,
                    json_body={
                        "error": {
                            "status": "PERMISSION_DENIED",
                            "errors": [{"reason": "rateLimitExceeded"}],
                        }
                    },
                ),
                GoogleApiResponse(
                    status_code=403,
                    json_body={
                        "error": {
                            "status": "PERMISSION_DENIED",
                            "errors": [{"reason": "rateLimitExceeded"}],
                        }
                    },
                ),
            ],
            GoogleProviderQuotaError,
            2,
        ),
        (
            [
                GoogleApiResponse(
                    status_code=403,
                    json_body={
                        "error": {"errors": [{"reason": "quotaExceeded"}]}
                    },
                ),
                GoogleApiResponse(
                    status_code=200,
                    json_body={"id": "file-1", "name": "unexpected retry", "mimeType": "text/plain", "version": "1"},
                ),
            ],
            GoogleProviderQuotaError,
            1,
        ),
        (
            [
                GoogleApiResponse(
                    status_code=403,
                    json_body={
                        "error": {"errors": [{"reason": "dailyLimitExceeded"}]}
                    },
                ),
                GoogleApiResponse(status_code=200, json_body={}),
            ],
            GoogleProviderQuotaError,
            1,
        ),
        (
            [GoogleApiResponse(status_code=400, json_body={"error": {}})],
            GoogleProviderContractError,
            1,
        ),
        (
            [GoogleApiResponse(status_code=404, json_body={"error": {}})],
            GoogleDriveAccessOutcomeError,
            1,
        ),
    ),
)
async def test_provider_failures_are_typed_and_only_retryable_failures_retry(
    responses, error_type, provider_attempts
) -> None:
    resolver = RecordingCredentialResolver()
    retry_observer = RecordingRetryObserver()
    transport = RecordingTransport(
        [
            GoogleApiResponse(
                status_code=200,
                json_body={"access_token": "token", "expires_in": 3600},
            ),
            *responses,
        ]
    )

    with pytest.raises(error_type):
        await _service(
            resolver, transport, retry_observer=retry_observer
        ).get_metadata_for_file(_connection(), RESOURCE_SCOPE, "file-1")

    assert len(transport.requests) == 1 + provider_attempts
    assert len(retry_observer.events) == provider_attempts - 1
    if retry_observer.events:
        event = retry_observer.events[0]
        assert event.workspace_id == _connection().workspace_id
        assert event.environment_id == _connection().environment_id
        assert event.connection_id == _connection().connection_id
        assert event.operation == "provider"
        assert event.schema_version == "google-api-retry:v1"
        assert not hasattr(event, "request_url")
        assert not hasattr(event, "response_body")


@pytest.mark.asyncio
async def test_retry_observer_failure_does_not_change_provider_retry_outcome() -> None:
    class FailingRetryObserver:
        async def record_retry(self, event):
            del event
            raise RuntimeError("diagnostic sink unavailable")

    transport = RecordingTransport(
        [
            GoogleApiResponse(
                status_code=200,
                json_body={"access_token": "token", "expires_in": 3600},
            ),
            GoogleApiResponse(status_code=503, json_body={"error": {}}),
            GoogleApiResponse(
                status_code=200,
                json_body={
                    "id": "file-1",
                    "name": "Recovered",
                    "mimeType": "text/plain",
                    "version": "2",
                },
            ),
        ]
    )

    result = await _service(
        RecordingCredentialResolver(),
        transport,
        retry_observer=FailingRetryObserver(),
    ).get_metadata_for_file(_connection(), RESOURCE_SCOPE, "file-1")

    assert result.version == "2"
    assert len(transport.requests) == 3
