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
from spine.connectors.bitrix24_disk import (
    Bitrix24DiskAuthorizationRevokedError,
    Bitrix24DiskConcurrentModificationError,
    Bitrix24DiskDownloadLinkExpiredError,
    Bitrix24DiskFile,
    Bitrix24DiskFileContent,
    Bitrix24DiskFileTooLargeError,
    Bitrix24DiskOverloadError,
    Bitrix24DiskPartialDownloadError,
    Bitrix24DiskPermissionLostError,
    Bitrix24DiskProviderAdapter,
    Bitrix24DiskReadProfile,
    Bitrix24DiskResourceNotFoundError,
    Bitrix24DiskThrottleError,
    Bitrix24DiskTransientError,
    Bitrix24DiskContractError,
    Bitrix24DiskFilePage,
    bitrix24_disk_capabilities,
    BITRIX24_DISK_REQUIRED_CAPABILITIES,
)
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapabilityKind,
    ProviderCapabilityStatus,
    ProviderConnection,
    ProviderConnectionStatus,
    ProviderKind,
)
from spine.domain.connections.bitrix24_disk_scope import (
    Bitrix24DiskMembershipEvidence,
    Bitrix24DiskStorageClass,
)


NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{value:012d}")


SCOPE = OpaqueProviderReference(value=_id(9))
CONTENT = b"governed bitrix24 disk source"


def _file(
    *,
    file_id: str = "9043",
    version: str = "42",
    name: str = "brief.pdf",
    size: int = len(CONTENT),
    download_url: str | None = "https://example.bitrix24.test/rest/download.json?auth=SIGNED-SECRET",
) -> Bitrix24DiskFile:
    return Bitrix24DiskFile(
        ID=file_id,
        NAME=name,
        TYPE="file",
        STORAGE_ID="1357",
        PARENT_ID="8907",
        GLOBAL_CONTENT_VERSION=version,
        FILE_ID="32718",
        SIZE=size,
        CREATE_TIME=NOW,
        UPDATE_TIME=NOW,
        DOWNLOAD_URL=download_url,
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
        provider_kind=ProviderKind.BITRIX24,
        credential_reference=OpaqueProviderReference(value=_id(7)),
        grant_reference=OpaqueProviderReference(value=_id(8)),
        approved_resource_scope_references=(SCOPE,),
        authorization_generation=2,
        configuration_generation=3,
        status=status,
        capabilities=bitrix24_disk_capabilities(
            qualification_reference=OpaqueProviderReference(value=_id(10))
        ),
    )


def _command(
    *,
    workspace_id: UUID = _id(4),
    connection_id: UUID = _id(2),
    scope: OpaqueProviderReference = SCOPE,
    target: str | None = "9043",
    acting_subject_id: UUID | None = _id(77),
    required_capabilities: tuple[ProviderCapabilityKind, ...] = BITRIX24_DISK_REQUIRED_CAPABILITIES,
) -> ProviderReadCommand:
    return ProviderReadCommand(
        workspace_id=workspace_id,
        environment_id=_id(5),
        connection_id=connection_id,
        connection_version_id=_id(1),
        resource_scope_reference=scope,
        authorization_generation=2,
        configuration_generation=3,
        required_capabilities=required_capabilities,
        resource_object_reference=target,
        acting_subject_id=acting_subject_id,
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
        self.file = _file()
        self.download_attempts = 0
        self.throttle_once = False

    async def authorize_read(self, connection, scope, *, file_id, acting_principal_id):
        self.calls.append(("authorize", (connection.service_principal_id, scope, file_id, acting_principal_id)))

    async def list_children(self, connection, scope, *, start, page_size, acting_principal_id):
        self.calls.append(("list", (connection.credential_reference, scope, start, page_size, acting_principal_id)))
        if self.throttle_once:
            self.throttle_once = False
            raise Bitrix24DiskThrottleError(retry_after_seconds=0.25)
        return Bitrix24DiskFilePage(result=[self.file], total=1)

    async def get_file(self, connection, scope, *, file_id, acting_principal_id):
        self.calls.append(("get", (connection.grant_reference, scope, file_id, acting_principal_id)))
        return self.file

    async def download(self, connection, scope, *, file_id, download_url, max_bytes, acting_principal_id):
        self.calls.append(("download", (scope, file_id, download_url, max_bytes, acting_principal_id)))
        self.download_attempts += 1
        return Bitrix24DiskFileContent(content=CONTENT, media_type="application/pdf")


def _adapter(service: OfficialShapeService, **profile):
    return Bitrix24DiskProviderAdapter(
        service=service,
        profile=Bitrix24DiskReadProfile(**profile),
        clock=lambda: NOW,
        sleep=lambda _: _async_none(),
    )


def test_revision_evidence_must_match_exact_original_bytes() -> None:
    evidence = ProviderFileRevisionEvidence(
        provider_revision="42",
        resource_name="brief.pdf",
        resource_path=None,
        byte_length=len(b"other"),
        content_sha256=hashlib.sha256(b"other").hexdigest(),
        modified_at=NOW,
    )

    with pytest.raises(ValueError, match="byte length"):
        BinaryObservationRepresentation(
            original_bytes=CONTENT,
            media_type="application/pdf",
            extraction_profile="bitrix24-disk-binary:v1",
            locator_contract_version="locator:bitrix24-disk-object:v1",
            revision_evidence=evidence,
        )


@pytest.mark.asyncio
async def test_reader_preserves_disk_object_version_exact_bytes_and_omits_signed_url() -> None:
    service = OfficialShapeService()
    observation = await _adapter(service).read(_connection(), _command())

    assert observation.provider_kind is ProviderKind.BITRIX24
    assert observation.original_bytes == CONTENT
    assert observation.external_identity.assurance is ExternalIdentityAssurance.PROVIDER_STABLE
    assert observation.external_identity.provider_key.provider_object_id == "9043"
    assert observation.discovery is ProviderObservationDiscovery.SNAPSHOT
    assert observation.provider_version_before == "42"
    evidence = observation.representation.revision_evidence
    assert evidence is not None
    assert evidence.provider_revision == "42"
    assert evidence.provider_resource_id == "9043"
    assert evidence.resource_name == "brief.pdf"
    assert evidence.byte_length == len(CONTENT)
    assert hashlib.sha256(CONTENT).hexdigest() == evidence.content_sha256
    assert evidence.provider_payload_digest is not None
    assert evidence.provider_payload is not None
    assert evidence.provider_payload["ID"] == "9043"
    assert "DOWNLOAD_URL" not in evidence.provider_payload
    assert "SIGNED-SECRET" not in observation.model_dump_json()


@pytest.mark.asyncio
async def test_reader_carries_exact_scope_policy_and_actor_lineage() -> None:
    service = OfficialShapeService()
    base = _command(acting_subject_id=_id(77))
    membership = Bitrix24DiskMembershipEvidence(
        workspace_id=base.workspace_id,
        environment_id=base.environment_id,
        connection_id=base.connection_id,
        connection_version_id=base.connection_version_id,
        resource_scope_reference=base.resource_scope_reference,
        root_policy_id=_id(31),
        root_policy_version="root-v4",
        root_policy_digest="a" * 64,
        content_policy_id=_id(32),
        content_policy_version="content-v2",
        content_policy_digest="b" * 64,
        authorization_generation=base.authorization_generation,
        configuration_generation=base.configuration_generation,
        grant_binding_id=_id(33),
        grant_token_epoch=4,
        grant_security_fence=0,
        storage_id="1357",
        storage_class=Bitrix24DiskStorageClass.COMMON,
        storage_root_object_id="1000",
        approved_root_folder_id="8907",
        object_id="9043",
        parent_folder_ids=("8907",),
        provider_revision="42",
    )
    command = replace(base, membership_evidence=membership)
    observation = await _adapter(service).read(_connection(), command)

    lineage = observation.scope_membership
    assert lineage is not None
    assert lineage.root_policy_version == "root-v4"
    assert lineage.content_policy_digest == "b" * 64
    assert lineage.parent_folder_ids == ("8907",)
    assert lineage.grant_token_epoch == 4


@pytest.mark.asyncio
async def test_scope_workspace_and_revocation_are_denied_before_provider_read() -> None:
    service = OfficialShapeService()
    admission = ProviderReadAdmission(
        repository=Repository(_connection()),
        provider=_adapter(service),
    )

    for command in (
        _command(scope=OpaqueProviderReference(value=_id(99))),
        _command(workspace_id=_id(44)),
    ):
        with pytest.raises(ProviderReadDeniedError):
            await admission.read(command, object())

    with pytest.raises(ProviderReadDeniedError):
        await ProviderReadAdmission(
            repository=Repository(
                _connection(status=ProviderConnectionStatus.REVOKED)
            ),
            provider=_adapter(service),
        ).read(_command(), object())

    assert service.calls == []


@pytest.mark.asyncio
async def test_reader_requires_qualified_disk_capabilities_and_forwards_acting_user() -> None:
    service = OfficialShapeService()
    acting_user = _id(77)
    observation = await _adapter(service).read(
        _connection(), _command(acting_subject_id=acting_user)
    )

    assert observation.original_bytes == CONTENT
    assert all(acting_user in args for _, args in service.calls)

    with pytest.raises(Bitrix24DiskContractError):
        await _adapter(service).read(
            _connection(),
            _command(required_capabilities=BITRIX24_DISK_REQUIRED_CAPABILITIES[:-1]),
        )

    with pytest.raises(Bitrix24DiskContractError):
        await _adapter(service).read(
            _connection(), _command(acting_subject_id=None)
        )
    with pytest.raises(Bitrix24DiskContractError):
        await _adapter(service).read(
            _connection(), _command(acting_subject_id=_id(6))
        )

    service.calls.clear()

    async def deny(connection, scope, *, file_id, acting_principal_id):
        del connection, scope, file_id, acting_principal_id
        raise Bitrix24DiskPermissionLostError()

    service.authorize_read = deny
    with pytest.raises(Bitrix24DiskPermissionLostError):
        await _adapter(service, max_attempts=1).read(_connection(), _command())
    assert all(name == "authorize" for name, _ in service.calls)


@pytest.mark.asyncio
async def test_two_workspaces_keep_same_file_in_distinct_source_identities() -> None:
    async def read(workspace_id: UUID, connection_id: UUID):
        return await _adapter(OfficialShapeService()).read(
            _connection(workspace_id=workspace_id, connection_id=connection_id),
            _command(workspace_id=workspace_id, connection_id=connection_id),
        )

    first = await read(_id(4), _id(2))
    second = await read(_id(44), _id(22))

    assert first.external_identity.external_object_id != second.external_identity.external_object_id
    assert first.original_bytes == second.original_bytes


def test_capabilities_match_the_qualified_disk_slice() -> None:
    capabilities = {
        item.kind: item
        for item in bitrix24_disk_capabilities(
            qualification_reference=OpaqueProviderReference(value=_id(10))
        )
    }

    assert set(capabilities) == set(ProviderCapabilityKind)
    for capability in (
        ProviderCapabilityKind.ENUMERATION,
        ProviderCapabilityKind.METADATA_READ,
        ProviderCapabilityKind.BINARY_READ,
        ProviderCapabilityKind.STABLE_IDENTITY,
    ):
        assert capabilities[capability].status is ProviderCapabilityStatus.SUPPORTED
    for capability in (
        ProviderCapabilityKind.TABULAR_READ,
        ProviderCapabilityKind.STRUCTURED_READ,
        ProviderCapabilityKind.INCREMENTAL_CHANGE_DISCOVERY,
        ProviderCapabilityKind.FULL_RECONCILIATION,
        ProviderCapabilityKind.LIFECYCLE_EVIDENCE,
    ):
        assert capabilities[capability].status is ProviderCapabilityStatus.UNSUPPORTED


@pytest.mark.asyncio
async def test_pagination_duplicate_entries_and_throttle_are_bounded_and_replayable() -> None:
    service = OfficialShapeService()
    service.file = _file(file_id="9043")
    service.throttle_once = True

    async def list_children(connection, scope, *, start, page_size, acting_principal_id):
        del acting_principal_id
        service.calls.append(("list", (scope, start, page_size)))
        if service.throttle_once:
            service.throttle_once = False
            raise Bitrix24DiskThrottleError(retry_after_seconds=0.25)
        if start == 0:
            return Bitrix24DiskFilePage(
                result=[service.file, service.file], total=51
            )
        return Bitrix24DiskFilePage(
            result=[service.file.model_copy(update={"ID": "9044"})], total=51
        )

    service.list_children = list_children
    adapter = _adapter(service, page_size=50, max_attempts=2)
    first = await adapter.read(_connection(), _command())
    replay = await adapter.read(_connection(), _command())

    assert first == replay
    assert [name for name, _ in service.calls].count("list") == 5
    assert [args[1] for name, args in service.calls if name == "list"] == [0, 0, 50, 0, 50]


@pytest.mark.asyncio
async def test_expired_download_link_is_refreshed_without_entering_observation() -> None:
    service = OfficialShapeService()
    old = _file(download_url="https://example.test/download?auth=OLD")
    new = _file(download_url="https://example.test/download?auth=NEW")
    get_count = 0

    async def get_file(connection, scope, *, file_id, acting_principal_id):
        nonlocal get_count
        del connection, scope, file_id, acting_principal_id
        get_count += 1
        return old if get_count == 1 else new

    async def download(connection, scope, *, file_id, download_url, max_bytes, acting_principal_id):
        del connection, scope, file_id, max_bytes, acting_principal_id
        if download_url.endswith("OLD"):
            raise Bitrix24DiskDownloadLinkExpiredError()
        return Bitrix24DiskFileContent(content=CONTENT, media_type="application/pdf")

    service.get_file = get_file
    service.download = download
    observation = await _adapter(service).read(_connection(), _command())

    assert observation.original_bytes == CONTENT
    assert "auth=OLD" not in observation.model_dump_json()
    assert "auth=NEW" not in observation.model_dump_json()
    assert get_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    (
        Bitrix24DiskOverloadError(),
        Bitrix24DiskTransientError(),
        Bitrix24DiskAuthorizationRevokedError(),
        Bitrix24DiskPermissionLostError(),
        Bitrix24DiskResourceNotFoundError(),
    ),
)
async def test_typed_provider_failures_never_leak_provider_payload(error) -> None:
    service = OfficialShapeService()

    async def list_children(*args, **kwargs):
        del args, kwargs
        raise error

    service.list_children = list_children
    with pytest.raises(type(error)) as caught:
        await _adapter(service, max_attempts=1).read(_connection(), _command())

    assert "SIGNED-SECRET" not in str(caught.value)


@pytest.mark.asyncio
async def test_partial_download_and_concurrent_revision_never_emit_observation() -> None:
    partial = OfficialShapeService()

    async def bad_download(*args, **kwargs):
        del args, kwargs
        return Bitrix24DiskFileContent(content=b"short", media_type="application/pdf")

    partial.download = bad_download
    with pytest.raises(Bitrix24DiskPartialDownloadError):
        await _adapter(partial, max_attempts=1).read(_connection(), _command())

    changed = OfficialShapeService()
    changed.file = _file(version="43")
    with pytest.raises(Bitrix24DiskConcurrentModificationError):
        initial = _file(version="42")
        changed.file = initial
        get_count = 0

        async def change_after_first_get(
            connection, scope, *, file_id, acting_principal_id
        ):
            nonlocal get_count
            del connection, scope, file_id, acting_principal_id
            get_count += 1
            if get_count == 1:
                return initial
            return _file(version="43")

        changed.get_file = change_after_first_get
        await _adapter(changed, max_attempts=1).read(_connection(), _command())

    too_large = OfficialShapeService()
    too_large.file = _file(size=8 * 1024 * 1024)
    with pytest.raises(Bitrix24DiskFileTooLargeError):
        await _adapter(too_large, max_attempts=1, max_file_bytes=1024).read(
            _connection(), _command()
        )


@pytest.mark.asyncio
async def test_malformed_pagination_is_rejected_before_download() -> None:
    service = OfficialShapeService()

    async def list_children(
        connection, scope, *, start, page_size, acting_principal_id
    ):
        del connection, scope, start, page_size, acting_principal_id
        return Bitrix24DiskFilePage(result=[service.file] * 51, total=51)

    service.list_children = list_children
    with pytest.raises(Bitrix24DiskContractError):
        await _adapter(service, page_size=50, max_attempts=1).read(
            _connection(), _command()
        )

    assert all(name != "download" for name, _ in service.calls)


async def _async_none() -> None:
    return None
