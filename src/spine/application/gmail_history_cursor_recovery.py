"""Recover a stale Gmail history cursor through a bounded no-gap rescan."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, model_validator

from spine.application.email_sync import (
    EmailSyncAdmission,
    EmailSyncAdmissionState,
    EmailSyncAttempt,
    EmailSyncConcurrentUpdateError,
    EmailSyncCoordinator,
    EmailSyncStatus,
)
from spine.application.gmail_initial_snapshot import (
    GmailInitialSnapshotCommand,
    GmailInitialSnapshotResult,
)
from spine.domain.common import DefinitionModel
from spine.domain.connections.email_scope import EmailOperation, MailboxReadScope


class GmailHistoryCursorRecoveryState(str, Enum):
    RECONCILING = "reconciling"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    SUSPENDED = "suspended"


class GmailHistoryCursorRecoveryCommand(DefinitionModel):
    """A bounded recovery request for one stale, generation-bound cursor."""

    schema_version: Literal[
        "gmail-history-cursor-recovery-command:v1"
    ] = "gmail-history-cursor-recovery-command:v1"
    scope: MailboxReadScope
    stale_attempt_id: UUID
    stale_checkpoint: str = Field(min_length=1, max_length=4096, repr=False)

    @model_validator(mode="after")
    def validate_recovery(self) -> "GmailHistoryCursorRecoveryCommand":
        required = (
            EmailOperation.ENUMERATE_MAILBOX,
            EmailOperation.READ_RAW_MESSAGE,
            EmailOperation.SYNCHRONIZE,
        )
        if (
            self.scope.provider_kind != "google"
            or any(not self.scope.allows(operation) for operation in required)
            or len(self.scope.allowed_label_references) != 1
            or self.scope.excluded_label_references
            or self.stale_attempt_id.int == 0
        ):
            raise ValueError(
                "Gmail history recovery requires one exact approved label and operations"
            )
        return self


class GmailHistoryCursorRecoveryProfile(DefinitionModel):
    """Finite limits and policy for one complete recovery rescan."""

    schema_version: Literal[
        "gmail-history-cursor-recovery-profile:v1"
    ] = "gmail-history-cursor-recovery-profile:v1"
    snapshot_page_size: int = Field(ge=1, le=500)
    max_snapshot_pages: int = Field(ge=1, le=10_000)
    history_page_size: int = Field(ge=1, le=500)
    max_history_pages: int = Field(ge=1, le=10_000)
    max_message_bytes: int = Field(ge=1, le=268_435_456)
    access_policy_version_id: UUID

    @model_validator(mode="after")
    def validate_policy(self) -> "GmailHistoryCursorRecoveryProfile":
        if self.access_policy_version_id.int == 0:
            raise ValueError("Gmail recovery policy must be non-zero")
        return self


class GmailHistoryCursorRecoveryReceipt(DefinitionModel):
    schema_version: Literal[
        "gmail-history-cursor-recovery-receipt:v1"
    ] = "gmail-history-cursor-recovery-receipt:v1"
    recovery_id: UUID
    stale_attempt_id: UUID
    reconciliation_attempt_id: UUID | None = None
    stale_checkpoint: str = Field(min_length=1, max_length=4096, repr=False)
    state: GmailHistoryCursorRecoveryState


class GmailHistoryCursorRecoveryPort(Protocol):
    async def run(
        self, command: GmailInitialSnapshotCommand
    ) -> GmailInitialSnapshotResult: ...


@dataclass(frozen=True, slots=True)
class GmailHistoryCursorRecoveryCoordinator:
    """Fence a stale cursor before starting a durable no-gap full reconciliation."""

    sync: EmailSyncCoordinator
    reconciliation: GmailHistoryCursorRecoveryPort
    profile: GmailHistoryCursorRecoveryProfile

    async def reconcile(
        self, command: GmailHistoryCursorRecoveryCommand
    ) -> GmailHistoryCursorRecoveryReceipt:
        scope = command.scope
        recovery_id = uuid5(
            NAMESPACE_URL,
            "spine:gmail-history-cursor-recovery:v1:"
            f"{scope.workspace_id}:{scope.environment_id}:{scope.connection_id}:"
            f"{scope.connection_version_id}:{scope.mailbox_binding_id}:"
            f"{command.stale_attempt_id}:{command.stale_checkpoint}",
        )
        fenced = await self._fence(command)
        if fenced.status is not EmailSyncStatus.RECONCILIATION_REQUIRED:
            return self._receipt(
                command,
                recovery_id=recovery_id,
                reconciliation_attempt_id=None,
                state=self._state(fenced.status),
            )
        if (
            await self.sync.admission_state(
                fenced.attempt_id,
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
            )
            is not EmailSyncAdmissionState.ALLOWED
        ):
            return self._receipt(
                command,
                recovery_id=recovery_id,
                reconciliation_attempt_id=None,
                state=GmailHistoryCursorRecoveryState.INCOMPLETE,
            )

        attempt_key = f"gmail-history-recovery.{recovery_id.hex}"
        snapshot_command = GmailInitialSnapshotCommand(
            scope=scope,
            attempt_key=attempt_key,
            initial_checkpoint=command.stale_checkpoint,
            snapshot_page_size=self.profile.snapshot_page_size,
            max_snapshot_pages=self.profile.max_snapshot_pages,
            history_page_size=self.profile.history_page_size,
            max_history_pages=self.profile.max_history_pages,
            max_message_bytes=self.profile.max_message_bytes,
            access_policy_version_id=self.profile.access_policy_version_id,
        )
        try:
            result = await self.reconciliation.run(snapshot_command)
            successor = result.attempt
        except EmailSyncConcurrentUpdateError:
            successor = await self.sync.create_attempt(
                scope,
                attempt_key=attempt_key,
                initial_checkpoint=command.stale_checkpoint,
            )
        return self._receipt(
            command,
            recovery_id=recovery_id,
            reconciliation_attempt_id=successor.attempt_id,
            state=self._state(successor.status),
        )

    async def _fence(
        self, command: GmailHistoryCursorRecoveryCommand
    ) -> EmailSyncAttempt:
        scope = command.scope
        detected = await self.sync.get_attempt(
            command.stale_attempt_id,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
        )
        if detected.admission != EmailSyncAdmission.from_scope(scope):
            raise KeyError("email sync attempt not found")
        if detected.committed_checkpoint != command.stale_checkpoint:
            raise ValueError("Gmail recovery checkpoint does not match the stale attempt")
        if (
            detected.status is not EmailSyncStatus.RECONCILIATION_REQUIRED
            or detected.failure_code != "gmail_history_cursor_stale"
        ):
            raise ValueError(
                "Gmail recovery requires a stale history cursor reconciliation hold"
            )
        return await self.sync.fence_for_reconciliation(
            command.stale_attempt_id,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            expected_admission=EmailSyncAdmission.from_scope(scope),
            restore_committed_checkpoint=detected.committed_checkpoint,
            failure_code="gmail_history_cursor_stale",
        )

    @staticmethod
    def _receipt(
        command: GmailHistoryCursorRecoveryCommand,
        *,
        recovery_id: UUID,
        reconciliation_attempt_id: UUID | None,
        state: GmailHistoryCursorRecoveryState,
    ) -> GmailHistoryCursorRecoveryReceipt:
        return GmailHistoryCursorRecoveryReceipt(
            recovery_id=recovery_id,
            stale_attempt_id=command.stale_attempt_id,
            reconciliation_attempt_id=reconciliation_attempt_id,
            stale_checkpoint=command.stale_checkpoint,
            state=state,
        )

    @staticmethod
    def _state(status: EmailSyncStatus) -> GmailHistoryCursorRecoveryState:
        if status is EmailSyncStatus.COMPLETE:
            return GmailHistoryCursorRecoveryState.COMPLETE
        if status in {EmailSyncStatus.PENDING, EmailSyncStatus.RUNNING}:
            return GmailHistoryCursorRecoveryState.RECONCILING
        if status is EmailSyncStatus.SUSPENDED:
            return GmailHistoryCursorRecoveryState.SUSPENDED
        return GmailHistoryCursorRecoveryState.INCOMPLETE


__all__ = [
    "GmailHistoryCursorRecoveryCommand",
    "GmailHistoryCursorRecoveryCoordinator",
    "GmailHistoryCursorRecoveryPort",
    "GmailHistoryCursorRecoveryProfile",
    "GmailHistoryCursorRecoveryReceipt",
    "GmailHistoryCursorRecoveryState",
]
