from __future__ import annotations

import asyncio
import sys

import pytest


if sys.platform == "win32":
    # psycopg's async driver requires SelectorEventLoop on Windows.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Expose stable node IDs in JUnit evidence without test payloads."""

    for item in items:
        item.user_properties.append(("spine_nodeid", item.nodeid))
