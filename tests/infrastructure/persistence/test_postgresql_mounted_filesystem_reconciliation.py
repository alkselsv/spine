from types import SimpleNamespace
from datetime import datetime, timezone
import hashlib
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
from spine.domain.sources import RevisionKind


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

    async def rollback(self):
        return None


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


@pytest.mark.asyncio
async def test_v2_occurrence_identity_is_returned_for_reconciliation_reuse() -> None:
    command = _command()
    occurrence = "a" * 32
    path = "returned.md"
    source_id = _id(7)
    revision_id = _id(8)
    external_identity = hashlib.sha256(
        f"{command.resource_scope_reference}:{path}|occurrence:{occurrence}".encode()
    ).hexdigest()

    class UnitOfWork(_UnitOfWork):
        @property
        def canonical(self):
            return self

        async def list_sources(self, *, limit, after):
            del limit
            if after is not None:
                return ()
            return (
                SimpleNamespace(
                    identity_mode=SimpleNamespace(value="connector"),
                    connection_id=command.connection_id,
                    external_namespace="mounted_filesystem",
                    external_generation=(
                        f"mounted-path:v2:{command.connection_version_id.hex}"
                    ),
                    external_object_id=external_identity,
                    source_object_id=source_id,
                    created_at=datetime(2026, 10, 30, tzinfo=timezone.utc),
                ),
            )

        async def resolve_current(self, candidate):
            return SimpleNamespace(current_revision_id=revision_id)

        async def resolve_revision(self, candidate):
            return SimpleNamespace(
                revision_id=revision_id,
                kind=RevisionKind.CONTENT,
                revision_metadata=SimpleNamespace(
                    external_file_revision=SimpleNamespace(
                        provider_kind="mounted_filesystem",
                        identity_assurance="path_scoped",
                        resource_path=path,
                        provider_payload={
                            "resource_scope_reference": str(
                                command.resource_scope_reference
                            ),
                            "connection_version_id": str(command.connection_version_id),
                            "identity_occurrence": occurrence,
                        },
                    )
                ),
            )

    context = SimpleNamespace(
        scope=EnvironmentScope(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
        )
    )
    reader = PostgreSQLMountedFilesystemLifecycleSourceReader(
        lambda supplied_context: UnitOfWork()
    )

    result = await reader.list_sources(command, context)

    assert result[0].identity_occurrence == occurrence
