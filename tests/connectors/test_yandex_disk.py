from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from uuid import UUID

import pytest

from spine.application.connections.provider import (
    ProviderReadAdmission,
    ProviderReadCommand,
    ProviderReadDeniedError,
)
from spine.connectors.observations_v2 import (
    BinaryObservationRepresentation,
    ExternalIdentityAssurance,
    ProviderFileRevisionEvidence,
    ProviderObservationDiscovery,
)
from spine.connectors.yandex_disk import (
    YandexDiskAuthorizationRevokedError,
    YandexDiskConcurrentModificationError,
    YandexDiskContractError,
    YandexDiskFileContent,
    YandexDiskFileTooLargeError,
    YandexDiskPartialDownloadError,
    YandexDiskProviderAdapter,
    YandexDiskReadProfile,
    YandexDiskResource,
    YandexDiskResourcePage,
    YandexDiskThrottleError,
    YandexDiskTransientError,
    yandex_disk_capabilities,
)
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapabilityKind,
    ProviderCapabilityStatus,
    ProviderConnection,
    ProviderConnectionStatus,
    ProviderKind,
)


NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{value:012d}")


SCOPE = OpaqueProviderReference(value=_id(9))


def test_binary_revision_evidence_must_match_exact_original_bytes() -> None:
    evidence = ProviderFileRevisionEvidence(
        provider_revision="revision-1",
        resource_name="brief.pdf",
        resource_path="disk:/approved/brief.pdf",
        content_md5=hashlib.md5(b"other").hexdigest(),  # noqa: S324
        content_sha256=hashlib.sha256(b"other").hexdigest(),
        byte_length=len(b"other"),
        modified_at=NOW,
    )

    with pytest.raises(ValueError, match="byte length"):
        BinaryObservationRepresentation(
            original_bytes=b"original",
            media_type="application/pdf",
            extraction_profile="yandex-disk-binary:v1",
            locator_contract_version="locator:yandex-disk-path:v1",
            revision_evidence=evidence,
        )


def _resource(*, revision: str = "1700000000000000", path: str = "disk:/approved/brief.pdf") -> YandexDiskResource:
    content = b"%PDF governed yandex source"
    return YandexDiskResource(
        name="brief.pdf",
        path=path,
        type="file",
        mime_type="application/pdf",
        size=len(content),
        md5=hashlib.md5(content).hexdigest(),  # noqa: S324 - provider evidence
        sha256=hashlib.sha256(content).hexdigest(),
        revision=revision,
        modified="2026-10-29T12:00:00+00:00",
        resource_id="1573541:abc123",
    )


def _connection(
    *,
    workspace_id: UUID = _id(4),
    connection_id: UUID = _id(2),
    status: ProviderConnectionStatus = ProviderConnectionStatus.ACTIVE,
) -> ProviderConnection:
    return ProviderConnection(
        connection_version_id=_id(1),
        connection_id=connection_id,
        approval_id=_id(3),
        version=1,
        workspace_id=workspace_id,
        environment_id=_id(5),
        service_principal_id=_id(6),
        provider_kind=ProviderKind.YANDEX_DISK,
        credential_reference=OpaqueProviderReference(value=_id(7)),
        grant_reference=OpaqueProviderReference(value=_id(8)),
        approved_resource_scope_references=(SCOPE,),
        authorization_generation=2,
        configuration_generation=3,
        status=status,
        capabilities=yandex_disk_capabilities(
            qualification_reference=OpaqueProviderReference(value=_id(10))
        ),
    )


def _command(
    *,
    workspace_id: UUID = _id(4),
    connection_id: UUID = _id(2),
    scope: OpaqueProviderReference = SCOPE,
) -> ProviderReadCommand:
    return ProviderReadCommand(
        workspace_id=workspace_id,
        environment_id=_id(5),
        connection_id=connection_id,
        connection_version_id=_id(1),
        resource_scope_reference=scope,
        authorization_generation=2,
        configuration_generation=3,
        required_capabilities=(
            ProviderCapabilityKind.ENUMERATION,
            ProviderCapabilityKind.METADATA_READ,
            ProviderCapabilityKind.BINARY_READ,
        ),
        resource_object_reference="disk:/approved/brief.pdf",
    )


class Repository:
    def __init__(self, connection: ProviderConnection) -> None:
        self.connection = connection

    @asynccontextmanager
    async def admit_read(self, command, context):
        del command, context
        yield self.connection


class OfficialShapeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.throttle_once = False
        self.after = _resource()
        self.content = b"%PDF governed yandex source"

    async def list_resources(self, connection, scope, *, offset, limit):
        self.calls.append(("list", (connection.credential_reference, connection.grant_reference, scope, offset, limit)))
        if self.throttle_once:
            self.throttle_once = False
            raise YandexDiskThrottleError(retry_after_seconds=0.25)
        item = _resource()
        if offset == 0:
            return YandexDiskResourcePage(
                _embedded={
                    "path": "disk:/approved",
                    "items": [
                        item.model_dump(mode="json"),
                        _resource(path="disk:/approved/second.pdf").model_dump(mode="json"),
                    ],
                    "limit": 2,
                    "offset": 0,
                    "total": 3,
                }
            )
        return YandexDiskResourcePage(
            _embedded={
                "path": "disk:/approved",
                "items": [_resource(path="disk:/approved/other.txt").model_dump(mode="json")],
                "limit": 2,
                "offset": 2,
                "total": 3,
            }
        )

    async def download(self, connection, scope, *, path, revision, max_bytes):
        self.calls.append(("download", (scope, path, revision, max_bytes)))
        return YandexDiskFileContent(content=self.content, media_type="application/pdf")

    async def get_resource(self, connection, scope, *, path):
        self.calls.append(("metadata", (scope, path)))
        return self.after


@pytest.mark.asyncio
async def test_reader_preserves_official_metadata_exact_bytes_and_path_scoped_identity() -> None:
    service = OfficialShapeService()
    adapter = YandexDiskProviderAdapter(
        service=service,
        profile=YandexDiskReadProfile(page_size=2, max_attempts=2),
        clock=lambda: NOW,
        sleep=lambda _: _return_none(),
    )

    observation = await adapter.read(_connection(), _command())

    assert observation.provider_kind is ProviderKind.YANDEX_DISK
    assert observation.original_bytes == service.content
    assert observation.media_type == "application/pdf"
    assert observation.external_identity.assurance is ExternalIdentityAssurance.PATH_SCOPED
    assert observation.external_identity.provider_key.provider_object_id == "disk:/approved/brief.pdf"
    assert observation.discovery is ProviderObservationDiscovery.SNAPSHOT
    assert observation.provider_version_before == "1700000000000000"
    assert observation.provider_version_after == "1700000000000000"
    evidence = observation.representation.revision_evidence
    assert evidence is not None
    assert evidence.provider_revision == "1700000000000000"
    assert evidence.provider_resource_id == "1573541:abc123"
    assert evidence.resource_name == "brief.pdf"
    assert evidence.resource_path == "disk:/approved/brief.pdf"
    assert evidence.content_sha256 == hashlib.sha256(service.content).hexdigest()
    assert evidence.byte_length == len(service.content)
    assert [name for name, _ in service.calls] == ["list", "list", "download", "metadata"]
    assert service.calls[0][1][:2] == (
        _connection().credential_reference,
        _connection().grant_reference,
    )


@pytest.mark.asyncio
async def test_path_scoped_reappearance_occurrence_never_claims_identity_continuity() -> None:
    adapter = YandexDiskProviderAdapter(
        service=OfficialShapeService(),
        profile=YandexDiskReadProfile(page_size=2, max_attempts=1),
        clock=lambda: NOW,
        sleep=lambda _: _return_none(),
    )

    original = await adapter.read(_connection(), _command())
    reappeared = await adapter.read(
        _connection(),
        replace(_command(), external_identity_occurrence="scan-occurrence-2"),
    )

    assert original.external_identity.assurance is ExternalIdentityAssurance.PATH_SCOPED
    assert reappeared.external_identity.assurance is ExternalIdentityAssurance.PATH_SCOPED
    assert original.external_identity.provider_key == reappeared.external_identity.provider_key
    assert original.external_identity.external_object_id != reappeared.external_identity.external_object_id


@pytest.mark.asyncio
async def test_wrong_scope_and_workspace_are_denied_before_provider_content_read() -> None:
    service = OfficialShapeService()
    provider = YandexDiskProviderAdapter(
        service=service,
        profile=YandexDiskReadProfile(page_size=2, max_attempts=1),
        clock=lambda: NOW,
        sleep=lambda _: _return_none(),
    )
    admission = ProviderReadAdmission(
        repository=Repository(_connection()),
        provider=provider,
    )

    for command in (
        _command(scope=OpaqueProviderReference(value=_id(99))),
        _command(workspace_id=_id(44)),
    ):
        with pytest.raises(ProviderReadDeniedError):
            await admission.read(command, object())

    assert service.calls == []

    revoked = ProviderReadAdmission(
        repository=Repository(
            _connection(status=ProviderConnectionStatus.REVOKED)
        ),
        provider=provider,
    )
    with pytest.raises(ProviderReadDeniedError):
        await revoked.read(_command(), object())
    assert service.calls == []


@pytest.mark.asyncio
async def test_two_workspaces_keep_equivalent_files_in_distinct_source_identities() -> None:
    async def read(workspace_id: UUID, connection_id: UUID):
        return await YandexDiskProviderAdapter(
            service=OfficialShapeService(),
            profile=YandexDiskReadProfile(page_size=2),
            clock=lambda: NOW,
            sleep=lambda _: _return_none(),
        ).read(
            _connection(
                workspace_id=workspace_id,
                connection_id=connection_id,
            ),
            _command(
                workspace_id=workspace_id,
                connection_id=connection_id,
            ),
        )

    first = await read(_id(4), _id(2))
    second = await read(_id(44), _id(22))

    assert first.workspace_id != second.workspace_id
    assert first.external_identity.external_object_id != (
        second.external_identity.external_object_id
    )
    assert first.original_bytes == second.original_bytes


def test_capabilities_do_not_emulate_stable_identity_or_incremental_changes() -> None:
    capabilities = {
        item.kind: item
        for item in yandex_disk_capabilities(
            qualification_reference=OpaqueProviderReference(value=_id(10))
        )
    }

    assert set(capabilities) == set(ProviderCapabilityKind)
    assert capabilities[ProviderCapabilityKind.BINARY_READ].status is ProviderCapabilityStatus.SUPPORTED
    assert capabilities[ProviderCapabilityKind.STABLE_IDENTITY].status is ProviderCapabilityStatus.UNSUPPORTED
    assert capabilities[ProviderCapabilityKind.INCREMENTAL_CHANGE_DISCOVERY].status is ProviderCapabilityStatus.UNSUPPORTED
    assert capabilities[ProviderCapabilityKind.FULL_RECONCILIATION].status is ProviderCapabilityStatus.SUPPORTED
    assert capabilities[ProviderCapabilityKind.LIFECYCLE_EVIDENCE].status is ProviderCapabilityStatus.UNSUPPORTED
    assert capabilities[ProviderCapabilityKind.HEALTH_DIAGNOSTICS].status is ProviderCapabilityStatus.UNQUALIFIED


@pytest.mark.asyncio
async def test_full_scan_uses_opaque_durable_checkpoints_and_complete_official_pages() -> None:
    service = OfficialShapeService()
    adapter = YandexDiskProviderAdapter(
        service=service,
        profile=YandexDiskReadProfile(page_size=2, max_attempts=1),
        clock=lambda: NOW,
        sleep=lambda _: _return_none(),
    )

    first = await adapter.scan_page(
        _connection(),
        _command().resource_scope_reference,
        checkpoint=None,
    )
    second = await adapter.scan_page(
        _connection(),
        _command().resource_scope_reference,
        checkpoint=first.next_checkpoint,
    )

    assert [item.provider_object_reference for item in first.items] == [
        "disk:/approved/brief.pdf",
        "disk:/approved/second.pdf",
    ]
    assert [item.provider_object_reference for item in second.items] == [
        "disk:/approved/other.txt"
    ]
    assert first.requested_checkpoint is None
    assert first.next_checkpoint is not None
    assert first.checkpoint == first.next_checkpoint
    assert "disk:/" not in first.next_checkpoint
    assert first.next_checkpoint != "2"
    assert second.requested_checkpoint == first.next_checkpoint
    assert second.next_checkpoint is None
    assert second.checkpoint != first.checkpoint
    assert first.expected_entry_count == second.expected_entry_count == 3
    assert len(first.coverage_entry_digests + second.coverage_entry_digests) == 3
    assert second.confirmed_scan_generation is not None


@pytest.mark.asyncio
async def test_full_scan_rejects_same_count_unique_substitution_without_stable_generation() -> None:
    class ChangingInventory(OfficialShapeService):
        def __init__(self) -> None:
            super().__init__()
            self.list_calls = 0

        async def list_resources(self, connection, scope, *, offset, limit):
            del connection, scope
            self.list_calls += 1
            if offset == 0:
                paths = ("disk:/approved/a.pdf", "disk:/approved/b.pdf")
            elif self.list_calls <= 2:
                paths = ("disk:/approved/c.pdf",)
            else:
                paths = ("disk:/approved/d.pdf",)
            return YandexDiskResourcePage(
                _embedded={
                    "path": "disk:/approved",
                    "items": [_resource(path=path) for path in paths],
                    "limit": limit,
                    "offset": offset,
                    "total": 3,
                }
            )

    adapter = YandexDiskProviderAdapter(
        service=ChangingInventory(),
        profile=YandexDiskReadProfile(page_size=2, max_attempts=1),
        clock=lambda: NOW,
        sleep=lambda _: _return_none(),
    )
    first = await adapter.scan_page(
        _connection(), _command().resource_scope_reference, checkpoint=None
    )

    with pytest.raises(YandexDiskContractError):
        await adapter.scan_page(
            _connection(),
            _command().resource_scope_reference,
            checkpoint=first.next_checkpoint,
        )


@pytest.mark.asyncio
async def test_throttled_listing_retries_without_duplicate_observation() -> None:
    service = OfficialShapeService()
    service.throttle_once = True
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    adapter = YandexDiskProviderAdapter(
        service=service,
        profile=YandexDiskReadProfile(page_size=2, max_attempts=2),
        clock=lambda: NOW,
        sleep=sleep,
        jitter=lambda _: 0.0,
    )

    first = await adapter.read(_connection(), _command())
    replay = await adapter.read(_connection(), _command())

    assert first == replay
    assert delays == [0.25]


@pytest.mark.asyncio
async def test_listing_limits_bound_provider_controlled_pagination() -> None:
    class OversizedListing(OfficialShapeService):
        async def list_resources(self, connection, scope, *, offset, limit):
            page = await super().list_resources(
                connection, scope, offset=offset, limit=limit
            )
            return page.model_copy(
                update={
                    "embedded": page.embedded.model_copy(update={"total": 101})
                }
            )

    service = OversizedListing()
    adapter = YandexDiskProviderAdapter(
        service=service,
        profile=YandexDiskReadProfile(
            page_size=2,
            max_pages=2,
            max_items=100,
        ),
        clock=lambda: NOW,
        sleep=lambda _: _return_none(),
    )

    with pytest.raises(YandexDiskContractError):
        await adapter.read(_connection(), _command())

    assert [name for name, _ in service.calls] == ["list"]


@pytest.mark.asyncio
async def test_transient_outage_uses_bounded_backoff_and_jitter() -> None:
    service = OfficialShapeService()
    original = service.list_resources
    attempts = 0

    async def transient_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise YandexDiskTransientError()
        return await original(*args, **kwargs)

    service.list_resources = transient_once
    delays: list[float] = []
    adapter = YandexDiskProviderAdapter(
        service=service,
        profile=YandexDiskReadProfile(
            page_size=2,
            max_attempts=2,
            backoff_seconds=(0.5,),
        ),
        clock=lambda: NOW,
        sleep=lambda delay: _record_delay(delays, delay),
        jitter=lambda backoff: backoff / 2,
    )

    await adapter.read(_connection(), _command())

    assert delays == [0.75]


@pytest.mark.asyncio
async def test_partial_download_and_concurrent_revision_never_emit_observation() -> None:
    partial = OfficialShapeService()
    partial.content = b"truncated"
    adapter = YandexDiskProviderAdapter(
        service=partial,
        profile=YandexDiskReadProfile(page_size=2, max_attempts=1),
        clock=lambda: NOW,
        sleep=lambda _: _return_none(),
    )
    with pytest.raises(YandexDiskPartialDownloadError):
        await adapter.read(_connection(), _command())

    wrong_media = OfficialShapeService()

    async def mismatched_download(*args, **kwargs):
        del args, kwargs
        return YandexDiskFileContent(
            content=wrong_media.content,
            media_type="application/octet-stream",
        )

    wrong_media.download = mismatched_download
    with pytest.raises(YandexDiskPartialDownloadError):
        await YandexDiskProviderAdapter(
            service=wrong_media,
            profile=YandexDiskReadProfile(page_size=2, max_attempts=1),
            clock=lambda: NOW,
            sleep=lambda _: _return_none(),
        ).read(_connection(), _command())

    changed = OfficialShapeService()
    changed.after = _resource(revision="1700000000000001")
    changed_adapter = YandexDiskProviderAdapter(
        service=changed,
        profile=YandexDiskReadProfile(page_size=2, max_attempts=1),
        clock=lambda: NOW,
        sleep=lambda _: _return_none(),
    )
    with pytest.raises(YandexDiskConcurrentModificationError):
        await changed_adapter.read(_connection(), _command())

    replaced = OfficialShapeService()
    replaced.after = _resource().model_copy(
        update={"resource_id": "1573541:replacement"}
    )
    with pytest.raises(YandexDiskConcurrentModificationError):
        await YandexDiskProviderAdapter(
            service=replaced,
            profile=YandexDiskReadProfile(page_size=2, max_attempts=1, backoff_seconds=()),
            clock=lambda: NOW,
            sleep=lambda _: _return_none(),
        ).read(_connection(), _command())


@pytest.mark.asyncio
async def test_size_limit_and_provider_revocation_fail_before_observation() -> None:
    too_large = OfficialShapeService()
    adapter = YandexDiskProviderAdapter(
        service=too_large,
        profile=YandexDiskReadProfile(
            page_size=2,
            max_attempts=1,
            max_file_bytes=8,
            backoff_seconds=(),
        ),
        clock=lambda: NOW,
        sleep=lambda _: _return_none(),
    )
    with pytest.raises(YandexDiskFileTooLargeError):
        await adapter.read(_connection(), _command())
    assert all(name != "download" for name, _ in too_large.calls)

    revoked = OfficialShapeService()

    async def rejected_download(*args, **kwargs):
        del args, kwargs
        raise YandexDiskAuthorizationRevokedError()

    revoked.download = rejected_download
    with pytest.raises(YandexDiskAuthorizationRevokedError):
        await YandexDiskProviderAdapter(
            service=revoked,
            profile=YandexDiskReadProfile(
                page_size=2, max_attempts=1, backoff_seconds=()
            ),
            clock=lambda: NOW,
            sleep=lambda _: _return_none(),
        ).read(_connection(), _command())
    assert [name for name, _ in revoked.calls] == ["list", "list"]


async def _return_none() -> None:
    return None


async def _record_delay(values: list[float], delay: float) -> None:
    values.append(delay)
