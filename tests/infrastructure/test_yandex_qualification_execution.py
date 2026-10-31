from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from spine.application.yandex_qualification_execution import (
    YandexQualificationExecutionDeniedError,
    YandexQualificationExecutionScope,
)
from spine.domain.connections.provider import OpaqueProviderReference
from spine.infrastructure.yandex_qualification_execution import (
    FileYandexQualificationExecutionLease,
)


def _scope() -> YandexQualificationExecutionScope:
    return YandexQualificationExecutionScope(
        workspace_id=UUID("00000000-0000-4000-8000-000000000001"),
        environment_id=UUID("00000000-0000-4000-8000-000000000002"),
        connection_id=UUID("00000000-0000-4000-8000-000000000003"),
        connection_version_id=UUID("00000000-0000-4000-8000-000000000004"),
        resource_scope_reference=OpaqueProviderReference(
            value=UUID("00000000-0000-4000-8000-000000000005")
        ),
        authorization_generation=3,
        configuration_generation=4,
    )


@pytest.mark.asyncio
async def test_file_execution_lease_denies_concurrent_scope_and_releases_after_exit(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "execution.lock").resolve()
    first = FileYandexQualificationExecutionLease(
        lock_file=path,
        approved_scope=_scope(),
    )
    second = FileYandexQualificationExecutionLease(
        lock_file=path,
        approved_scope=_scope(),
    )

    async with first.hold(_scope()):
        with pytest.raises(YandexQualificationExecutionDeniedError):
            async with second.hold(_scope()):
                pytest.fail("concurrent qualification lease must be denied")

    async with second.hold(_scope()):
        assert path.stat().st_mode & 0o777 == 0o600
