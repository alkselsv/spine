from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.infrastructure.object_storage import AuthorizedReadGrantRegistry
from spine.infrastructure.object_storage.testing import (
    DeterministicOriginalObjectStoreFake,
)

from .adapter import OriginalObjectStoreAdapter, OriginalObjectStoreFactory


NOW = datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)


def create_deterministic_original_object_store() -> OriginalObjectStoreAdapter:
    registry = AuthorizedReadGrantRegistry()
    generations = iter(
        UUID(f"50000000-0000-4000-8000-{value:012d}") for value in range(1, 100)
    )
    store = DeterministicOriginalObjectStoreFake(
        grant_registry=registry,
        generation_factory=lambda: next(generations),
        clock=lambda: NOW,
        max_chunk_bytes=8,
        max_object_bytes=64,
        read_chunk_bytes=3,
    )
    return OriginalObjectStoreAdapter(
        store=store,
        grant_registry=registry,
        now=NOW,
        max_write_chunk_bytes=8,
        max_object_bytes=64,
        read_chunk_bytes=3,
    )


@pytest.fixture(
    params=[create_deterministic_original_object_store],
    ids=["deterministic-fake"],
)
def original_object_store_factory(
    request: pytest.FixtureRequest,
) -> OriginalObjectStoreFactory:
    """Adapters extend this parameter list to run the shared suite unchanged."""

    return request.param
