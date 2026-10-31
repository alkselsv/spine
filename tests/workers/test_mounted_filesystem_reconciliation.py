from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from spine.connectors.files.mounted_filesystem import MountedFilesystemRoot
from spine.domain.connections.provider import OpaqueProviderReference
from spine.infrastructure.persistence.postgresql_mounted_filesystem_reconciliation import (
    PostgreSQLMountedFilesystemLifecycleSourceReader,
    PostgreSQLMountedFilesystemReconciliationRepository,
    PostgreSQLMountedFilesystemRemovalPolicyAdmission,
)
from spine.application.mounted_filesystem_reconciliation import (
    MountedFilesystemSourceLifecycleRemoval,
    ServerMountedFilesystemTombstoneComposer,
)
from spine.workers.mounted_filesystem_reconciliation import (
    create_postgresql_mounted_filesystem_reconciliation_worker,
)


def _id(number: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{number:012d}")


def test_production_worker_composes_postgresql_and_authorized_adapters(
    tmp_path: Path,
) -> None:
    root = tmp_path / "mount"
    root.mkdir()
    worker = create_postgresql_mounted_filesystem_reconciliation_worker(
        uow_factory=lambda context: None,
        context_factory=lambda operation, identity: None,
        connection_repository=object(),
        roots={
            _id(1): MountedFilesystemRoot(
                resource_scope_reference=OpaqueProviderReference(value=_id(2)),
                path=root,
            )
        },
        ingestion=object(),
        document_lifecycle_composer=object(),
        source_lifecycle=object(),
        lifecycle_reader=object(),
        clock=lambda: datetime(2026, 10, 30, tzinfo=timezone.utc),
    )

    assert isinstance(
        worker.coordinator.repository,
        PostgreSQLMountedFilesystemReconciliationRepository,
    )
    assert isinstance(
        worker.coordinator.sources,
        PostgreSQLMountedFilesystemLifecycleSourceReader,
    )
    assert isinstance(
        worker.coordinator.removal_policies,
        PostgreSQLMountedFilesystemRemovalPolicyAdmission,
    )
    assert isinstance(
        worker.coordinator.removals,
        MountedFilesystemSourceLifecycleRemoval,
    )
    assert isinstance(
        worker.coordinator.removals.composer,
        ServerMountedFilesystemTombstoneComposer,
    )
