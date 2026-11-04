from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from spine.application.email_sync import (
    EmailSyncAdmission,
    EmailSyncAdmissionState,
    EmailSyncAttempt,
    EmailSyncCoordinator,
    EmailSyncEffect,
    EmailSyncEffectReceipt,
    EmailSyncPage,
    EmailSyncStatus,
    InMemoryEmailSyncRepository,
)
from spine.application.yandex_mail_snapshot import (
    YandexMailInitialSnapshotCommand,
    YandexMailInitialSnapshotCoordinator,
    YandexMailInitialSnapshotResult,
    YandexMailRequiredFolderGeneration,
    YandexMailSnapshotFolderCode,
    YandexMailSnapshotFolderDiscovery,
    YandexMailSnapshotFolderGeneration,
    YandexMailSnapshotGenerationCode,
    YandexMailSnapshotMessageCommand,
    YandexMailSnapshotMessageMetadata,
    YandexMailSnapshotMessageReceipt,
    YandexMailSnapshotUidPage,
    YandexMailSnapshotUidPageCode,
    YandexMailSnapshotUidPageRequest,
)
from spine.application.yandex_mail_uidvalidity_reset import (
    YandexMailUidValidityResetCommand,
    YandexMailUidValidityResetCoordinator,
    YandexMailUidValidityResetProfile,
    YandexMailUidValidityResetState,
)
from spine.domain.connections.email_scope import (
    EmailProviderOperation,
    MailboxReadScope,
)
from spine.domain.sources.imap import ImapMessageKey, imap_source_identity


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


FOLDER_ID = _id(11)


def _scope() -> MailboxReadScope:
    return MailboxReadScope(
        workspace_id=_id(1),
        environment_id=_id(2),
        connection_id=_id(3),
        connection_version_id=_id(4),
        provider_kind="yandex_mail",
        resource_scope_reference=_id(5),
        mailbox_subject_reference=_id(6),
        mailbox_binding_id=_id(7),
        authorization_generation=8,
        configuration_generation=9,
        scope_generation=10,
        allowed_folder_references=(FOLDER_ID,),
        allowed_operations=tuple(EmailProviderOperation),
    )


class _Admission:
    def __init__(self) -> None:
        self.state = EmailSyncAdmissionState.ALLOWED

    async def check(self, admission: EmailSyncAdmission) -> EmailSyncAdmissionState:
        del admission
        return self.state


class _Effects:
    def __init__(self) -> None:
        self.applied: list[str] = []

    async def apply(self, effect: EmailSyncEffect) -> EmailSyncEffectReceipt:
        self.applied.append(effect.effect_key)
        return EmailSyncEffectReceipt(
            effect_key=effect.effect_key,
            receipt_reference=f"receipt.{effect.effect_key}",
            durable=True,
        )


class _StaleWorkerRaceRepository(InMemoryEmailSyncRepository):
    def __init__(self) -> None:
        super().__init__()
        self.race_pending = True

    async def fence_for_reconciliation(
        self, attempt: EmailSyncAttempt, *, failure_code: str
    ) -> EmailSyncAttempt:
        if self.race_pending:
            self.race_pending = False
            await super().save(
                attempt.model_copy(
                    update={
                        "status": EmailSyncStatus.RUNNING,
                        "committed_checkpoint": "stale-cursor",
                        "failure_code": None,
                    }
                )
            )
        return await super().fence_for_reconciliation(
            attempt,
            failure_code=failure_code,
        )


class _Reconciliation:
    def __init__(self, sync: EmailSyncCoordinator) -> None:
        self.sync = sync
        self.commands: list[YandexMailInitialSnapshotCommand] = []

    async def run(
        self, command: YandexMailInitialSnapshotCommand
    ) -> YandexMailInitialSnapshotResult:
        self.commands.append(command)
        attempt = await self.sync.create_attempt(
            command.scope, attempt_key=command.attempt_key
        )
        return YandexMailInitialSnapshotResult(
            attempt=attempt,
            completed_folders=0,
            completed_uid_pages=0,
            durable_message_effects=0,
        )


class _SnapshotReader:
    def __init__(self, uid_validity: int) -> None:
        self.uid_validity = uid_validity

    async def discover_folders(
        self, scope: MailboxReadScope
    ) -> YandexMailSnapshotFolderDiscovery:
        return YandexMailSnapshotFolderDiscovery(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            complete=True,
            code=YandexMailSnapshotFolderCode.COMPLETE,
            discovered_count=1,
            approved_count=1,
            folders=(FOLDER_ID,),
        )

    async def observe_folder_generation(
        self, scope: MailboxReadScope, folder_reference: UUID
    ) -> YandexMailSnapshotFolderGeneration:
        return YandexMailSnapshotFolderGeneration(
            complete=True,
            code=YandexMailSnapshotGenerationCode.COMPLETE,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            connection_id=scope.connection_id,
            connection_version_id=scope.connection_version_id,
            mailbox_binding_id=scope.mailbox_binding_id,
            folder_reference=folder_reference,
            authorization_generation=scope.authorization_generation,
            configuration_generation=scope.configuration_generation,
            scope_generation=scope.scope_generation,
            uid_validity=self.uid_validity,
            uid_next=2,
            read_only=True,
        )

    async def enumerate_uid_page(
        self, request: YandexMailSnapshotUidPageRequest
    ) -> YandexMailSnapshotUidPage:
        return YandexMailSnapshotUidPage(
            complete=True,
            code=YandexMailSnapshotUidPageCode.COMPLETE,
            workspace_id=request.scope.workspace_id,
            environment_id=request.scope.environment_id,
            connection_id=request.scope.connection_id,
            connection_version_id=request.scope.connection_version_id,
            mailbox_binding_id=request.scope.mailbox_binding_id,
            folder_reference=request.folder_reference,
            authorization_generation=request.scope.authorization_generation,
            configuration_generation=request.scope.configuration_generation,
            scope_generation=request.scope.scope_generation,
            uid_validity=self.uid_validity,
            covered_uid_range=(1, 1),
            messages=(YandexMailSnapshotMessageMetadata(uid=1, size_octets=100),),
        )


class _CanonicalMessages:
    def __init__(self) -> None:
        self.commands: list[YandexMailSnapshotMessageCommand] = []

    async def apply(
        self, command: YandexMailSnapshotMessageCommand
    ) -> YandexMailSnapshotMessageReceipt:
        self.commands.append(command)
        return YandexMailSnapshotMessageReceipt(
            effect_key=command.effect_key,
            workspace_id=command.scope.workspace_id,
            environment_id=command.scope.environment_id,
            canonical_acceptance_id=uuid5(NAMESPACE_URL, command.effect_key),
            receipt_reference=f"receipt.{command.effect_key}",
        )


def _real_coordinator(
    *, reader_uid_validity: int
) -> tuple[YandexMailUidValidityResetCoordinator, EmailSyncCoordinator, _CanonicalMessages]:
    sync = EmailSyncCoordinator(
        repository=InMemoryEmailSyncRepository(),
        admission=_Admission(),
        effects=_Effects(),
        clock=lambda: datetime(2026, 11, 4, tzinfo=UTC),
    )
    messages = _CanonicalMessages()
    snapshot = YandexMailInitialSnapshotCoordinator(
        sync=sync,
        reader=_SnapshotReader(reader_uid_validity),
        messages=messages,
    )
    return (
        YandexMailUidValidityResetCoordinator(
            sync=sync,
            reconciliation=snapshot,
            profile=_profile(),
        ),
        sync,
        messages,
    )


def _command(
    *, stale_attempt_id: UUID, observed_uid_validity: int = 102
) -> YandexMailUidValidityResetCommand:
    return YandexMailUidValidityResetCommand(
        scope=_scope(),
        stale_attempt_id=stale_attempt_id,
        folder_reference=FOLDER_ID,
        stale_uid_validity=101,
        observed_uid_validity=observed_uid_validity,
    )


def _profile() -> YandexMailUidValidityResetProfile:
    return YandexMailUidValidityResetProfile(
        uid_page_size=10,
        uid_page_response_bytes=4096,
        max_uid_pages_per_folder=2,
        max_message_bytes=4096,
        access_policy_version_id=_id(12),
    )


@pytest.mark.asyncio
async def test_changed_uidvalidity_fences_old_attempt_and_starts_one_new_generation() -> None:
    scope = _scope()
    sync = EmailSyncCoordinator(
        repository=InMemoryEmailSyncRepository(),
        admission=_Admission(),
        effects=_Effects(),
        clock=lambda: datetime(2026, 11, 4, tzinfo=UTC),
    )
    stale = await sync.create_attempt(scope, attempt_key="incremental-folder-11")
    reconciliation = _Reconciliation(sync)
    coordinator = YandexMailUidValidityResetCoordinator(
        sync=sync,
        reconciliation=reconciliation,
        profile=_profile(),
    )
    command = _command(stale_attempt_id=stale.attempt_id)

    first = await coordinator.reconcile(command)
    replay = await coordinator.reconcile(command)
    fenced = await sync.get_attempt(
        stale.attempt_id,
        workspace_id=scope.workspace_id,
        environment_id=scope.environment_id,
    )

    assert first.state is YandexMailUidValidityResetState.RECONCILING
    assert replay.reset_id == first.reset_id
    assert replay.reconciliation_attempt_id == first.reconciliation_attempt_id
    assert first.reconciliation_attempt_id != stale.attempt_id
    assert fenced.status is EmailSyncStatus.RECONCILIATION_REQUIRED
    assert fenced.failure_code == "uidvalidity_changed"
    assert len({command.attempt_key for command in reconciliation.commands}) == 1
    assert all(
        command.required_folder_generations
        == (
            YandexMailRequiredFolderGeneration(
                folder_reference=FOLDER_ID,
                uid_validity=102,
            ),
        )
        for command in reconciliation.commands
    )


@pytest.mark.asyncio
async def test_stale_worker_race_is_fenced_without_advancing_cursor() -> None:
    scope = _scope()
    fence_time = datetime(2026, 11, 4, 12, 30, tzinfo=UTC)
    repository = _StaleWorkerRaceRepository()
    sync = EmailSyncCoordinator(
        repository=repository,
        admission=_Admission(),
        effects=_Effects(),
        clock=lambda: fence_time,
    )
    stale = await sync.create_attempt(scope, attempt_key="reset-cas-conflict")
    reconciliation = _Reconciliation(sync)
    coordinator = YandexMailUidValidityResetCoordinator(
        sync=sync,
        reconciliation=reconciliation,
        profile=_profile(),
    )

    result = await coordinator.reconcile(
        _command(stale_attempt_id=stale.attempt_id)
    )
    fenced = await sync.get_attempt(
        stale.attempt_id,
        workspace_id=scope.workspace_id,
        environment_id=scope.environment_id,
    )

    assert result.state is YandexMailUidValidityResetState.RECONCILING
    assert fenced.status is EmailSyncStatus.RECONCILIATION_REQUIRED
    assert fenced.committed_checkpoint is None
    assert fenced.updated_at == fence_time
    assert fenced.failure_code == "uidvalidity_changed"
    assert len(reconciliation.commands) == 1


@pytest.mark.asyncio
async def test_stale_generation_read_is_rejected_before_new_effects() -> None:
    coordinator, sync, messages = _real_coordinator(reader_uid_validity=101)
    stale = await sync.create_attempt(
        _scope(), attempt_key="incremental-stale-generation"
    )

    result = await coordinator.reconcile(
        _command(stale_attempt_id=stale.attempt_id)
    )
    reconciliation = await sync.get_attempt(
        result.reconciliation_attempt_id,
        workspace_id=_scope().workspace_id,
        environment_id=_scope().environment_id,
    )

    assert result.state is YandexMailUidValidityResetState.INCOMPLETE
    assert reconciliation.status is EmailSyncStatus.RECONCILIATION_REQUIRED
    assert reconciliation.failure_code == "folder_generation_changed"
    assert messages.commands == []


@pytest.mark.asyncio
async def test_reused_uid_in_new_generation_has_distinct_source_identity() -> None:
    coordinator, sync, messages = _real_coordinator(reader_uid_validity=102)
    scope = _scope()
    stale = await sync.create_attempt(scope, attempt_key="incremental-reused-uid")

    result = await coordinator.reconcile(
        _command(stale_attempt_id=stale.attempt_id)
    )

    assert result.state is YandexMailUidValidityResetState.COMPLETE
    assert len(messages.commands) == 1
    assert messages.commands[0].uid == 1
    assert messages.commands[0].uid_validity == 102
    old_identity = imap_source_identity(
        ImapMessageKey(
            workspace_id=scope.workspace_id,
            connection_id=scope.connection_id,
            mailbox_binding_id=scope.mailbox_binding_id,
            folder_binding_id=FOLDER_ID,
            uid_validity=101,
            uid=1,
        )
    )
    new_identity = imap_source_identity(
        old_identity.provider_key.model_copy(update={"uid_validity": 102})
    )
    assert new_identity.external_object_id != old_identity.external_object_id
    assert new_identity.external_generation == "imap-uidvalidity-v1:102"


def _page(
    scope: MailboxReadScope,
    *,
    page_id: str,
    requested_checkpoint: str | None,
    next_checkpoint: str,
) -> EmailSyncPage:
    return EmailSyncPage(
        workspace_id=scope.workspace_id,
        environment_id=scope.environment_id,
        page_id=page_id,
        requested_checkpoint=requested_checkpoint,
        next_checkpoint=next_checkpoint,
        effects=(
            EmailSyncEffect(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                effect_key=f"effect.{page_id}",
                effect_reference=uuid5(NAMESPACE_URL, page_id),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_reset_before_stale_page_rejects_effect_and_cursor() -> None:
    scope = _scope()
    effects = _Effects()
    sync = EmailSyncCoordinator(
        repository=InMemoryEmailSyncRepository(),
        admission=_Admission(),
        effects=effects,
    )
    stale = await sync.create_attempt(scope, attempt_key="reset-before-page")
    reset = YandexMailUidValidityResetCoordinator(
        sync=sync,
        reconciliation=_Reconciliation(sync),
        profile=_profile(),
    )

    await reset.reconcile(_command(stale_attempt_id=stale.attempt_id))
    rejected = await sync.apply_page(
        stale.attempt_id,
        _page(
            scope,
            page_id="stale-page",
            requested_checkpoint=None,
            next_checkpoint="stale-cursor",
        ),
        workspace_id=scope.workspace_id,
        environment_id=scope.environment_id,
    )

    assert rejected.status is EmailSyncStatus.RECONCILIATION_REQUIRED
    assert rejected.committed_checkpoint is None
    assert effects.applied == []


@pytest.mark.asyncio
async def test_reset_after_page_preserves_history_but_never_advances_stale_cursor() -> None:
    scope = _scope()
    effects = _Effects()
    sync = EmailSyncCoordinator(
        repository=InMemoryEmailSyncRepository(),
        admission=_Admission(),
        effects=effects,
    )
    stale = await sync.create_attempt(scope, attempt_key="reset-after-page")
    accepted = await sync.apply_page(
        stale.attempt_id,
        _page(
            scope,
            page_id="historical-page",
            requested_checkpoint=None,
            next_checkpoint="historical-cursor",
        ),
        workspace_id=scope.workspace_id,
        environment_id=scope.environment_id,
    )
    reset = YandexMailUidValidityResetCoordinator(
        sync=sync,
        reconciliation=_Reconciliation(sync),
        profile=_profile(),
    )

    await reset.reconcile(_command(stale_attempt_id=stale.attempt_id))
    rejected = await sync.apply_page(
        stale.attempt_id,
        _page(
            scope,
            page_id="post-reset-page",
            requested_checkpoint=accepted.committed_checkpoint,
            next_checkpoint="must-not-advance",
        ),
        workspace_id=scope.workspace_id,
        environment_id=scope.environment_id,
    )

    assert rejected.status is EmailSyncStatus.RECONCILIATION_REQUIRED
    assert rejected.committed_checkpoint == "historical-cursor"
    assert effects.applied == ["effect.historical-page"]


@pytest.mark.asyncio
async def test_repeated_reset_rechecks_current_authority_before_reconciliation() -> None:
    scope = _scope()
    admission = _Admission()
    sync = EmailSyncCoordinator(
        repository=InMemoryEmailSyncRepository(),
        admission=admission,
        effects=_Effects(),
    )
    stale = await sync.create_attempt(scope, attempt_key="reset-authority-replay")
    reconciliation = _Reconciliation(sync)
    reset = YandexMailUidValidityResetCoordinator(
        sync=sync,
        reconciliation=reconciliation,
        profile=_profile(),
    )
    command = _command(stale_attempt_id=stale.attempt_id)

    first = await reset.reconcile(command)
    admission.state = EmailSyncAdmissionState.REVOKED
    revoked = await reset.reconcile(command)

    assert first.state is YandexMailUidValidityResetState.RECONCILING
    assert revoked.state is YandexMailUidValidityResetState.SUSPENDED
    assert len(reconciliation.commands) == 1


@pytest.mark.asyncio
async def test_reset_attempt_is_isolated_between_workspaces_without_disclosure() -> None:
    scope = _scope()
    sync = EmailSyncCoordinator(
        repository=InMemoryEmailSyncRepository(),
        admission=_Admission(),
        effects=_Effects(),
    )
    stale = await sync.create_attempt(
        scope,
        attempt_key="protected-reset-attempt",
    )
    reconciliation = _Reconciliation(sync)
    reset = YandexMailUidValidityResetCoordinator(
        sync=sync,
        reconciliation=reconciliation,
        profile=_profile(),
    )
    other_scope = scope.model_copy(
        update={
            "workspace_id": _id(98),
            "environment_id": _id(99),
        }
    )

    with pytest.raises(KeyError) as error:
        await reset.reconcile(
            _command(stale_attempt_id=stale.attempt_id).model_copy(
                update={"scope": other_scope}
            )
        )

    assert "protected-reset-attempt" not in str(error.value)
    assert reconciliation.commands == []
    current = await sync.get_attempt(
        stale.attempt_id,
        workspace_id=scope.workspace_id,
        environment_id=scope.environment_id,
    )
    assert current.status is EmailSyncStatus.PENDING
