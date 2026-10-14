"""Task-local visibility of an open canonical persistence boundary.

Infrastructure adapters enter this scope for the lifetime of a Unit of Work.
Application services can then reject physical I/O while canonical persistence is
still open without depending on a concrete database adapter.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TypeAlias


_ActivityOwner: TypeAlias = asyncio.Task[object] | None


_ACTIVE_UNIT_OF_WORK_OWNERS: ContextVar[tuple[_ActivityOwner, ...]] = ContextVar(
    "spine_active_unit_of_work_owners",
    default=(),
)


def _current_owner() -> _ActivityOwner:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def unit_of_work_is_active() -> bool:
    """Return whether the current task is inside any tracked Unit of Work."""

    owner = _current_owner()
    return any(
        active_owner is owner
        for active_owner in _ACTIVE_UNIT_OF_WORK_OWNERS.get()
    )


@contextmanager
def track_unit_of_work_activity() -> Iterator[None]:
    """Track a Unit of Work until its infrastructure context has closed."""

    owners = _ACTIVE_UNIT_OF_WORK_OWNERS.get()
    token = _ACTIVE_UNIT_OF_WORK_OWNERS.set((*owners, _current_owner()))
    try:
        yield
    finally:
        _ACTIVE_UNIT_OF_WORK_OWNERS.reset(token)


__all__ = ["track_unit_of_work_activity", "unit_of_work_is_active"]
