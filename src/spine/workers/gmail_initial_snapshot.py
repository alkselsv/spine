"""Production composition seam for one bounded Gmail initial snapshot."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from spine.application.connections.email_read import MailboxReadAdmission
from spine.application.connector_ingestion import ConnectorIngestionOrchestrator
from spine.application.email_sync import EmailSyncCoordinator
from spine.application.gmail_initial_snapshot import (
    GmailInitialSnapshotCommand,
    GmailInitialSnapshotCoordinator,
    GmailInitialSnapshotReader,
    GmailInitialSnapshotResult,
)
from spine.application.persistence.context import (
    PersistenceOperation,
    TrustedPersistenceContext,
)
from spine.connectors.gmail import GmailRawMessageResult
from spine.connectors.gmail_history import GmailHistoryResult
from spine.connectors.gmail_incremental import (
    GmailHistoryEffectFactory,
    GmailIncrementalHistoryApplier,
)
from spine.connectors.gmail_snapshot import (
    GmailCanonicalSnapshotMessageAdapter,
    GmailInitialHistoryDrainAdapter,
)
from spine.domain.connections.email_scope import MailboxReadScope


@dataclass(frozen=True, slots=True)
class GmailInitialSnapshotOperation:
    """Executable Gmail snapshot exposed by the worker composition root."""

    coordinator: GmailInitialSnapshotCoordinator
    scope: MailboxReadScope

    async def run(
        self, command: GmailInitialSnapshotCommand
    ) -> GmailInitialSnapshotResult:
        if command.scope != self.scope:
            raise ValueError("Gmail snapshot command is outside the composed scope")
        return await self.coordinator.run(command)


def create_gmail_initial_snapshot_operation(
    *,
    scope: MailboxReadScope,
    sync: EmailSyncCoordinator,
    snapshot_reader: GmailInitialSnapshotReader,
    history_reader: MailboxReadAdmission[GmailHistoryResult],
    history_effects: GmailHistoryEffectFactory,
    context_factory: Callable[[PersistenceOperation, UUID], TrustedPersistenceContext],
    ingestion: ConnectorIngestionOrchestrator,
    raw_messages: MailboxReadAdmission[GmailRawMessageResult],
    clock: Callable[[], datetime],
) -> GmailInitialSnapshotOperation:
    """Wire admitted Gmail reads and canonical effects into one durable use case."""

    effective_labels = tuple(
        label
        for label in scope.allowed_label_references
        if label not in scope.excluded_label_references
    )
    if len(effective_labels) != 1 or len(scope.allowed_label_references) != 1:
        raise ValueError("Gmail initial snapshot requires one exact label")
    history_applier = GmailIncrementalHistoryApplier(sync, scope, history_effects)
    return GmailInitialSnapshotOperation(
        scope=scope,
        coordinator=GmailInitialSnapshotCoordinator(
            sync=sync,
            reader=snapshot_reader,
            history=GmailInitialHistoryDrainAdapter(
                reader=history_reader,
                applier=history_applier,
                context_factory=context_factory,
            ),
            messages=GmailCanonicalSnapshotMessageAdapter(
                ingestion=ingestion,
                raw_messages=raw_messages,
                authorized_label_reference=effective_labels[0],
                clock=clock,
            ),
        ),
    )


__all__ = [
    "GmailInitialSnapshotOperation",
    "create_gmail_initial_snapshot_operation",
]
