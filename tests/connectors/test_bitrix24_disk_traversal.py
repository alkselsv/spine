from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.connectors.bitrix24_disk import Bitrix24DiskFile, Bitrix24DiskFilePage
from spine.connectors.bitrix24_disk_traversal import (
    Bitrix24DiskRootTraversal,
    Bitrix24DiskTraversalProfile,
)
from spine.domain.connections.bitrix24_disk_scope import (
    Bitrix24DiskDiscoveryMode,
    Bitrix24DiskRootPolicy,
    Bitrix24DiskStorageClass,
)
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderConnection,
    ProviderConnectionStatus,
    ProviderKind,
)

NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)


def ident(value: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{value:012d}")


def file_object(
    object_id: str,
    parent_id: str,
    *,
    kind: str = "folder",
    storage_id: str = "storage-1",
) -> Bitrix24DiskFile:
    fields: dict[str, object] = {
        "ID": object_id,
        "NAME": "synthetic",
        "TYPE": kind,
        "STORAGE_ID": storage_id,
        "PARENT_ID": parent_id,
        "CREATE_TIME": NOW,
        "UPDATE_TIME": NOW,
    }
    if kind == "file":
        fields.update({"GLOBAL_CONTENT_VERSION": "rev-1", "SIZE": 4})
    return Bitrix24DiskFile(**fields)


def connection() -> ProviderConnection:
    return ProviderConnection(
        connection_version_id=ident(1),
        connection_id=ident(2),
        approval_id=ident(3),
        version=1,
        workspace_id=ident(4),
        environment_id=ident(5),
        service_principal_id=ident(6),
        provider_kind=ProviderKind.BITRIX24,
        credential_reference=OpaqueProviderReference(value=ident(7)),
        grant_reference=OpaqueProviderReference(value=ident(8)),
        approved_resource_scope_references=(OpaqueProviderReference(value=ident(9)),),
        authorization_generation=1,
        configuration_generation=1,
        status=ProviderConnectionStatus.ACTIVE,
        capabilities=(),
    )


def policy(**updates: object) -> Bitrix24DiskRootPolicy:
    values: dict[str, object] = {
        "policy_id": ident(10),
        "version": "v1",
        "workspace_id": ident(4),
        "environment_id": ident(5),
        "connection_id": ident(2),
        "connection_version_id": ident(1),
        "resource_scope_reference": OpaqueProviderReference(value=ident(9)),
        "storage_id": "storage-1",
        "storage_class": Bitrix24DiskStorageClass.COMMON,
        "storage_root_object_id": "storage-root",
        "approved_root_folder_id": "approved-root",
        "recurse": False,
        "discovery_mode": Bitrix24DiskDiscoveryMode.FIXED_MANIFEST,
        "manifest_object_ids": ("file-1",),
        "content_policy_id": ident(11),
        "content_policy_version": "v1",
        "content_policy_digest": "a" * 64,
        "traversal_profile_reference": OpaqueProviderReference(value=ident(12)),
    }
    values.update(updates)
    return Bitrix24DiskRootPolicy(**values)


class FakeDisk:
    def __init__(self, pages: dict[str, tuple[Bitrix24DiskFilePage, ...]]) -> None:
        self.pages = pages
        self.folders = {
            "approved-root": file_object("approved-root", "storage-root"),
            "storage-root": file_object("storage-root", "storage-root"),
        }
        self.calls: list[tuple[str, int]] = []

    async def list_children(self, connection: ProviderConnection, scope: OpaqueProviderReference, *, folder_id: str, start: int, page_size: int, acting_principal_id: UUID) -> Bitrix24DiskFilePage:
        self.calls.append((folder_id, start))
        return self.pages[folder_id][start // page_size]

    async def get_folder(self, connection: ProviderConnection, scope: OpaqueProviderReference, *, folder_id: str, acting_principal_id: UUID) -> Bitrix24DiskFile:
        return self.folders[folder_id]


@pytest.mark.asyncio
async def test_scan_returns_exact_membership_for_positive_member_even_when_other_pages_are_unavailable() -> None:
    item = file_object("file-1", "approved-root", kind="file")
    service = FakeDisk({"approved-root": (Bitrix24DiskFilePage(result=(item,), total=1),)})
    result = await Bitrix24DiskRootTraversal(
        service=service,
        profile=Bitrix24DiskTraversalProfile(),
    ).scan(
        connection(), policy(), acting_principal_id=ident(20), authorize_folder=allow,
    )
    assert result.complete is True
    assert len(result.memberships) == 1
    proof = result.memberships[0]
    assert proof.object_id == "file-1"
    assert proof.parent_folder_ids == ("approved-root",)
    assert proof.storage_root_object_id != proof.approved_root_folder_id


@pytest.mark.asyncio
async def test_incomplete_listing_does_not_prove_non_membership() -> None:
    item = file_object("file-1", "approved-root", kind="file")
    service = FakeDisk({
        "approved-root": (
            Bitrix24DiskFilePage(result=(item,), total=2),
            Bitrix24DiskFilePage(result=(), total=3),
        )
    })
    result = await Bitrix24DiskRootTraversal(
        service=service,
        profile=Bitrix24DiskTraversalProfile(page_size=1),
    ).scan(
        connection(), policy(), acting_principal_id=ident(20), authorize_folder=allow,
    )
    assert result.complete is False
    assert result.reason_code == "page_drift"


@pytest.mark.asyncio
async def test_service_principal_cannot_be_used_as_acting_user() -> None:
    service = FakeDisk({})
    result = await Bitrix24DiskRootTraversal(
        service=service,
        profile=Bitrix24DiskTraversalProfile(),
    ).scan(
        connection(), policy(), acting_principal_id=ident(6), authorize_folder=allow,
    )
    assert result.complete is False
    assert result.reason_code == "acting_user_authority_required"
    assert service.calls == []


async def allow(folder_id: str) -> bool:
    del folder_id
    return True
