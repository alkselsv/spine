from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.connections.bitrix24_disk_scope import (
    Bitrix24DiskContentPolicy,
    Bitrix24DiskDiscoveryMode,
    Bitrix24DiskMembershipEvidence,
    Bitrix24DiskRootPolicy,
    Bitrix24DiskStorageClass,
)
from spine.domain.connections.provider import OpaqueProviderReference


def ident(value: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{value:012d}")


def content_policy(**updates: object) -> Bitrix24DiskContentPolicy:
    values: dict[str, object] = {
        "policy_id": ident(1),
        "version": "v1",
        "workspace_id": ident(2),
        "environment_id": ident(3),
        "connection_id": ident(4),
        "connection_version_id": ident(5),
        "resource_scope_reference": OpaqueProviderReference(value=ident(6)),
        "allowed_formats": ("pdf", "docx"),
    }
    values.update(updates)
    return Bitrix24DiskContentPolicy(**values)


def root_policy(**updates: object) -> Bitrix24DiskRootPolicy:
    values: dict[str, object] = {
        "policy_id": ident(11),
        "version": "v1",
        "workspace_id": ident(2),
        "environment_id": ident(3),
        "connection_id": ident(4),
        "connection_version_id": ident(5),
        "resource_scope_reference": OpaqueProviderReference(value=ident(6)),
        "storage_id": "storage-1",
        "storage_class": Bitrix24DiskStorageClass.COMMON,
        "storage_root_object_id": "root-1",
        "approved_root_folder_id": "approved-1",
        "recurse": False,
        "discovery_mode": Bitrix24DiskDiscoveryMode.FIXED_MANIFEST,
        "manifest_object_ids": ("file-1",),
        "content_policy_id": ident(12),
        "content_policy_version": "v1",
        "content_policy_digest": "a" * 64,
        "traversal_profile_reference": OpaqueProviderReference(value=ident(7)),
    }
    values.update(updates)
    return Bitrix24DiskRootPolicy(**values)


def test_content_policy_digest_is_immutable_and_empty_allowlist_denies_all() -> None:
    policy = content_policy(allowed_formats=())
    assert policy.digest == "" or len(policy.digest) == 64
    assert policy.allowed_formats == ()
    with pytest.raises(ValidationError):
        content_policy(digest="f" * 64)


def test_group_root_requires_exact_entity_and_manifest_ids_are_unique() -> None:
    with pytest.raises(ValidationError):
        root_policy(storage_class=Bitrix24DiskStorageClass.GROUP)
    with pytest.raises(ValidationError):
        root_policy(manifest_object_ids=("file-1", "file-1"))


def test_fixed_manifest_requires_exact_member_and_subtree_requires_recursion() -> None:
    with pytest.raises(ValidationError):
        root_policy(manifest_object_ids=())
    with pytest.raises(ValidationError):
        root_policy(
            discovery_mode=Bitrix24DiskDiscoveryMode.APPROVED_SUBTREE,
            recurse=False,
        )


def test_membership_evidence_rejects_ancestry_that_does_not_end_at_approved_root() -> None:
    with pytest.raises(ValidationError):
        Bitrix24DiskMembershipEvidence(
            workspace_id=ident(2),
            environment_id=ident(3),
            connection_id=ident(4),
            connection_version_id=ident(5),
            resource_scope_reference=OpaqueProviderReference(value=ident(6)),
            root_policy_id=ident(11),
            root_policy_version="v1",
            root_policy_digest="a" * 64,
            storage_id="storage-1",
            storage_root_object_id="root-1",
            approved_root_folder_id="approved-1",
            object_id="file-1",
            parent_folder_ids=("unrelated-ancestor",),
        )
