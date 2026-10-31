"""Bounded fake-first Bitrix24 Disk traversal and positive root membership."""

from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from pydantic import Field, StrictInt

from spine.connectors.bitrix24_disk import Bitrix24DiskFile, Bitrix24DiskFilePage
from spine.domain.common import DefinitionModel
from spine.domain.connections.bitrix24_disk_scope import (
    Bitrix24DiskDiscoveryMode,
    Bitrix24DiskMembershipEvidence,
    Bitrix24DiskRootPolicy,
)
from spine.domain.connections.provider import OpaqueProviderReference, ProviderConnection


class Bitrix24DiskTraversalProfile(DefinitionModel):
    page_size: StrictInt = Field(default=50, ge=1, le=50)
    max_pages: StrictInt = Field(default=100, ge=1, le=1000)
    max_items: StrictInt = Field(default=10_000, ge=1, le=1_000_000)
    max_depth: StrictInt = Field(default=32, ge=1, le=128)


class Bitrix24DiskTraversalService(Protocol):
    async def list_children(
        self,
        connection: ProviderConnection,
        scope: OpaqueProviderReference,
        *,
        folder_id: str,
        start: int,
        page_size: int,
        acting_principal_id: UUID,
    ) -> Bitrix24DiskFilePage: ...

    async def get_folder(
        self,
        connection: ProviderConnection,
        scope: OpaqueProviderReference,
        *,
        folder_id: str,
        acting_principal_id: UUID,
    ) -> Bitrix24DiskFile: ...


AuthorizeFolder = Callable[[str], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class Bitrix24DiskTraversalResult:
    memberships: tuple[Bitrix24DiskMembershipEvidence, ...]
    complete: bool
    reason_code: str
    pages_read: int
    items_read: int


class Bitrix24DiskRootTraversal:
    """Enumerate only the pinned root and emit positive, exact membership proofs."""

    def __init__(
        self,
        *,
        service: Bitrix24DiskTraversalService,
        profile: Bitrix24DiskTraversalProfile,
    ) -> None:
        self._service = service
        self._profile = profile

    async def scan(
        self,
        connection: ProviderConnection,
        policy: Bitrix24DiskRootPolicy,
        *,
        acting_principal_id: UUID,
        authorize_folder: AuthorizeFolder,
    ) -> Bitrix24DiskTraversalResult:
        if acting_principal_id.int == 0 or acting_principal_id == connection.service_principal_id:
            return self._incomplete("acting_user_authority_required")
        if (
            policy.workspace_id,
            policy.environment_id,
            policy.connection_id,
            policy.connection_version_id,
        ) != (
            connection.workspace_id,
            connection.environment_id,
            connection.connection_id,
            connection.connection_version_id,
        ):
            return self._incomplete("scope_mismatch")
        if policy.resource_scope_reference not in connection.approved_resource_scope_references:
            return self._incomplete("scope_mismatch")
        root_chain = await self._folder_chain(
            connection, policy, policy.approved_root_folder_id,
            acting_principal_id=acting_principal_id,
        )
        if root_chain is None:
            return self._incomplete("root_ancestry_unverified")
        if root_chain[-1] != policy.storage_root_object_id:
            return self._incomplete("root_outside_storage")

        pending: deque[tuple[str, int]] = deque([(policy.approved_root_folder_id, 0)])
        visited: set[str] = set()
        evidence: dict[str, Bitrix24DiskMembershipEvidence] = {}
        pages_read = 0
        items_read = 0
        while pending:
            folder_id, depth = pending.popleft()
            if folder_id in visited:
                return self._result(evidence, False, "ancestry_cycle", pages_read, items_read)
            visited.add(folder_id)
            if depth > 0 and not policy.recurse:
                continue
            if not await authorize_folder(folder_id):
                return self._result(evidence, False, "read_authority_denied", pages_read, items_read)
            start = 0
            expected_total: int | None = None
            folder_seen: dict[str, Bitrix24DiskFile] = {}
            while True:
                pages_read += 1
                if pages_read > self._profile.max_pages:
                    return self._result(evidence, False, "page_limit_exceeded", pages_read - 1, items_read)
                page = await self._service.list_children(
                    connection, policy.resource_scope_reference,
                    folder_id=folder_id, start=start,
                    page_size=self._profile.page_size,
                    acting_principal_id=acting_principal_id,
                )
                if len(page.items) > self._profile.page_size:
                    return self._result(evidence, False, "invalid_page", pages_read, items_read)
                if expected_total is None:
                    expected_total = page.total
                elif page.total != expected_total:
                    return self._result(evidence, False, "page_drift", pages_read, items_read)
                items_read += len(page.items)
                if items_read > self._profile.max_items:
                    return self._result(evidence, False, "item_limit_exceeded", pages_read, items_read - len(page.items))
                for item in page.items:
                    previous = folder_seen.get(item.id)
                    if previous is not None and previous != item:
                        return self._result(evidence, False, "ambiguous_duplicate", pages_read, items_read)
                    folder_seen[item.id] = item
                next_start = start + self._profile.page_size
                if next_start >= (expected_total or 0):
                    break
                if not page.items or next_start <= start:
                    return self._result(evidence, False, "page_gap", pages_read, items_read)
                start = next_start
            for item in folder_seen.values():
                if item.storage_id != policy.storage_id:
                    continue
                if item.type == "folder":
                    if policy.recurse:
                        if depth >= self._profile.max_depth:
                            return self._result(evidence, False, "depth_limit_exceeded", pages_read, items_read)
                        pending.append((item.id, depth + 1))
                    continue
                if policy.discovery_mode is Bitrix24DiskDiscoveryMode.FIXED_MANIFEST and item.id not in policy.manifest_object_ids:
                    continue
                chain = await self._folder_chain(
                    connection, policy, item.parent_id,
                    acting_principal_id=acting_principal_id,
                )
                if chain is None or policy.approved_root_folder_id not in chain:
                    continue
                chain = chain[: chain.index(policy.approved_root_folder_id) + 1]
                evidence[item.id] = Bitrix24DiskMembershipEvidence(
                    workspace_id=policy.workspace_id,
                    environment_id=policy.environment_id,
                    connection_id=policy.connection_id,
                    connection_version_id=policy.connection_version_id,
                    resource_scope_reference=policy.resource_scope_reference,
                    root_policy_id=policy.policy_id,
                    root_policy_version=policy.version,
                    root_policy_digest=policy.digest,
                    storage_id=policy.storage_id,
                    storage_root_object_id=policy.storage_root_object_id,
                    approved_root_folder_id=policy.approved_root_folder_id,
                    object_id=item.id,
                    parent_folder_ids=chain,
                    provider_revision=item.global_content_version,
                )
        return self._result(evidence, True, "complete", pages_read, items_read)

    async def _folder_chain(
        self,
        connection: ProviderConnection,
        policy: Bitrix24DiskRootPolicy,
        folder_id: str,
        *,
        acting_principal_id: UUID,
    ) -> tuple[str, ...] | None:
        chain: list[str] = []
        current = folder_id
        for _ in range(self._profile.max_depth + 1):
            if current in chain:
                return None
            chain.append(current)
            if current == policy.storage_root_object_id:
                return tuple(chain)
            folder = await self._service.get_folder(
                connection, policy.resource_scope_reference,
                folder_id=current, acting_principal_id=acting_principal_id,
            )
            if (
                folder.id != current
                or folder.type != "folder"
                or folder.storage_id != policy.storage_id
            ):
                return None
            current = folder.parent_id
        return None

    def _incomplete(self, reason: str) -> Bitrix24DiskTraversalResult:
        return self._result({}, False, reason, 0, 0)

    @staticmethod
    def _result(
        memberships: dict[str, Bitrix24DiskMembershipEvidence],
        complete: bool,
        reason: str,
        pages: int,
        items: int,
    ) -> Bitrix24DiskTraversalResult:
        return Bitrix24DiskTraversalResult(
            memberships=tuple(memberships[key] for key in sorted(memberships)),
            complete=complete,
            reason_code=reason,
            pages_read=pages,
            items_read=items,
        )


__all__ = [
    "Bitrix24DiskRootTraversal",
    "Bitrix24DiskTraversalProfile",
    "Bitrix24DiskTraversalResult",
    "Bitrix24DiskTraversalService",
]
