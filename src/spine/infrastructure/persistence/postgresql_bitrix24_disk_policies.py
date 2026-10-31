"""PostgreSQL persistence for immutable Bitrix24 Disk policy heads."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TypeVar
from uuid import UUID

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from spine.domain.connections.bitrix24_disk_scope import (
    Bitrix24DiskContentPolicy,
    Bitrix24DiskRootPolicy,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    bitrix24_disk_content_policy_versions,
    bitrix24_disk_root_policy_versions,
    current_bitrix24_disk_content_policies,
    current_bitrix24_disk_root_policies,
)

Policy = TypeVar("Policy", Bitrix24DiskContentPolicy, Bitrix24DiskRootPolicy)


class Bitrix24DiskPolicyConflict(RuntimeError):
    """The expected current policy head changed or an immutable version conflicts."""


class PostgreSQLBitrix24DiskPolicyRepository:
    def __init__(
        self,
        *,
        uow_factory: Callable[..., object],
        context_factory: Callable[[str, UUID, UUID], object],
    ) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory

    async def get_current_root_policy(
        self, *, workspace_id: UUID, environment_id: UUID, policy_id: UUID
    ) -> Bitrix24DiskRootPolicy | None:
        return await self._current(
            workspace_id,
            environment_id,
            policy_id,
            current_bitrix24_disk_root_policies,
            bitrix24_disk_root_policy_versions,
            Bitrix24DiskRootPolicy,
        )

    async def get_current_content_policy(
        self, *, workspace_id: UUID, environment_id: UUID, policy_id: UUID
    ) -> Bitrix24DiskContentPolicy | None:
        return await self._current(
            workspace_id,
            environment_id,
            policy_id,
            current_bitrix24_disk_content_policies,
            bitrix24_disk_content_policy_versions,
            Bitrix24DiskContentPolicy,
        )

    async def publish_root_policy(
        self,
        policy: Bitrix24DiskRootPolicy,
        *,
        expected_version: str | None,
        expected_digest: str | None,
        recorded_at: datetime,
    ) -> None:
        await self._publish(
            policy,
            current_bitrix24_disk_root_policies,
            bitrix24_disk_root_policy_versions,
            expected_version=expected_version,
            expected_digest=expected_digest,
            recorded_at=recorded_at,
        )

    async def publish_content_policy(
        self,
        policy: Bitrix24DiskContentPolicy,
        *,
        expected_version: str | None,
        expected_digest: str | None,
        recorded_at: datetime,
    ) -> None:
        await self._publish(
            policy,
            current_bitrix24_disk_content_policies,
            bitrix24_disk_content_policy_versions,
            expected_version=expected_version,
            expected_digest=expected_digest,
            recorded_at=recorded_at,
        )

    async def _current(
        self,
        workspace_id: UUID,
        environment_id: UUID,
        policy_id: UUID,
        heads: object,
        versions: object,
        model: type[Policy],
    ) -> Policy | None:
        context = self._context_factory("bitrix24_disk_policy.read", workspace_id, environment_id)
        async with self._uow_factory(context) as uow:
            result = await uow._execute(  # type: ignore[attr-defined]
                select(versions.c.record)
                .select_from(
                    versions.join(
                        heads,
                        (heads.c.workspace_id == versions.c.workspace_id)
                        & (heads.c.environment_id == versions.c.environment_id)
                        & (heads.c.policy_id == versions.c.policy_id)
                        & (heads.c.current_version == versions.c.version)
                        & (heads.c.current_digest == versions.c.digest),
                    )
                )
                .where(
                    versions.c.workspace_id == workspace_id,
                    versions.c.environment_id == environment_id,
                    versions.c.policy_id == policy_id,
                )
            )
            row = result.one_or_none()
            await uow.rollback()  # type: ignore[attr-defined]
            return None if row is None else model.model_validate(row.record)

    async def _publish(
        self,
        policy: Policy,
        heads: object,
        versions: object,
        *,
        expected_version: str | None,
        expected_digest: str | None,
        recorded_at: datetime,
    ) -> None:
        if (expected_version is None) != (expected_digest is None):
            raise Bitrix24DiskPolicyConflict()
        context = self._context_factory(
            "bitrix24_disk_policy.write", policy.workspace_id, policy.environment_id
        )
        try:
            async with self._uow_factory(context) as uow:
                current_result = await uow._execute(  # type: ignore[attr-defined]
                    select(heads)
                    .where(
                        heads.c.workspace_id == policy.workspace_id,
                        heads.c.environment_id == policy.environment_id,
                        heads.c.policy_id == policy.policy_id,
                    )
                    .with_for_update()
                )
                current = current_result.mappings().one_or_none()
                actual = None if current is None else (current.current_version, current.current_digest)
                if actual != (expected_version, expected_digest):
                    raise Bitrix24DiskPolicyConflict()
                generation = 1 if current is None else current.generation + 1
                await uow._execute(  # type: ignore[attr-defined]
                    insert(versions).values(
                        policy_id=policy.policy_id,
                        version=policy.version,
                        digest=policy.digest,
                        workspace_id=policy.workspace_id,
                        environment_id=policy.environment_id,
                        connection_id=policy.connection_id,
                        connection_version_id=policy.connection_version_id,
                        resource_scope_reference=policy.resource_scope_reference.value,
                        record=policy.model_dump(mode="json"),
                        recorded_at=recorded_at,
                        **(
                            {
                                "content_policy_id": policy.content_policy_id,
                                "content_policy_version": policy.content_policy_version,
                                "content_policy_digest": policy.content_policy_digest,
                            }
                            if isinstance(policy, Bitrix24DiskRootPolicy)
                            else {}
                        ),
                    )
                )
                head_values = {
                    "current_version": policy.version,
                    "current_digest": policy.digest,
                    "predecessor_version": expected_version,
                    "predecessor_digest": expected_digest,
                    "generation": generation,
                    "updated_at": recorded_at,
                }
                if current is None:
                    await uow._execute(  # type: ignore[attr-defined]
                        insert(heads).values(
                            workspace_id=policy.workspace_id,
                            environment_id=policy.environment_id,
                            policy_id=policy.policy_id,
                            **head_values,
                        )
                    )
                else:
                    await uow._execute(  # type: ignore[attr-defined]
                        update(heads)
                        .where(
                            heads.c.workspace_id == policy.workspace_id,
                            heads.c.environment_id == policy.environment_id,
                            heads.c.policy_id == policy.policy_id,
                            heads.c.current_version == expected_version,
                            heads.c.current_digest == expected_digest,
                        )
                        .values(**head_values)
                    )
                await uow.commit()  # type: ignore[attr-defined]
        except IntegrityError:
            raise Bitrix24DiskPolicyConflict() from None


__all__ = ["Bitrix24DiskPolicyConflict", "PostgreSQLBitrix24DiskPolicyRepository"]
