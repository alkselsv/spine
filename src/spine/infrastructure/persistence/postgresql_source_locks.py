"""Canonical PostgreSQL source lifecycle and admission transaction locks.

These locks are shared by canonical admission, lifecycle mutation, and consumers
that must fence a source revision while publishing derived artifacts. Keep the
lock identity and ordering here so an adapter cannot accidentally create a
private serialization domain.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select, text


Execute = Callable[..., Awaitable[object]]


def source_lifecycle_key(
    *, workspace_id: UUID, environment_id: UUID, source_object_id: UUID
) -> str:
    return f"{workspace_id}:{environment_id}:{source_object_id}"


async def lock_source_lifecycle_shared(
    execute: Execute, *, workspace_id: UUID, environment_id: UUID,
    source_object_id: UUID,
) -> None:
    await execute(
        text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:key, 0))"),
        {"key": source_lifecycle_key(
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_object_id=source_object_id,
        )},
    )


async def lock_source_lifecycle_exclusive(
    execute: Execute, *, workspace_id: UUID, environment_id: UUID,
    source_object_id: UUID,
) -> None:
    await execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": source_lifecycle_key(
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_object_id=source_object_id,
        )},
    )


def retention_source_class_key(
    *, workspace_id: UUID, environment_id: UUID, source_class: str
) -> str:
    return f"retention-source-class:{workspace_id}:{environment_id}:{source_class}"


async def lock_retention_source_class_shared(
    execute: Execute,
    *,
    workspace_id: UUID,
    environment_id: UUID,
    source_class: str,
) -> None:
    await execute(
        text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:key, 0))"),
        {
            "key": retention_source_class_key(
                workspace_id=workspace_id,
                environment_id=environment_id,
                source_class=source_class,
            )
        },
    )


async def lock_retention_source_class_exclusive(
    execute: Execute,
    *,
    workspace_id: UUID,
    environment_id: UUID,
    source_class: str,
) -> None:
    await execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {
            "key": retention_source_class_key(
                workspace_id=workspace_id,
                environment_id=environment_id,
                source_class=source_class,
            )
        },
    )


def admission_serialization_key(
    *, workspace_id: UUID, environment_id: UUID, source_object_id: UUID,
    revision_id: UUID,
) -> tuple[int, int]:
    """Return canonical namespaced int4 parts for the admission xact lock."""
    lock_id = uuid5(
        NAMESPACE_URL,
        "spine.admission-serialization.v1:"
        f"{workspace_id}:{environment_id}:{source_object_id}:{revision_id}",
    )
    return (
        int.from_bytes(lock_id.bytes[:4], byteorder="big", signed=True),
        int.from_bytes(lock_id.bytes[4:8], byteorder="big", signed=True),
    )


async def lock_admission_revision(
    execute: Execute, *, workspace_id: UUID, environment_id: UUID,
    source_object_id: UUID, revision_id: UUID,
) -> None:
    key = admission_serialization_key(
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_object_id=source_object_id,
        revision_id=revision_id,
    )
    await execute(select(func.pg_advisory_xact_lock(*key)))
