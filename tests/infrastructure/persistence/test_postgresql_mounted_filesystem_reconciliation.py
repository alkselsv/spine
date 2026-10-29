from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.persistence.context import EnvironmentScope
from spine.application.mounted_filesystem_reconciliation import (
    MountedFilesystemReconciliationCommand,
)
from spine.infrastructure.persistence.postgresql_mounted_filesystem_reconciliation import (
    MountedFilesystemIdentityMigrationRequiredError,
    PostgreSQLMountedFilesystemLifecycleSourceReader,
)


def _id(number: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{number:012d}")


def _command() -> MountedFilesystemReconciliationCommand:
    return MountedFilesystemReconciliationCommand(
        workspace_id=_id(1),
        environment_id=_id(2),
        connection_id=_id(3),
        connection_version_id=_id(4),
        resource_scope_reference=_id(5),
        authorization_generation=1,
        configuration_generation=1,
        access_policy_version_id=_id(6),
        idempotency_key="legacy-v1",
    )


class _UnitOfWork:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    @property
    def sources(self):
        return self

    async def list_sources(self, *, limit, after):
        del limit
        if after is not None:
            return ()
        return (
            SimpleNamespace(
                identity_mode=SimpleNamespace(value="connector"),
                connection_id=_command().connection_id,
                external_namespace="mounted_filesystem",
                external_generation="mounted-path:v1",
            ),
        )


@pytest.mark.asyncio
async def test_legacy_v1_identity_fails_closed_before_source_or_removal_effect() -> None:
    command = _command()
    context = SimpleNamespace(
        scope=EnvironmentScope(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
        )
    )
    reader = PostgreSQLMountedFilesystemLifecycleSourceReader(
        lambda supplied_context: _UnitOfWork()
    )

    with pytest.raises(MountedFilesystemIdentityMigrationRequiredError):
        await reader.list_sources(command, context)
