from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
import hashlib
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from spine.application.email_sync import (
    EmailSyncAdmission,
    EmailSyncAdmissionState,
    EmailSyncCoordinator,
    EmailSyncEffect,
    EmailSyncEffectReceipt,
    EmailSyncPageKind,
    EmailSyncStatus,
    InMemoryEmailSyncRepository,
)
from spine.application.gmail_initial_snapshot import (
    GmailHistoryBaseline,
    GmailHistoryBaselineState,
    GmailInitialSnapshotCommand,
    GmailInitialSnapshotCoordinator,
    GmailSnapshotMessageCommand,
    GmailSnapshotMessageReceipt,
    GmailSnapshotPage,
    GmailSnapshotPageRequest,
    gmail_snapshot_effect_key,
)
from spine.connectors.gmail_checkpoints import (
    decode_gmail_history_checkpoint,
    encode_gmail_history_checkpoint,
    encode_gmail_snapshot_page_checkpoint,
)
from spine.connectors.gmail_history import (
    GmailHistoryChange,
    GmailHistoryChangeKind,
    GmailHistoryResult,
    GmailHistoryState,
    gmail_history_scope_digest,
)
from spine.connectors.gmail_incremental import GmailIncrementalHistoryApplier
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
LABEL_ID = UUID("80000000-0000-0000-0000-000000000008")
POLICY_ID = UUID("90000000-0000-0000-0000-000000000009")


def scope() -> MailboxReadScope:
    return MailboxReadScope(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        connection_id=CONNECTION_ID,
        connection_version_id=CONNECTION_VERSION_ID,
        provider_kind="google",
        resource_scope_reference=SCOPE_ID,
        mailbox_subject_reference=SUBJECT_ID,
        mailbox_binding_id=MAILBOX_ID,
        authorization_generation=3,
        configuration_generation=4,
        scope_generation=5,
        allowed_label_references=(LABEL_ID,),
        allowed_operations=tuple(EmailProviderOperation),
    )


def command() -> GmailInitialSnapshotCommand:
    return GmailInitialSnapshotCommand(
        scope=scope(),
        attempt_key="issue-838-initial",
        snapshot_page_size=2,
        max_snapshot_pages=3,
        history_page_size=100,
        max_history_pages=3,
        max_message_bytes=8192,
        access_policy_version_id=POLICY_ID,
    )


def test_command_rejects_multi_label_scope_without_complete_coverage_plan() -> None:
    with pytest.raises(ValueError, match="one exact label"):
        GmailInitialSnapshotCommand.model_validate(
            {
                **command().model_dump(),
                "scope": scope().model_copy(
                    update={"allowed_label_references": (LABEL_ID, POLICY_ID)}
                ),
            }
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


class HistoryEffects:
    def __init__(self) -> None:
        self.calls: list[EmailSyncEffect] = []
        self.receipts: dict[str, EmailSyncEffectReceipt] = {}

    async def apply(self, effect: EmailSyncEffect) -> EmailSyncEffectReceipt:
        self.calls.append(effect)
        return self.receipts.setdefault(
            effect.effect_key,
            EmailSyncEffectReceipt(
                effect_key=effect.effect_key,
                receipt_reference=f"history.{effect.effect_reference.hex}",
                durable=True,
            ),
        )


class HistoryEffectFactory:
    async def prepare(
        self, mailbox_scope: MailboxReadScope, change: GmailHistoryChange
    ) -> EmailSyncEffect:
        identity = f"{mailbox_scope.mailbox_binding_id}:{change.event_id}"
        digest = hashlib.sha256(identity.encode()).hexdigest()
        return EmailSyncEffect(
            workspace_id=mailbox_scope.workspace_id,
            environment_id=mailbox_scope.environment_id,
            effect_key=f"gmail.history.{digest}",
            effect_reference=uuid5(NAMESPACE_URL, identity),
        )


class ScriptedReader:
    def __init__(self) -> None:
        second_page = encode_gmail_snapshot_page_checkpoint(scope(), "page-2")
        self.baseline = GmailHistoryBaseline(
            state=GmailHistoryBaselineState.CAPTURED,
            admission_scope_digest=gmail_history_scope_digest(scope()),
            checkpoint=encode_gmail_history_checkpoint(scope(), "100"),
        )
        self.pages = {
            None: GmailSnapshotPage(
                requested_checkpoint=None,
                message_ids=("message-a", "message-b"),
                next_checkpoint=second_page,
            ),
            second_page: GmailSnapshotPage(
                requested_checkpoint=second_page,
                message_ids=("message-c", "message-d"),
                next_checkpoint=None,
            ),
        }
        self.history = GmailHistoryResult(
            requested_start_history_id="100",
            admission_scope_digest=gmail_history_scope_digest(scope()),
            state=GmailHistoryState.COMPLETE,
            terminal_history_id="106",
            pages_read=2,
            duplicate_count=0,
            malformed_count=0,
            changes=(
                GmailHistoryChange(
                    history_id="101",
                    kind=GmailHistoryChangeKind.MESSAGE_ADDED,
                    message_id="message-d",
                ),
                GmailHistoryChange(
                    history_id="103",
                    kind=GmailHistoryChangeKind.MESSAGE_DELETED,
                    message_id="message-b",
                ),
                GmailHistoryChange(
                    history_id="105",
                    kind=GmailHistoryChangeKind.LABEL_ADDED,
                    message_id="message-a",
                    label_id="STARRED",
                ),
            ),
        )
        self.baseline_calls = 0
        self.snapshot_calls: list[str | None] = []
        self.history_calls: list[str] = []

    async def capture_history_baseline(
        self, mailbox_scope: MailboxReadScope
    ) -> GmailHistoryBaseline:
        del mailbox_scope
        self.baseline_calls += 1
        return self.baseline

    async def enumerate_snapshot_page(
        self, request: GmailSnapshotPageRequest
    ) -> GmailSnapshotPage:
        self.snapshot_calls.append(request.checkpoint)
        return self.pages[request.checkpoint]


class HistoryDrain:
    def __init__(
        self,
        reader: ScriptedReader,
        applier: GmailIncrementalHistoryApplier,
    ) -> None:
        self.reader = reader
        self.applier = applier

    async def stage(
        self,
        attempt_id: UUID,
        mailbox_scope: MailboxReadScope,
        *,
        baseline_checkpoint: str,
        max_results: int,
        max_pages: int,
        covered_snapshot_effect_keys: tuple[str, ...],
    ):
        del max_results, max_pages
        start_history_id = decode_gmail_history_checkpoint(
            mailbox_scope, baseline_checkpoint
        )
        self.reader.history_calls.append(start_history_id)
        covered = set(covered_snapshot_effect_keys)
        filtered = self.reader.history.model_copy(
            update={
                "changes": tuple(
                    change
                    for change in self.reader.history.changes
                    if not (
                        change.kind is GmailHistoryChangeKind.MESSAGE_ADDED
                        and gmail_snapshot_effect_key(mailbox_scope, change.message_id)
                        in covered
                    )
                )
            }
        )
        return await self.applier.stage(
            attempt_id, filtered, start_history_id=start_history_id
        )


class CanonicalMessages:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.receipts: dict[str, GmailSnapshotMessageReceipt] = {}

    async def apply(
        self, message: GmailSnapshotMessageCommand
    ) -> GmailSnapshotMessageReceipt:
        self.calls.append(message.message_id)
        return self.receipts.setdefault(
            message.message_id,
            GmailSnapshotMessageReceipt(
                effect_key=message.effect_key,
                workspace_id=message.scope.workspace_id,
                environment_id=message.scope.environment_id,
                canonical_acceptance_id=uuid5(NAMESPACE_URL, message.message_id),
                receipt_reference=f"canonical.{uuid5(NAMESPACE_URL, message.message_id).hex}",
            ),
        )


class SyntheticProcessCrash(BaseException):
    pass


class CrashAfterDurableSave(InMemoryEmailSyncRepository):
    def __init__(self, boundary: str) -> None:
        super().__init__()
        self.boundary = boundary
        self.crashed = False

    async def save(self, attempt):
        saved = await super().save(attempt)
        if self.crashed:
            return saved
        reached = {
            "baseline": any(
                page.committed and page.page_kind is EmailSyncPageKind.HISTORY_BASELINE
                for page in attempt.pages
            ),
            "snapshot_page": any(
                page.committed and page.page_kind is EmailSyncPageKind.SNAPSHOT_PAGE
                for page in attempt.pages
            ),
            "accepted_effect": any(page.effect_receipts for page in attempt.pages),
            "history_drain": any(
                page.committed and page.page_kind is EmailSyncPageKind.HISTORY_DRAIN
                for page in attempt.pages
            ),
            "cursor_commit": attempt.status is EmailSyncStatus.COMPLETE,
        }[self.boundary]
        if reached:
            self.crashed = True
            raise SyntheticProcessCrash(f"synthetic crash after {self.boundary}")
        return saved


def service(
    reader: ScriptedReader,
    messages: CanonicalMessages,
    *,
    repository: InMemoryEmailSyncRepository | None = None,
    admission: Admission | None = None,
) -> GmailInitialSnapshotCoordinator:
    effects = HistoryEffects()
    sync = EmailSyncCoordinator(
        repository=repository or InMemoryEmailSyncRepository(),
        admission=admission or Admission(),
        effects=effects,
        clock=lambda: datetime(2026, 11, 4, tzinfo=UTC),
    )
    applier = GmailIncrementalHistoryApplier(sync, scope(), HistoryEffectFactory())
    return GmailInitialSnapshotCoordinator(
        sync=sync,
        reader=reader,
        messages=messages,
        history=HistoryDrain(reader, applier),
    )


@pytest.mark.asyncio
async def test_captured_baseline_snapshot_and_history_drain_publish_one_terminal_cursor() -> (
    None
):
    reader = ScriptedReader()
    messages = CanonicalMessages()
    coordinator = service(reader, messages)

    result = await coordinator.run(command())
    replay = await coordinator.run(command())

    assert result.attempt.status is EmailSyncStatus.COMPLETE
    assert (
        decode_gmail_history_checkpoint(scope(), result.attempt.committed_checkpoint)
        == "106"
    )
    assert [page.page_kind for page in result.attempt.pages] == [
        EmailSyncPageKind.HISTORY_BASELINE,
        EmailSyncPageKind.SNAPSHOT_PAGE,
        EmailSyncPageKind.SNAPSHOT_PAGE,
        EmailSyncPageKind.HISTORY_DRAIN,
        EmailSyncPageKind.AGGREGATE_CHECKPOINT,
    ]
    assert all(page.committed for page in result.attempt.pages)
    assert result.snapshot_messages == 4
    assert result.history_effects == 2
    assert messages.calls == ["message-a", "message-b", "message-c", "message-d"]
    assert replay.attempt == result.attempt
    assert reader.baseline_calls == 1
    assert reader.snapshot_calls == [
        None,
        encode_gmail_snapshot_page_checkpoint(scope(), "page-2"),
    ]
    assert reader.history_calls == ["100"]


@pytest.mark.asyncio
async def test_concurrent_add_delete_and_label_change_are_each_applied_once() -> None:
    reader = ScriptedReader()
    messages = CanonicalMessages()
    coordinator = service(reader, messages)

    result = await coordinator.run(command())
    history_page = next(
        page
        for page in result.attempt.pages
        if page.page_kind is EmailSyncPageKind.HISTORY_DRAIN
    )

    assert result.attempt.status is EmailSyncStatus.COMPLETE
    assert len(history_page.effect_receipts) == 2
    assert len({receipt.effect_key for receipt in history_page.effect_receipts}) == 2
    assert Counter(messages.calls) == Counter(
        {"message-a": 1, "message-b": 1, "message-c": 1, "message-d": 1}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    (
        "baseline",
        "snapshot_page",
        "accepted_effect",
        "history_drain",
        "cursor_commit",
    ),
)
async def test_crash_after_durable_boundary_replays_without_cursor_gap(
    boundary: str,
) -> None:
    reader = ScriptedReader()
    messages = CanonicalMessages()
    repository = CrashAfterDurableSave(boundary)
    coordinator = service(reader, messages, repository=repository)

    with pytest.raises(SyntheticProcessCrash, match=f"after {boundary}"):
        await coordinator.run(command())
    interrupted = await coordinator.sync.create_attempt(
        scope(), attempt_key=command().attempt_key
    )
    if boundary != "cursor_commit":
        assert interrupted.committed_checkpoint is None
    recovered = await coordinator.run(command())

    assert recovered.attempt.status is EmailSyncStatus.COMPLETE
    assert (
        decode_gmail_history_checkpoint(scope(), recovered.attempt.committed_checkpoint)
        == "106"
    )
    assert len(messages.receipts) == 4
    assert sum(page.terminal for page in recovered.attempt.pages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe_stage", ("baseline", "snapshot", "history"))
async def test_unsafe_or_incomplete_coverage_requires_reconciliation_without_cursor(
    unsafe_stage: str,
) -> None:
    reader = ScriptedReader()
    if unsafe_stage == "baseline":
        reader.baseline = reader.baseline.model_copy(
            update={
                "state": GmailHistoryBaselineState.UNSAFE,
                "checkpoint": None,
                "error_code": "baseline_unavailable",
            }
        )
    elif unsafe_stage == "snapshot":
        second_page = encode_gmail_snapshot_page_checkpoint(scope(), "page-2")
        reader.pages[second_page] = reader.pages[second_page].model_copy(
            update={"complete": False, "error_code": "provider_unavailable"}
        )
    else:
        reader.history = reader.history.model_copy(
            update={
                "state": GmailHistoryState.INCOMPLETE,
                "terminal_history_id": None,
                "error_code": "page_limit_reached",
            }
        )

    result = await service(reader, CanonicalMessages()).run(command())

    assert result.attempt.status is EmailSyncStatus.RECONCILIATION_REQUIRED
    assert result.attempt.committed_checkpoint is None
    assert not any(page.terminal for page in result.attempt.pages)


@pytest.mark.asyncio
async def test_stale_generation_before_terminal_commit_requires_reconciliation() -> (
    None
):
    reader = ScriptedReader()
    messages = CanonicalMessages()

    result = await service(
        reader,
        messages,
        admission=Admission(stale_after=12),
    ).run(command())

    assert result.attempt.status is EmailSyncStatus.RECONCILIATION_REQUIRED
    assert result.attempt.committed_checkpoint is None
    assert not any(page.terminal and page.committed for page in result.attempt.pages)
