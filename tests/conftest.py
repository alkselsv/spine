from __future__ import annotations

import asyncio
import os
import sys

import pytest

pytest_plugins = (
    ("tests.qualification.r1_target_receipt_plugin",)
    if os.getenv("SPINE_R1_TARGET_ROLE") is not None
    else ()
)


if sys.platform == "win32":
    # psycopg's async driver requires SelectorEventLoop on Windows.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Expose stable node IDs in JUnit evidence without test payloads."""

    for item in items:
        item.user_properties.append(("spine_nodeid", item.nodeid))
