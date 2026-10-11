from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from spine.infrastructure.object_storage import (
    AuthorizedReadGrantRegistry,
    OriginalObjectStore,
)


@dataclass(frozen=True, slots=True)
class OriginalObjectStoreAdapter:
    """Public conformance seam supplied by each storage implementation."""

    store: OriginalObjectStore
    grant_registry: AuthorizedReadGrantRegistry
    now: datetime
    max_write_chunk_bytes: int
    max_object_bytes: int
    read_chunk_bytes: int


OriginalObjectStoreFactory = Callable[[], OriginalObjectStoreAdapter]
