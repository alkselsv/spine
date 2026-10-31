"""Resolve exact current Bitrix24 Disk policy references before traversal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from spine.domain.connections.bitrix24_disk_scope import (
    Bitrix24DiskContentPolicy,
    Bitrix24DiskRootPolicy,
)
from spine.domain.connections.provider import ProviderConnection


class Bitrix24DiskPolicyRepository(Protocol):
    async def get_current_root_policy(
        self, *, workspace_id: UUID, environment_id: UUID, policy_id: UUID
    ) -> Bitrix24DiskRootPolicy | None: ...

    async def get_current_content_policy(
        self, *, workspace_id: UUID, environment_id: UUID, policy_id: UUID
    ) -> Bitrix24DiskContentPolicy | None: ...


@dataclass(frozen=True, slots=True)
class Bitrix24DiskScopeAdmission:
    root_policy: Bitrix24DiskRootPolicy
    content_policy: Bitrix24DiskContentPolicy


class Bitrix24DiskScopeDenied(RuntimeError):
    """Content-free fail-closed policy resolution error."""


class Bitrix24DiskScopeAdmissionService:
    def __init__(self, repository: Bitrix24DiskPolicyRepository) -> None:
        self._repository = repository

    async def resolve_current(
        self,
        connection: ProviderConnection,
        *,
        root_policy_id: UUID,
        root_policy_version: str,
        root_policy_digest: str,
    ) -> Bitrix24DiskScopeAdmission:
        root = await self._repository.get_current_root_policy(
            workspace_id=connection.workspace_id,
            environment_id=connection.environment_id,
            policy_id=root_policy_id,
        )
        if root is None:
            raise Bitrix24DiskScopeDenied("root_policy_unavailable")
        if (
            root.version != root_policy_version
            or root.digest != root_policy_digest
            or root.workspace_id != connection.workspace_id
            or root.environment_id != connection.environment_id
            or root.connection_id != connection.connection_id
            or root.connection_version_id != connection.connection_version_id
            or root.resource_scope_reference not in connection.approved_resource_scope_references
        ):
            raise Bitrix24DiskScopeDenied("root_policy_stale_or_mismatched")
        content = await self._repository.get_current_content_policy(
            workspace_id=connection.workspace_id,
            environment_id=connection.environment_id,
            policy_id=root.content_policy_id,
        )
        if content is None:
            raise Bitrix24DiskScopeDenied("content_policy_unavailable")
        if (
            content.policy_id != root.content_policy_id
            or content.version != root.content_policy_version
            or content.digest != root.content_policy_digest
            or content.workspace_id != root.workspace_id
            or content.environment_id != root.environment_id
            or content.connection_id != root.connection_id
            or content.connection_version_id != root.connection_version_id
            or content.resource_scope_reference != root.resource_scope_reference
            or not content.allowed_formats
        ):
            raise Bitrix24DiskScopeDenied("content_policy_stale_or_mismatched")
        return Bitrix24DiskScopeAdmission(root_policy=root, content_policy=content)


__all__ = [
    "Bitrix24DiskPolicyRepository",
    "Bitrix24DiskScopeAdmission",
    "Bitrix24DiskScopeAdmissionService",
    "Bitrix24DiskScopeDenied",
]
