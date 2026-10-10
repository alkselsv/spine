from __future__ import annotations

from collections.abc import Awaitable, Callable

from spine.auth.ports import AuthorizationDirectory
from spine.auth.records import AuthorizationDirectoryRecords


AuthorizationDirectoryFactory = Callable[
    [AuthorizationDirectoryRecords], Awaitable[AuthorizationDirectory]
]
