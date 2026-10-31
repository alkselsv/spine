"""Resolve exact current Bitrix24 Disk policy references before traversal."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from spine.domain.connections.bitrix24_disk_scope import (
    Bitrix24DiskContentPolicy,
    Bitrix24DiskRootPolicy,
    Bitrix24DiskScopeAdmission,
)
from spine.domain.connections.provider import ProviderConnection
from spine.application.connections.bitrix24_oauth import (
    Bitrix24GrantAdmission,
    Bitrix24OAuthError,
    Bitrix24ReadAuthorization,
    Bitrix24ReadMethod,
)
from spine.domain.connections.bitrix24_oauth import Bitrix24Scope
from spine.connectors.bitrix24_disk_traversal import (
    AuthorizeFolder,
    Bitrix24DiskRootTraversal,
    Bitrix24DiskTraversalResult,
)


class Bitrix24DiskPolicyRepository(Protocol):
    async def get_current_root_policy(
        self, *, workspace_id: UUID, environment_id: UUID, policy_id: UUID
    ) -> Bitrix24DiskRootPolicy | None: ...

    async def get_current_content_policy(
        self, *, workspace_id: UUID, environment_id: UUID, policy_id: UUID
    ) -> Bitrix24DiskContentPolicy | None: ...


class Bitrix24DiskScopeDenied(RuntimeError):
    """Content-free fail-closed policy resolution error."""


class Bitrix24DiskTraversalCoordinator:
    """Resolve current root policy and S1 actor grant around each bounded scan."""

    def __init__(
        self,
        *,
        scope_admission: "Bitrix24DiskScopeAdmissionService",
        grant_admission: Bitrix24GrantAdmission,
        traversal: Bitrix24DiskRootTraversal,
    ) -> None:
        self._scope_admission = scope_admission
        self._grant_admission = grant_admission
        self._traversal = traversal

    async def scan(
        self,
        connection: ProviderConnection,
        *,
        root_policy_id: UUID,
        root_policy_version: str,
        root_policy_digest: str,
        authorize_folder: AuthorizeFolder,
    ) -> Bitrix24DiskTraversalResult:
        try:
            current = await self._scope_admission.resolve_current(
                connection,
                root_policy_id=root_policy_id,
                root_policy_version=root_policy_version,
                root_policy_digest=root_policy_digest,
            )
            grant = await self._grant_admission.resolve(
                connection,
                Bitrix24ReadAuthorization(
                    method=Bitrix24ReadMethod.DISK_READ,
                    required_scope=Bitrix24Scope.DISK,
                ),
            )
        except (Bitrix24DiskScopeDenied, Bitrix24OAuthError):
            return self._traversal.incomplete("current_authority_unavailable")

        result = await self._traversal.scan(
            connection,
            current,
            pinned_grant=grant,
            authorize_folder=authorize_folder,
        )
        if not result.complete and not result.memberships:
            return result
        try:
            current_after = await self._scope_admission.resolve_current(
                connection,
                root_policy_id=root_policy_id,
                root_policy_version=root_policy_version,
                root_policy_digest=root_policy_digest,
            )
            grant_after = await self._grant_admission.resolve(
                connection,
                Bitrix24ReadAuthorization(
                    method=Bitrix24ReadMethod.DISK_READ,
                    required_scope=Bitrix24Scope.DISK,
                ),
            )
        except (Bitrix24DiskScopeDenied, Bitrix24OAuthError):
            return self._traversal.incomplete("authority_superseded_during_scan")
        if (
            current_after != current
            or (
                grant_after.binding_id,
                grant_after.acting_user_id,
                grant_after.token_epoch,
                grant_after.security_fence,
            )
            != (
                grant.binding_id,
                grant.acting_user_id,
                grant.token_epoch,
                grant.security_fence,
            )
        ):
            return self._traversal.incomplete("authority_superseded_during_scan")
        return result


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
        try:
            return Bitrix24DiskScopeAdmission(root_policy=root, content_policy=content)
        except ValueError:
            raise Bitrix24DiskScopeDenied("policy_binding_mismatch") from None


__all__ = [
    "Bitrix24DiskPolicyRepository",
    "Bitrix24DiskScopeAdmission",
    "Bitrix24DiskScopeAdmissionService",
    "Bitrix24DiskScopeDenied",
    "Bitrix24DiskTraversalCoordinator",
]
