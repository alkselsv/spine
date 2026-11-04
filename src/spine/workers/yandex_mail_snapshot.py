"""Production composition seam for one bounded Yandex Mail initial snapshot."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from spine.application.connector_ingestion import ConnectorIngestionOrchestrator
from spine.application.email_sync import EmailSyncCoordinator
from spine.application.persistence.context import (
    PersistenceOperation,
    TrustedPersistenceContext,
)
from spine.application.yandex_mail_snapshot import (
    YandexMailInitialSnapshotCommand,
    YandexMailInitialSnapshotCoordinator,
    YandexMailInitialSnapshotResult,
)
from spine.connectors.yandex_mail_folders import YandexMailFolderBindingRepository
from spine.connectors.yandex_mail_observation import YandexMailRawMessageAdmission
from spine.connectors.yandex_mail_snapshot import (
    YandexMailAdmittedSnapshotReader,
    YandexMailCanonicalSnapshotMessageAdapter,
    YandexMailInitialSnapshotAdapter,
)


@dataclass(frozen=True, slots=True)
class YandexMailInitialSnapshotOperation:
    """Executable use case exposed by the worker composition root."""

    coordinator: YandexMailInitialSnapshotCoordinator

    async def run(
        self, command: YandexMailInitialSnapshotCommand
    ) -> YandexMailInitialSnapshotResult:
        return await self.coordinator.run(command)


def create_yandex_mail_initial_snapshot_operation(
    *,
    sync: EmailSyncCoordinator,
    admitted_reader: YandexMailAdmittedSnapshotReader,
    folder_bindings: YandexMailFolderBindingRepository,
    context_factory: Callable[
        [PersistenceOperation, UUID], TrustedPersistenceContext
    ],
    ingestion: ConnectorIngestionOrchestrator,
    raw_messages: YandexMailRawMessageAdmission,
) -> YandexMailInitialSnapshotOperation:
    """Wire admitted IMAP reads and canonical effects into the durable use case."""

    reader = YandexMailInitialSnapshotAdapter(
        reader=admitted_reader,
        bindings=folder_bindings,
        context_factory=context_factory,
    )
    messages = YandexMailCanonicalSnapshotMessageAdapter(
        ingestion=ingestion,
        raw_messages=raw_messages,
    )
    return YandexMailInitialSnapshotOperation(
        coordinator=YandexMailInitialSnapshotCoordinator(
            sync=sync,
            reader=reader,
            messages=messages,
        )
    )


__all__ = [
    "YandexMailInitialSnapshotOperation",
    "create_yandex_mail_initial_snapshot_operation",
]
