from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from spine.application.email_sync import (
    EmailSyncAdmission,
    EmailSyncAdmissionState,
    EmailSyncCoordinator,
    EmailSyncEffect,
    EmailSyncPageKind,
    EmailSyncStatus,
    InMemoryEmailSyncRepository,
)
from spine.application.yandex_mail_snapshot import (
    YandexMailInitialSnapshotCommand,
    YandexMailInitialSnapshotCoordinator,
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
from spine.domain.connections.email_scope import (
    EmailProviderOperation,
    MailboxReadScope,
)


WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
ENVIRONMENT_ID = UUID("20000000-0000-0000-0000-000000000002")
CONNECTION_ID = UUID("30000000-0000-0000-0000-000000000003")
CONNECTION_VERSION_ID = UUID("40000000-0000-0000-0000-000000000004")
SCOPE_ID = UUID("50000000-0000-0000-0000-000000000005")
SUBJECT_ID = UUID("60000000-0000-0000-0000-000000000006")
MAILBOX_ID = UUID("70000000-0000-0000-0000-000000000007")
INBOX_ID = UUID("80000000-0000-0000-0000-000000000008")
ARCHIVE_ID = UUID("90000000-0000-0000-0000-000000000009")
POLICY_ID = UUID("a0000000-0000-0000-0000-00000000000a")


def scope() -> MailboxReadScope:
    return MailboxReadScope(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        connection_id=CONNECTION_ID,
        connection_version_id=CONNECTION_VERSION_ID,
        provider_kind="yandex_mail",
        resource_scope_reference=SCOPE_ID,
        mailbox_subject_reference=SUBJECT_ID,
        mailbox_binding_id=MAILBOX_ID,
        authorization_generation=3,
        configuration_generation=4,
        scope_generation=5,
        allowed_folder_references=(INBOX_ID, ARCHIVE_ID),
        allowed_operations=tuple(EmailProviderOperation),
    )


def command() -> YandexMailInitialSnapshotCommand:
    return YandexMailInitialSnapshotCommand(
        scope=scope(),
        attempt_key="issue-849-initial",
        uid_page_size=2,
        uid_page_response_bytes=4096,
        max_uid_pages_per_folder=4,
        max_message_bytes=8192,
        access_policy_version_id=POLICY_ID,
    )


class Admission:
    def __init__(self, *, stale_after: int | None = None) -> None:
        self.calls = 0
        self.stale_after = stale_after

    async def check(self, admission: EmailSyncAdmission) -> EmailSyncAdmissionState:
        del admission
        self.calls += 1
        if self.stale_after is not None and self.calls > self.stale_after:
            return EmailSyncAdmissionState.STALE_GENERATION
        return EmailSyncAdmissionState.ALLOWED


class UnusedEffects:
    async def apply(self, effect: EmailSyncEffect):
        del effect
        raise AssertionError("snapshot receipts must already be canonical and durable")


class CrashAfterDurableSave(InMemoryEmailSyncRepository):
    def __init__(self, boundary: str) -> None:
        super().__init__()
        self.boundary = boundary
        self.crashed = False

    async def save(self, attempt):
        saved = await super().save(attempt)
        if self.crashed:
            return saved
        pages = attempt.pages
        reached = {
            "folder": any(
                page.committed
                and page.page_kind is EmailSyncPageKind.FOLDER_GENERATION
                for page in pages
            ),
            "page": any(
                page.committed and page.page_kind is EmailSyncPageKind.UID_PAGE
                for page in pages
            ),
            "checkpoint": attempt.status is EmailSyncStatus.COMPLETE,
        }[self.boundary]
        if reached:
            self.crashed = True
            raise RuntimeError(f"synthetic crash after {self.boundary}")
        return saved


class ScriptedReader:
    def __init__(
        self,
        *,
        folder_uids: dict[UUID, tuple[int, ...]] | None = None,
        discovery_complete: bool = True,
    ) -> None:
        self.folder_uids = folder_uids or {INBOX_ID: (1, 2, 3), ARCHIVE_ID: ()}
        self.discovery_complete = discovery_complete
        self.uid_validities = {INBOX_ID: 101, ARCHIVE_ID: 202}
        self.fail_page_once: YandexMailSnapshotUidPageCode | None = None
        self.discovery_calls = 0
        self.generation_calls: list[UUID] = []
        self.page_calls: list[tuple[UUID, int]] = []

    async def discover_folders(
        self, mailbox_scope: MailboxReadScope
    ) -> YandexMailSnapshotFolderDiscovery:
        self.discovery_calls += 1
        if not self.discovery_complete:
            return YandexMailSnapshotFolderDiscovery(
                workspace_id=mailbox_scope.workspace_id,
                environment_id=mailbox_scope.environment_id,
                complete=False,
                code=YandexMailSnapshotFolderCode.PARTIAL_RESPONSE,
                discovered_count=1,
                approved_count=1,
            )
        return YandexMailSnapshotFolderDiscovery(
            workspace_id=mailbox_scope.workspace_id,
            environment_id=mailbox_scope.environment_id,
            complete=True,
            code=YandexMailSnapshotFolderCode.COMPLETE,
            discovered_count=len(self.folder_uids),
            approved_count=len(self.folder_uids),
            folders=tuple(sorted(self.folder_uids, key=lambda value: value.int)),
        )

    async def observe_folder_generation(
        self, mailbox_scope: MailboxReadScope, folder_reference: UUID
    ) -> YandexMailSnapshotFolderGeneration:
        self.generation_calls.append(folder_reference)
        uids = self.folder_uids[folder_reference]
        return YandexMailSnapshotFolderGeneration(
            complete=True,
            code=YandexMailSnapshotGenerationCode.COMPLETE,
            workspace_id=mailbox_scope.workspace_id,
            environment_id=mailbox_scope.environment_id,
            connection_id=mailbox_scope.connection_id,
            connection_version_id=mailbox_scope.connection_version_id,
            mailbox_binding_id=mailbox_scope.mailbox_binding_id,
            folder_reference=folder_reference,
            authorization_generation=mailbox_scope.authorization_generation,
            configuration_generation=mailbox_scope.configuration_generation,
            scope_generation=mailbox_scope.scope_generation,
            uid_validity=self.uid_validities[folder_reference],
            uid_next=max(uids, default=0) + 1,
            read_only=True,
        )

    async def enumerate_uid_page(
        self, request: YandexMailSnapshotUidPageRequest
    ) -> YandexMailSnapshotUidPage:
        self.page_calls.append((request.folder_reference, request.start_uid))
        code = self.fail_page_once
        if code is not None:
            self.fail_page_once = None
            return self._page(request, complete=False, code=code, messages=())
        messages = tuple(
            YandexMailSnapshotMessageMetadata(uid=uid, size_octets=100 + uid)
            for uid in self.folder_uids[request.folder_reference]
            if request.start_uid <= uid <= request.end_uid
        )
        return self._page(
            request,
            complete=True,
            code=YandexMailSnapshotUidPageCode.COMPLETE,
            messages=messages,
        )

    @staticmethod
    def _page(
        request: YandexMailSnapshotUidPageRequest,
        *,
        complete: bool,
        code: YandexMailSnapshotUidPageCode,
        messages: tuple[YandexMailSnapshotMessageMetadata, ...],
    ) -> YandexMailSnapshotUidPage:
        mailbox_scope = request.scope
        return YandexMailSnapshotUidPage(
            complete=complete,
            code=code,
            workspace_id=mailbox_scope.workspace_id,
            environment_id=mailbox_scope.environment_id,
            connection_id=mailbox_scope.connection_id,
            connection_version_id=mailbox_scope.connection_version_id,
            mailbox_binding_id=mailbox_scope.mailbox_binding_id,
            folder_reference=request.folder_reference,
            authorization_generation=mailbox_scope.authorization_generation,
            configuration_generation=mailbox_scope.configuration_generation,
            scope_generation=mailbox_scope.scope_generation,
            uid_validity=(
                request.uid_validity + 1
                if code is YandexMailSnapshotUidPageCode.UIDVALIDITY_CHANGED
                else request.uid_validity
            ),
            covered_uid_range=(request.start_uid, request.end_uid) if complete else None,
            messages=messages,
            continuation_uid=request.continuation_uid if complete else None,
        )


class CanonicalMessages:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_once = False
        self.receipts: dict[str, YandexMailSnapshotMessageReceipt] = {}

    async def apply(
        self, message: YandexMailSnapshotMessageCommand
    ) -> YandexMailSnapshotMessageReceipt:
        self.calls.append(message.effect_key)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("synthetic canonical boundary interruption")
        return self.receipts.setdefault(
            message.effect_key,
            YandexMailSnapshotMessageReceipt(
                effect_key=message.effect_key,
                workspace_id=message.scope.workspace_id,
                environment_id=message.scope.environment_id,
                canonical_acceptance_id=uuid5(NAMESPACE_URL, message.effect_key),
                receipt_reference=str(uuid5(NAMESPACE_URL, message.effect_key)),
            ),
        )


def coordinator(
    reader: ScriptedReader,
    messages: CanonicalMessages,
    *,
    admission: Admission | None = None,
    repository: InMemoryEmailSyncRepository | None = None,
) -> YandexMailInitialSnapshotCoordinator:
    sync = EmailSyncCoordinator(
        repository=repository or InMemoryEmailSyncRepository(),
        admission=admission or Admission(),
        effects=UnusedEffects(),
    )
    return YandexMailInitialSnapshotCoordinator(
        sync=sync,
        reader=reader,
        messages=messages,
    )


@pytest.mark.asyncio
async def test_multi_folder_snapshot_including_empty_folder_publishes_one_terminal_checkpoint() -> None:
    reader = ScriptedReader()
    messages = CanonicalMessages()

    service = coordinator(reader, messages)
    result = await service.run(command())
    replay = await service.run(command())

    assert result.attempt.status is EmailSyncStatus.COMPLETE
    assert result.completed_folders == 2
    assert result.completed_uid_pages == 3
    assert result.durable_message_effects == 3
    assert result.attempt.committed_checkpoint is not None
    assert result.attempt.committed_checkpoint.startswith("imap-snapshot:v1:")
    terminal_pages = [page for page in result.attempt.pages if page.terminal]
    assert len(terminal_pages) == 1
    assert all(page.committed for page in result.attempt.pages)
    assert len(reader.page_calls) == 3
    assert replay.attempt == result.attempt
    assert reader.discovery_calls == 1


@pytest.mark.asyncio
async def test_partial_folder_discovery_keeps_attempt_incomplete_without_effects() -> None:
    reader = ScriptedReader(discovery_complete=False)
    messages = CanonicalMessages()

    result = await coordinator(reader, messages).run(command())

    assert result.attempt.status is EmailSyncStatus.INCOMPLETE
    assert result.attempt.committed_checkpoint is None
    assert result.attempt.pages == ()
    assert messages.calls == []


@pytest.mark.asyncio
async def test_nominally_complete_subset_cannot_publish_missing_approved_folder() -> None:
    reader = ScriptedReader(folder_uids={INBOX_ID: (1,)})
    messages = CanonicalMessages()

    result = await coordinator(reader, messages).run(command())

    assert result.attempt.status is EmailSyncStatus.INCOMPLETE
    assert result.attempt.failure_code == "folder_coverage_inconsistent"
    assert result.attempt.committed_checkpoint is None
    assert messages.calls == []


@pytest.mark.asyncio
async def test_cross_workspace_discovery_is_rejected_before_manifest_commit() -> None:
    class WrongWorkspaceReader(ScriptedReader):
        async def discover_folders(self, mailbox_scope):
            discovery = await super().discover_folders(mailbox_scope)
            return discovery.model_copy(
                update={
                    "workspace_id": UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
                }
            )

    result = await coordinator(
        WrongWorkspaceReader(), CanonicalMessages()
    ).run(command())

    assert result.attempt.status is EmailSyncStatus.INCOMPLETE
    assert result.attempt.failure_code == "folder_coverage_inconsistent"
    assert result.attempt.pages == ()


@pytest.mark.asyncio
async def test_cross_workspace_message_receipt_is_rejected_before_page_commit() -> None:
    reader = ScriptedReader(folder_uids={INBOX_ID: (1,), ARCHIVE_ID: ()})

    class WrongWorkspaceMessages(CanonicalMessages):
        async def apply(self, message):
            receipt = await super().apply(message)
            return receipt.model_copy(
                update={
                    "workspace_id": UUID(
                        "ffffffff-ffff-ffff-ffff-ffffffffffff"
                    )
                }
            )

    result = await coordinator(reader, WrongWorkspaceMessages()).run(command())

    assert result.attempt.status is EmailSyncStatus.INCOMPLETE
    assert result.attempt.failure_code == "message_effect_incomplete"
    assert result.attempt.committed_checkpoint is None
    assert not any(
        page.page_kind is EmailSyncPageKind.UID_PAGE
        for page in result.attempt.pages
    )


@pytest.mark.asyncio
async def test_reconnect_failure_replays_stable_generation_and_message_effects() -> None:
    reader = ScriptedReader(folder_uids={INBOX_ID: (1,), ARCHIVE_ID: ()})
    reader.fail_page_once = YandexMailSnapshotUidPageCode.SERVER_REJECTED
    messages = CanonicalMessages()
    service = coordinator(reader, messages)

    interrupted = await service.run(command())
    recovered = await service.run(command())

    assert interrupted.attempt.status is EmailSyncStatus.INCOMPLETE
    assert interrupted.attempt.committed_checkpoint is None
    assert recovered.attempt.status is EmailSyncStatus.COMPLETE
    assert reader.generation_calls.count(INBOX_ID) == 1
    assert len(messages.receipts) == 1


@pytest.mark.asyncio
async def test_uidvalidity_reset_fences_attempt_without_terminal_checkpoint() -> None:
    reader = ScriptedReader(folder_uids={INBOX_ID: (1,), ARCHIVE_ID: ()})
    reader.fail_page_once = YandexMailSnapshotUidPageCode.UIDVALIDITY_CHANGED

    result = await coordinator(reader, CanonicalMessages()).run(command())

    assert result.attempt.status is EmailSyncStatus.RECONCILIATION_REQUIRED
    assert result.attempt.failure_code == "folder_generation_changed"
    assert result.attempt.committed_checkpoint is None
    assert not any(page.terminal for page in result.attempt.pages)


@pytest.mark.asyncio
async def test_crash_at_message_boundary_retries_one_stable_canonical_effect() -> None:
    reader = ScriptedReader(folder_uids={INBOX_ID: (1,), ARCHIVE_ID: ()})
    messages = CanonicalMessages()
    messages.fail_once = True
    service = coordinator(reader, messages)

    interrupted = await service.run(command())
    recovered = await service.run(command())

    assert interrupted.attempt.status is EmailSyncStatus.INCOMPLETE
    assert recovered.attempt.status is EmailSyncStatus.COMPLETE
    assert messages.calls[0] == messages.calls[1]
    assert len(messages.receipts) == 1
    assert recovered.durable_message_effects == 1


@pytest.mark.asyncio
async def test_stale_scope_generation_fences_aggregate_publication() -> None:
    reader = ScriptedReader(folder_uids={INBOX_ID: (1,), ARCHIVE_ID: ()})
    messages = CanonicalMessages()
    admission = Admission(stale_after=4)

    result = await coordinator(reader, messages, admission=admission).run(command())

    assert result.attempt.status is EmailSyncStatus.RECONCILIATION_REQUIRED
    assert result.attempt.committed_checkpoint is None
    assert not any(page.terminal and page.committed for page in result.attempt.pages)
    assert len(messages.receipts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ("folder", "page", "checkpoint"))
async def test_crash_at_durable_boundary_replays_to_one_terminal_checkpoint(
    boundary: str,
) -> None:
    reader = ScriptedReader(folder_uids={INBOX_ID: (1,), ARCHIVE_ID: ()})
    messages = CanonicalMessages()
    repository = CrashAfterDurableSave(boundary)
    service = coordinator(reader, messages, repository=repository)

    with pytest.raises(RuntimeError, match=f"after {boundary}"):
        await service.run(command())
    recovered = await service.run(command())

    assert recovered.attempt.status is EmailSyncStatus.COMPLETE
    assert recovered.attempt.committed_checkpoint is not None
    assert sum(page.terminal for page in recovered.attempt.pages) == 1
    assert len(messages.receipts) == 1
