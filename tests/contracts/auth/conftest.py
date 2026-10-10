from __future__ import annotations

import pytest

from spine.auth.in_memory import InMemoryAuthorizationDirectory
from spine.auth.records import AuthorizationDirectoryRecords

from .adapter import AuthorizationDirectoryFactory


async def create_in_memory_directory(
    records: AuthorizationDirectoryRecords,
) -> InMemoryAuthorizationDirectory:
    return InMemoryAuthorizationDirectory(records=records)


@pytest.fixture
def authorization_directory_factory() -> AuthorizationDirectoryFactory:
    return create_in_memory_directory
