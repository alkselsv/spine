"""Authorized recovery from an IMAP UIDVALIDITY generation reset."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, StrictInt, model_validator

from spine.application.email_sync import (
    EmailSyncAdmission,
    EmailSyncAttempt,
    EmailSyncConcurrentUpdateError,
    EmailSyncCoordinator,
    EmailSyncStatus,
)
from spine.application.yandex_mail_snapshot import (
    YandexMailInitialSnapshotCommand,
    YandexMailInitialSnapshotResult,
    YandexMailRequiredFolderGeneration,
)
from spine.domain.common import DefinitionModel
from spine.domain.connections.email_scope import EmailOperation, MailboxReadScope


class YandexMailUidValidityResetState(str, Enum):
    RECONCILING = "reconciling"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    SUSPENDED = "suspended"


class YandexMailUidValidityResetCommand(DefinitionModel):
    """A bounded reconciliation request from one qualified changed generation."""

    schema_version: Literal["yandex-mail-uidvalidity-reset-command:v1"] = (
        "yandex-mail-uidvalidity-reset-command:v1"
    )
    scope: MailboxReadScope
    stale_attempt_id: UUID
    folder_reference: UUID
    stale_uid_validity: StrictInt = Field(ge=1, le=2**32 - 1)
    observed_uid_validity: StrictInt = Field(ge=1, le=2**32 - 1)

    @model_validator(mode="after")
    def validate_reset(self) -> "YandexMailUidValidityResetCommand":
        required = (
            EmailOperation.ENUMERATE_MAILBOX,
            EmailOperation.OBSERVE_FOLDER_GENERATION,
            EmailOperation.ENUMERATE_FOLDER_UIDS,
            EmailOperation.READ_RAW_MESSAGE,
            EmailOperation.SYNCHRONIZE,
        )
        if (
            self.scope.provider_kind != "yandex_mail"
            or not self.scope.allows_location(self.folder_reference)
            or any(not self.scope.allows(operation) for operation in required)
            or self.stale_attempt_id.int == 0
            or self.stale_uid_validity == self.observed_uid_validity
        ):
            raise ValueError("UIDVALIDITY reset is not an authorized changed generation")
        return self


class YandexMailUidValidityResetProfile(DefinitionModel):
    """Approved finite bounds and policy for complete reset reconciliation."""

    schema_version: Literal["yandex-mail-uidvalidity-reset-profile:v1"] = (
        "yandex-mail-uidvalidity-reset-profile:v1"
    )
    uid_page_size: int = Field(ge=1, le=5000)
    uid_page_response_bytes: int = Field(ge=1024, le=8_388_608)
    max_uid_pages_per_folder: int = Field(ge=1, le=10_000)
    max_message_bytes: int = Field(ge=1, le=268_435_456)
    access_policy_version_id: UUID

    @model_validator(mode="after")
    def validate_policy(self) -> "YandexMailUidValidityResetProfile":
        if self.access_policy_version_id.int == 0:
            raise ValueError("reset reconciliation policy must be non-zero")
        return self


class YandexMailUidValidityResetReceipt(DefinitionModel):
    schema_version: Literal["yandex-mail-uidvalidity-reset-receipt:v1"] = (
        "yandex-mail-uidvalidity-reset-receipt:v1"
    )
    reset_id: UUID
    stale_attempt_id: UUID
    reconciliation_attempt_id: UUID | None = None
    folder_reference: UUID
    stale_uid_validity: StrictInt = Field(ge=1, le=2**32 - 1)
    observed_uid_validity: StrictInt = Field(ge=1, le=2**32 - 1)
    state: YandexMailUidValidityResetState


class YandexMailReconciliationPort(Protocol):
    async def run(
        self, command: YandexMailInitialSnapshotCommand
    ) -> YandexMailInitialSnapshotResult: ...


class YandexMailUidValidityResetPort(Protocol):
    async def reconcile(
        self, command: YandexMailUidValidityResetCommand
    ) -> YandexMailUidValidityResetReceipt: ...


@dataclass(frozen=True, slots=True)
class YandexMailUidValidityResetCoordinator:
    """Fence an old UID generation before bounded complete rediscovery."""

    sync: EmailSyncCoordinator
    reconciliation: YandexMailReconciliationPort
    profile: YandexMailUidValidityResetProfile

    async def reconcile(
        self, command: YandexMailUidValidityResetCommand
    ) -> YandexMailUidValidityResetReceipt:
        scope = command.scope
        reset_id = uuid5(
            NAMESPACE_URL,
            "yandex-mail-uidvalidity-reset:"
            f"{scope.workspace_id}:{scope.environment_id}:{scope.connection_id}:"
            f"{scope.connection_version_id}:{scope.mailbox_binding_id}:"
            f"{command.folder_reference}:{command.stale_uid_validity}:"
            f"{command.observed_uid_validity}:{command.stale_attempt_id}",
        )
        fenced = await self._fence(command)
        if fenced.status is not EmailSyncStatus.RECONCILIATION_REQUIRED:
            return self._receipt(
                command,
                reset_id=reset_id,
                reconciliation_attempt_id=None,
                state=YandexMailUidValidityResetState.SUSPENDED,
            )

        attempt_key = f"uidvalidity-reset.{reset_id.hex}"
        snapshot_command = YandexMailInitialSnapshotCommand(
            scope=scope,
            attempt_key=attempt_key,
            uid_page_size=self.profile.uid_page_size,
            uid_page_response_bytes=self.profile.uid_page_response_bytes,
            max_uid_pages_per_folder=self.profile.max_uid_pages_per_folder,
            max_message_bytes=self.profile.max_message_bytes,
            access_policy_version_id=self.profile.access_policy_version_id,
            required_folder_generations=(
                YandexMailRequiredFolderGeneration(
                    folder_reference=command.folder_reference,
                    uid_validity=command.observed_uid_validity,
                ),
            ),
        )
        try:
            result = await self.reconciliation.run(snapshot_command)
            successor = result.attempt
        except EmailSyncConcurrentUpdateError:
            # A duplicate signal may race the same deterministic successor. Its
            # winner owns progress; this delivery returns the durable state.
            successor = await self.sync.create_attempt(
                scope,
                attempt_key=attempt_key,
            )
        return self._receipt(
            command,
            reset_id=reset_id,
            reconciliation_attempt_id=successor.attempt_id,
            state=self._state(successor.status),
        )

    async def _fence(
        self, command: YandexMailUidValidityResetCommand
    ) -> EmailSyncAttempt:
        scope = command.scope
        detected = await self.sync.get_attempt(
            command.stale_attempt_id,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
        )
        return await self.sync.fence_for_reconciliation(
            command.stale_attempt_id,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            expected_admission=EmailSyncAdmission.from_scope(scope),
            restore_committed_checkpoint=detected.committed_checkpoint,
            failure_code="uidvalidity_changed",
        )

    @staticmethod
    def _receipt(
        command: YandexMailUidValidityResetCommand,
        *,
        reset_id: UUID,
        reconciliation_attempt_id: UUID | None,
        state: YandexMailUidValidityResetState,
    ) -> YandexMailUidValidityResetReceipt:
        return YandexMailUidValidityResetReceipt(
            reset_id=reset_id,
            stale_attempt_id=command.stale_attempt_id,
            reconciliation_attempt_id=reconciliation_attempt_id,
            folder_reference=command.folder_reference,
            stale_uid_validity=command.stale_uid_validity,
            observed_uid_validity=command.observed_uid_validity,
            state=state,
        )

    @staticmethod
    def _state(status: EmailSyncStatus) -> YandexMailUidValidityResetState:
        if status is EmailSyncStatus.COMPLETE:
            return YandexMailUidValidityResetState.COMPLETE
        if status in {EmailSyncStatus.PENDING, EmailSyncStatus.RUNNING}:
            return YandexMailUidValidityResetState.RECONCILING
        if status is EmailSyncStatus.SUSPENDED:
            return YandexMailUidValidityResetState.SUSPENDED
        return YandexMailUidValidityResetState.INCOMPLETE


__all__ = [
    "YandexMailUidValidityResetCommand",
    "YandexMailUidValidityResetCoordinator",
    "YandexMailUidValidityResetPort",
    "YandexMailUidValidityResetProfile",
    "YandexMailUidValidityResetReceipt",
    "YandexMailUidValidityResetState",
]
