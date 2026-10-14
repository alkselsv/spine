from __future__ import annotations

import asyncio

import pytest

from spine.application.persistence.unit_of_work_activity import (
    track_unit_of_work_activity,
    unit_of_work_is_active,
)


def test_nested_activity_remains_active_until_outer_scope_closes() -> None:
    assert unit_of_work_is_active() is False

    with track_unit_of_work_activity():
        assert unit_of_work_is_active() is True
        with track_unit_of_work_activity():
            assert unit_of_work_is_active() is True
        assert unit_of_work_is_active() is True

    assert unit_of_work_is_active() is False


def test_activity_is_cleared_when_scope_raises() -> None:
    with pytest.raises(RuntimeError, match="synthetic failure"):
        with track_unit_of_work_activity():
            assert unit_of_work_is_active() is True
            raise RuntimeError("synthetic failure")

    assert unit_of_work_is_active() is False


@pytest.mark.asyncio
async def test_parent_activity_is_not_inherited_as_child_task_activity() -> None:
    async def activity_is_visible() -> bool:
        return unit_of_work_is_active()

    with track_unit_of_work_activity():
        assert unit_of_work_is_active() is True
        assert await asyncio.create_task(activity_is_visible()) is False

    assert unit_of_work_is_active() is False
