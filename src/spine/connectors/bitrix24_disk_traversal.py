"""Bounded fake-first Bitrix24 Disk traversal and positive root membership."""

from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from pydantic import Field, StrictInt

from spine.application.connections.bitrix24_oauth import Bitrix24PinnedGrant
from spine.connectors.bitrix24_disk import (
    Bitrix24DiskFile,
    Bitrix24DiskFilePage,
    Bitrix24DiskReadError,
)
from spine.domain.common import DefinitionModel
from spine.domain.connections.bitrix24_disk_scope import (
    Bitrix24DiskMembershipEvidence,
    Bitrix24DiskRootPolicy,
    Bitrix24DiskStorageClass,
)
from spine.domain.connections.provider import OpaqueProviderReference, ProviderConnection


class Bitrix24DiskTraversalProfile(DefinitionModel):
    page_size: StrictInt = Field(default=50, ge=1, le=50)
    max_pages: StrictInt = Field(default=100, ge=1, le=1000)
    max_items: StrictInt = Field(default=10_000, ge=1, le=1_000_000)
    max_depth: StrictInt = Field(default=32, ge=1, le=128)


class Bitrix24DiskStorageIdentity(DefinitionModel):
    storage_id: str
    storage_class: Bitrix24DiskStorageClass
    storage_root_object_id: str
    group_entity_id: str | None = None


class Bitrix24DiskTraversalService(Protocol):
    async def get_storage(
        self,
        connection: ProviderConnection,
        scope: OpaqueProviderReference,
        *,
        storage_id: str,
        pinned_grant: Bitrix24PinnedGrant,
    ) -> Bitrix24DiskStorageIdentity: ...

    async def list_children(
        self,
        connection: ProviderConnection,
        scope: OpaqueProviderReference,
        *,
        folder_id: str,
        start: int,
        page_size: int,
        pinned_grant: Bitrix24PinnedGrant,
    ) -> Bitrix24DiskFilePage: ...

    async def get_folder(
        self,
        connection: ProviderConnection,
        scope: OpaqueProviderReference,
        *,
        folder_id: str,
        pinned_grant: Bitrix24PinnedGrant,
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
        pinned_grant: Bitrix24PinnedGrant,
        authorize_folder: AuthorizeFolder,
    ) -> Bitrix24DiskTraversalResult:
        acting_principal_id = UUID(pinned_grant.acting_user_id)
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
        try:
            storage = await self._service.get_storage(
                connection,
                policy.resource_scope_reference,
                storage_id=policy.storage_id,
                pinned_grant=pinned_grant,
            )
        except Bitrix24DiskReadError:
            return self._incomplete("storage_identity_unverified")
        if (
            storage.storage_id != policy.storage_id
            or storage.storage_class is not policy.storage_class
            or storage.storage_root_object_id != policy.storage_root_object_id
            or storage.group_entity_id != policy.group_entity_id
        ):
            return self._incomplete("storage_identity_mismatch")
        root_chain = await self._folder_chain(
            connection, policy, policy.approved_root_folder_id,
            pinned_grant=pinned_grant,
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
                try:
                    page = await self._service.list_children(
                        connection, policy.resource_scope_reference,
                        folder_id=folder_id, start=start,
                        page_size=self._profile.page_size,
                        pinned_grant=pinned_grant,
                    )
                except Bitrix24DiskReadError:
                    return self._result(evidence, False, "provider_read_incomplete", pages_read, items_read)
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
                    if item.parent_id != folder_id:
                        return self._result(evidence, False, "parent_mismatch", pages_read, items_read)
                    previous = folder_seen.get(item.id)
                    if previous is not None and previous != item:
                        evidence.pop(item.id, None)
                        return self._result(evidence, False, "ambiguous_duplicate", pages_read, items_read)
                    folder_seen[item.id] = item
                    if (
                        item.type == "file"
                        and item.storage_id == policy.storage_id
                        and (policy.future_discovery or item.id in policy.manifest_object_ids)
                    ):
                        proof = await self._membership_evidence(
                            connection,
                            policy,
                            item,
                            pinned_grant=pinned_grant,
                        )
                        if proof is not None:
                            evidence[item.id] = proof
                next_start = start + self._profile.page_size
                if next_start >= (expected_total or 0):
                    break
                if (
                    not page.items
                    or len(page.items) < self._profile.page_size
                    or next_start <= start
                ):
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
        return self._result(evidence, True, "complete", pages_read, items_read)

    async def _membership_evidence(
        self,
        connection: ProviderConnection,
        policy: Bitrix24DiskRootPolicy,
        item: Bitrix24DiskFile,
        *,
        pinned_grant: Bitrix24PinnedGrant,
    ) -> Bitrix24DiskMembershipEvidence | None:
        chain = await self._folder_chain(
            connection,
            policy,
            item.parent_id,
            pinned_grant=pinned_grant,
        )
        if chain is None or policy.approved_root_folder_id not in chain:
            return None
        chain = chain[: chain.index(policy.approved_root_folder_id) + 1]
        return Bitrix24DiskMembershipEvidence(
            workspace_id=policy.workspace_id,
            environment_id=policy.environment_id,
            connection_id=policy.connection_id,
            connection_version_id=policy.connection_version_id,
            resource_scope_reference=policy.resource_scope_reference,
            root_policy_id=policy.policy_id,
            root_policy_version=policy.version,
            root_policy_digest=policy.digest,
            content_policy_id=policy.content_policy_id,
            content_policy_version=policy.content_policy_version,
            content_policy_digest=policy.content_policy_digest,
            authorization_generation=connection.authorization_generation,
            configuration_generation=connection.configuration_generation,
            grant_binding_id=pinned_grant.binding_id,
            grant_token_epoch=pinned_grant.token_epoch,
            grant_security_fence=pinned_grant.security_fence,
            storage_id=policy.storage_id,
            storage_class=policy.storage_class,
            group_entity_id=policy.group_entity_id,
            storage_root_object_id=policy.storage_root_object_id,
            approved_root_folder_id=policy.approved_root_folder_id,
            object_id=item.id,
            parent_folder_ids=chain,
            provider_revision=item.global_content_version,
        )

    async def _folder_chain(
        self,
        connection: ProviderConnection,
        policy: Bitrix24DiskRootPolicy,
        folder_id: str,
        *,
        pinned_grant: Bitrix24PinnedGrant,
    ) -> tuple[str, ...] | None:
        chain: list[str] = []
        current = folder_id
        for _ in range(self._profile.max_depth + 1):
            if current in chain:
                return None
            chain.append(current)
            if current == policy.storage_root_object_id:
                return tuple(chain)
            try:
                folder = await self._service.get_folder(
                    connection, policy.resource_scope_reference,
                    folder_id=current, pinned_grant=pinned_grant,
                )
            except Bitrix24DiskReadError:
                return None
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
    "Bitrix24DiskStorageIdentity",
    "Bitrix24DiskTraversalProfile",
    "Bitrix24DiskTraversalResult",
    "Bitrix24DiskTraversalService",
]
