"""Production composition seam for bounded mounted-filesystem reconciliation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping
from uuid import UUID

from spine.application.connections.provider import ProviderConnectionRepository
from spine.application.document_lifecycle import ServerDocumentLifecycleComposer
from spine.application.mounted_filesystem_reconciliation import (
    AuthorizedMountedFilesystemScanner,
    MountedFilesystemIngestionPort,
    MountedFilesystemReconciliationCommand,
    MountedFilesystemReconciliationCoordinator,
    MountedFilesystemReconciliationRun,
    MountedFilesystemRemovalPolicy,
    MountedFilesystemSourceLifecycleRemoval,
    ServerMountedFilesystemTombstoneComposer,
)
from spine.application.persistence.context import (
    PersistenceOperation,
    TrustedPersistenceContext,
)
from spine.application.source_lifecycle import SourceLifecycleCoordinator
from spine.connectors.files.mounted_filesystem import (
    MountedFilesystemRoot,
    MountedFilesystemTreeScanner,
)
from spine.infrastructure.persistence.postgresql_mounted_filesystem_reconciliation import (
    PostgreSQLMountedFilesystemLifecycleSourceReader,
    PostgreSQLMountedFilesystemReconciliationRepository,
    PostgreSQLMountedFilesystemRemovalPolicyAdmission,
)


@dataclass(frozen=True, slots=True)
class MountedFilesystemReconciliationWorker:
    """Worker-callable entry point for one approved reconciliation generation."""

    coordinator: MountedFilesystemReconciliationCoordinator

    async def reconcile(
        self,
        command: MountedFilesystemReconciliationCommand,
        policy: MountedFilesystemRemovalPolicy,
    ) -> MountedFilesystemReconciliationRun:
        return await self.coordinator.run(command, policy)


def create_postgresql_mounted_filesystem_reconciliation_worker(
    *,
    uow_factory: Callable[[TrustedPersistenceContext], object],
    context_factory: Callable[
        [PersistenceOperation, UUID], TrustedPersistenceContext
    ],
    connection_repository: ProviderConnectionRepository,
    roots: Mapping[UUID, MountedFilesystemRoot],
    ingestion: MountedFilesystemIngestionPort,
    document_lifecycle_composer: ServerDocumentLifecycleComposer,
    source_lifecycle: SourceLifecycleCoordinator,
    clock: Callable[[], datetime],
    max_read_attempts: int = 2,
) -> MountedFilesystemReconciliationWorker:
    """Bind production PostgreSQL state, authorization and filesystem adapters."""

    repository = PostgreSQLMountedFilesystemReconciliationRepository(
        uow_factory=uow_factory,
        context_factory=context_factory,
    )
    coordinator = MountedFilesystemReconciliationCoordinator(
        repository=repository,
        scanner=AuthorizedMountedFilesystemScanner(
            repository=connection_repository,
            provider=MountedFilesystemTreeScanner(roots=roots),
        ),
        ingestion=ingestion,
        sources=PostgreSQLMountedFilesystemLifecycleSourceReader(uow_factory),
        removals=MountedFilesystemSourceLifecycleRemoval(
            composer=ServerMountedFilesystemTombstoneComposer(
                lifecycle_composer=document_lifecycle_composer,
            ),
            lifecycle=source_lifecycle,
        ),
        removal_policies=PostgreSQLMountedFilesystemRemovalPolicyAdmission(
            uow_factory
        ),
        context_factory=context_factory,
        clock=clock,
        max_read_attempts=max_read_attempts,
    )
    return MountedFilesystemReconciliationWorker(coordinator)


__all__ = [
    "MountedFilesystemReconciliationWorker",
    "create_postgresql_mounted_filesystem_reconciliation_worker",
]
