from uuid import UUID

import pytest

from spine.application.bitrix24_disk_scope_admission import (
    Bitrix24DiskScopeAdmissionService,
    Bitrix24DiskScopeDenied,
)
from spine.domain.connections.bitrix24_disk_scope import (
    Bitrix24DiskContentPolicy,
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


def ident(value: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{value:012d}")


def content_policy(**updates: object) -> Bitrix24DiskContentPolicy:
    values: dict[str, object] = {
        "policy_id": ident(11),
        "version": "v2",
        "workspace_id": ident(4),
        "environment_id": ident(5),
        "connection_id": ident(2),
        "connection_version_id": ident(1),
        "resource_scope_reference": OpaqueProviderReference(value=ident(9)),
        "allowed_formats": ("pdf",),
    }
    values.update(updates)
    return Bitrix24DiskContentPolicy(**values)


def root_policy(content: Bitrix24DiskContentPolicy, **updates: object) -> Bitrix24DiskRootPolicy:
    values: dict[str, object] = {
        "policy_id": ident(10),
        "version": "v3",
        "workspace_id": ident(4),
        "environment_id": ident(5),
        "connection_id": ident(2),
        "connection_version_id": ident(1),
        "resource_scope_reference": OpaqueProviderReference(value=ident(9)),
        "storage_id": "storage-1",
        "storage_class": Bitrix24DiskStorageClass.COMMON,
        "storage_root_object_id": "root-1",
        "approved_root_folder_id": "approved-1",
        "recurse": False,
        "discovery_mode": Bitrix24DiskDiscoveryMode.FIXED_MANIFEST,
        "manifest_object_ids": ("file-1",),
        "content_policy_id": content.policy_id,
        "content_policy_version": content.version,
        "content_policy_digest": content.digest,
        "traversal_profile_reference": OpaqueProviderReference(value=ident(12)),
    }
    values.update(updates)
    return Bitrix24DiskRootPolicy(**values)


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


class PolicyFake:
    def __init__(self, root: Bitrix24DiskRootPolicy, content: Bitrix24DiskContentPolicy) -> None:
        self.root = root
        self.content = content

    async def get_current_root_policy(self, *, workspace_id: UUID, environment_id: UUID, policy_id: UUID):
        if (workspace_id, environment_id, policy_id) != (self.root.workspace_id, self.root.environment_id, self.root.policy_id):
            return None
        return self.root

    async def get_current_content_policy(self, *, workspace_id: UUID, environment_id: UUID, policy_id: UUID):
        if (workspace_id, environment_id, policy_id) != (self.content.workspace_id, self.content.environment_id, self.content.policy_id):
            return None
        return self.content


@pytest.mark.asyncio
async def test_admission_resolves_exact_current_tenant_scoped_policy_versions() -> None:
    content = content_policy()
    root = root_policy(content)
    result = await Bitrix24DiskScopeAdmissionService(PolicyFake(root, content)).resolve_current(
        connection(), root_policy_id=root.policy_id,
        root_policy_version=root.version, root_policy_digest=root.digest,
    )
    assert result.root_policy is root
    assert result.content_policy is content


@pytest.mark.asyncio
async def test_admission_fences_superseded_root_policy() -> None:
    content = content_policy()
    root = root_policy(content, version="v4")
    with pytest.raises(Bitrix24DiskScopeDenied, match="root_policy_stale_or_mismatched"):
        await Bitrix24DiskScopeAdmissionService(PolicyFake(root, content)).resolve_current(
            connection(), root_policy_id=root.policy_id,
            root_policy_version="v3", root_policy_digest="b" * 64,
        )


@pytest.mark.asyncio
async def test_admission_denies_empty_content_allowlist() -> None:
    content = content_policy(allowed_formats=())
    root = root_policy(content)
    with pytest.raises(Bitrix24DiskScopeDenied, match="content_policy_stale_or_mismatched"):
        await Bitrix24DiskScopeAdmissionService(PolicyFake(root, content)).resolve_current(
            connection(), root_policy_id=root.policy_id,
            root_policy_version=root.version, root_policy_digest=root.digest,
        )
