from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from spine.application.email_sync import (
    EmailSyncAdmission,
    EmailSyncAdmissionState,
    EmailSyncCoordinator,
    EmailSyncEffect,
    EmailSyncEffectReceipt,
    EmailSyncStatus,
    InMemoryEmailSyncRepository,
)
from spine.application.gmail_history_cursor_recovery import (
    GmailHistoryCursorRecoveryCommand,
    GmailHistoryCursorRecoveryCoordinator,
    GmailHistoryCursorRecoveryProfile,
    GmailHistoryCursorRecoveryState,
)
from spine.application.gmail_initial_snapshot import (
    GmailInitialSnapshotCommand,
    GmailInitialSnapshotResult,
)
from spine.connectors.gmail_history import (
    GmailHistoryChange,
    GmailHistoryResult,
    GmailHistoryState,
    gmail_history_scope_digest,
)
from spine.connectors.gmail_incremental import (
    GmailHistoryEffectFactory,
    GmailIncrementalHistoryApplier,
)
from spine.domain.connections.email_scope import EmailProviderOperation, MailboxReadScope


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


def _scope() -> MailboxReadScope:
    return MailboxReadScope(
        workspace_id=_id(1),
        environment_id=_id(2),
        connection_id=_id(3),
        connection_version_id=_id(4),
        provider_kind="google",
        resource_scope_reference=_id(5),
        mailbox_subject_reference=_id(6),
        mailbox_binding_id=_id(7),
        authorization_generation=8,
        configuration_generation=9,
        scope_generation=10,
        allowed_label_references=(_id(11),),
        allowed_operations=tuple(EmailProviderOperation),
    )


class _Admission:
    def __init__(self) -> None:
        self.state = EmailSyncAdmissionState.ALLOWED

    async def check(self, admission: EmailSyncAdmission) -> EmailSyncAdmissionState:
        del admission
        return self.state


class _Effects:
    async def apply(self, effect: EmailSyncEffect) -> EmailSyncEffectReceipt:
        return EmailSyncEffectReceipt(
            effect_key=effect.effect_key,
            receipt_reference=f"receipt.{effect.effect_reference.hex}",
            durable=True,
        )


class _HistoryFactory(GmailHistoryEffectFactory):
    async def prepare(
        self, scope: MailboxReadScope, change: GmailHistoryChange
    ) -> EmailSyncEffect:
        del change
        return EmailSyncEffect(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            effect_key="gmail.history.synthetic",
            effect_reference=_id(12),
        )


class _Reconciliation:
    def __init__(self, sync: EmailSyncCoordinator) -> None:
        self.sync = sync
        self.commands: list[GmailInitialSnapshotCommand] = []

    async def run(
        self, command: GmailInitialSnapshotCommand
    ) -> GmailInitialSnapshotResult:
        self.commands.append(command)
        attempt = await self.sync.create_attempt(
            command.scope,
            attempt_key=command.attempt_key,
            initial_checkpoint=command.initial_checkpoint,
        )
        return GmailInitialSnapshotResult(
            attempt=attempt,
            completed_snapshot_pages=0,
            snapshot_messages=0,
            history_effects=0,
        )


def _sync(admission: _Admission | None = None) -> tuple[EmailSyncCoordinator, _Admission]:
    current = admission or _Admission()
    return (
        EmailSyncCoordinator(
            repository=InMemoryEmailSyncRepository(),
            admission=current,
            effects=_Effects(),
            clock=lambda: datetime(2026, 11, 5, tzinfo=UTC),
        ),
        current,
    )


def _profile() -> GmailHistoryCursorRecoveryProfile:
    return GmailHistoryCursorRecoveryProfile(
        snapshot_page_size=10,
        max_snapshot_pages=3,
        history_page_size=10,
        max_history_pages=3,
        max_message_bytes=4096,
        access_policy_version_id=_id(13),
    )


def _command(stale_attempt_id: UUID, checkpoint: str = "gmail-old-checkpoint"):
    return GmailHistoryCursorRecoveryCommand(
        scope=_scope(),
        stale_attempt_id=stale_attempt_id,
        stale_checkpoint=checkpoint,
    )


@pytest.mark.asyncio
async def test_history_404_preserves_checkpoint_and_requires_reconciliation() -> None:
    sync, _ = _sync()
    applier = GmailIncrementalHistoryApplier(sync, _scope(), _HistoryFactory())
    stale = await applier.start(attempt_key="gmail-incremental", start_history_id="98")

    result = await applier.apply(
        stale.attempt_id,
        GmailHistoryResult(
            requested_start_history_id="98",
            admission_scope_digest=gmail_history_scope_digest(_scope()),
            state=GmailHistoryState.RECONCILIATION_REQUIRED,
            pages_read=0,
            duplicate_count=0,
            malformed_count=0,
            error_code="history_cursor_stale",
        ),
        start_history_id="98",
    )

    assert result.status is EmailSyncStatus.RECONCILIATION_REQUIRED
    assert result.failure_code == "gmail_history_cursor_stale"
    assert result.committed_checkpoint == "98"
    assert result.pages == ()


@pytest.mark.asyncio
async def test_recovery_starts_one_idempotent_bounded_successor_from_old_checkpoint() -> None:
    sync, _ = _sync()
    stale = await sync.create_attempt(
        _scope(), attempt_key="gmail-incremental", initial_checkpoint="gmail-old-checkpoint"
    )
    await sync.require_reconciliation(
        stale.attempt_id,
        workspace_id=_scope().workspace_id,
        environment_id=_scope().environment_id,
        failure_code="gmail_history_cursor_stale",
    )
    reconciliation = _Reconciliation(sync)
    coordinator = GmailHistoryCursorRecoveryCoordinator(
        sync=sync, reconciliation=reconciliation, profile=_profile()
    )
    command = _command(stale.attempt_id)

    first = await coordinator.reconcile(command)
    replay = await coordinator.reconcile(command)
    fenced = await sync.get_attempt(
        stale.attempt_id,
        workspace_id=_scope().workspace_id,
        environment_id=_scope().environment_id,
    )
    successor = await sync.get_attempt(
        first.reconciliation_attempt_id,
        workspace_id=_scope().workspace_id,
        environment_id=_scope().environment_id,
    )

    assert first.state is GmailHistoryCursorRecoveryState.RECONCILING
    assert replay == first
    assert len(reconciliation.commands) == 2
    assert all(
        item.initial_checkpoint == "gmail-old-checkpoint"
        and item.attempt_key == reconciliation.commands[0].attempt_key
        for item in reconciliation.commands
    )
    assert fenced.status is EmailSyncStatus.RECONCILIATION_REQUIRED
    assert fenced.committed_checkpoint == "gmail-old-checkpoint"
    assert successor.committed_checkpoint == "gmail-old-checkpoint"


@pytest.mark.asyncio
async def test_recovery_does_not_start_after_authorization_revocation() -> None:
    admission = _Admission()
    sync, _ = _sync(admission)
    stale = await sync.create_attempt(
        _scope(), attempt_key="gmail-revoked", initial_checkpoint="old"
    )
    await sync.require_reconciliation(
        stale.attempt_id,
        workspace_id=_scope().workspace_id,
        environment_id=_scope().environment_id,
        failure_code="gmail_history_cursor_stale",
    )
    admission.state = EmailSyncAdmissionState.REVOKED
    reconciliation = _Reconciliation(sync)
    coordinator = GmailHistoryCursorRecoveryCoordinator(
        sync=sync, reconciliation=reconciliation, profile=_profile()
    )

    result = await coordinator.reconcile(_command(stale.attempt_id, "old"))

    assert result.state is GmailHistoryCursorRecoveryState.SUSPENDED
    assert result.reconciliation_attempt_id is None
    assert reconciliation.commands == []


@pytest.mark.asyncio
async def test_recovery_does_not_read_after_generation_change() -> None:
    admission = _Admission()
    sync, _ = _sync(admission)
    stale = await sync.create_attempt(
        _scope(), attempt_key="gmail-stale-generation", initial_checkpoint="old"
    )
    await sync.require_reconciliation(
        stale.attempt_id,
        workspace_id=_scope().workspace_id,
        environment_id=_scope().environment_id,
        failure_code="gmail_history_cursor_stale",
    )
    admission.state = EmailSyncAdmissionState.STALE_GENERATION
    reconciliation = _Reconciliation(sync)
    coordinator = GmailHistoryCursorRecoveryCoordinator(
        sync=sync, reconciliation=reconciliation, profile=_profile()
    )

    result = await coordinator.reconcile(_command(stale.attempt_id, "old"))

    assert result.state is GmailHistoryCursorRecoveryState.INCOMPLETE
    assert result.reconciliation_attempt_id is None
    assert reconciliation.commands == []


@pytest.mark.asyncio
async def test_recovery_rejects_a_checkpoint_from_another_attempt() -> None:
    sync, _ = _sync()
    stale = await sync.create_attempt(
        _scope(), attempt_key="gmail-checkpoint-mismatch", initial_checkpoint="actual"
    )
    coordinator = GmailHistoryCursorRecoveryCoordinator(
        sync=sync, reconciliation=_Reconciliation(sync), profile=_profile()
    )

    with pytest.raises(ValueError, match="does not match"):
        await coordinator.reconcile(_command(stale.attempt_id, "forged"))


@pytest.mark.asyncio
async def test_recovery_rejects_an_attempt_without_a_stale_cursor_hold() -> None:
    sync, _ = _sync()
    stale = await sync.create_attempt(
        _scope(), attempt_key="gmail-not-stale", initial_checkpoint="actual"
    )
    coordinator = GmailHistoryCursorRecoveryCoordinator(
        sync=sync, reconciliation=_Reconciliation(sync), profile=_profile()
    )

    with pytest.raises(ValueError, match="stale history cursor"):
        await coordinator.reconcile(_command(stale.attempt_id, "actual"))
