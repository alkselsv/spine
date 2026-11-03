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
    EmailSyncEffectKind,
    EmailSyncEffectReceipt,
    EmailSyncStatus,
    InMemoryEmailSyncRepository,
)
from spine.connectors.gmail_history import (
    GmailHistoryChange,
    GmailHistoryChangeKind,
    GmailHistoryResult,
    GmailHistoryState,
    gmail_history_scope_digest,
)
from spine.connectors.gmail_incremental import (
    GmailHistoryEffectFactory,
    GmailIncrementalHistoryApplier,
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
MAILBOX_BINDING_ID = UUID("70000000-0000-0000-0000-000000000007")


class Admission:
    def __init__(
        self, state: EmailSyncAdmissionState = EmailSyncAdmissionState.ALLOWED
    ):
        self.state = state

    async def check(self, admission: EmailSyncAdmission) -> EmailSyncAdmissionState:
        del admission
        return self.state


class Effects:
    def __init__(self) -> None:
        self.calls: list[EmailSyncEffect] = []
        self.receipts: dict[str, EmailSyncEffectReceipt] = {}
        self.fail_once_at: int | None = None

    async def apply(self, effect: EmailSyncEffect) -> EmailSyncEffectReceipt:
        self.calls.append(effect)
        if self.fail_once_at == len(self.calls):
            self.fail_once_at = None
            raise RuntimeError("synthetic item failure")
        return self.receipts.setdefault(
            effect.effect_key,
            EmailSyncEffectReceipt(
                effect_key=effect.effect_key,
                receipt_reference=f"receipt.{effect.effect_reference.hex}",
                durable=True,
            ),
        )


class EffectFactory:
    async def prepare(
        self,
        mailbox_scope: MailboxReadScope,
        change: GmailHistoryChange,
    ) -> EmailSyncEffect:
        identity = f"{mailbox_scope.mailbox_binding_id}:{change.event_id}"
        digest = hashlib.sha256(identity.encode()).hexdigest()
        return EmailSyncEffect(
            workspace_id=mailbox_scope.workspace_id,
            environment_id=mailbox_scope.environment_id,
            effect_key=f"gmail.history.{digest}",
            effect_reference=uuid5(NAMESPACE_URL, identity),
        )


class ProtectedFailureFactory:
    async def prepare(
        self,
        mailbox_scope: MailboxReadScope,
        change: GmailHistoryChange,
    ) -> EmailSyncEffect:
        del mailbox_scope, change
        raise RuntimeError(
            "token=protected subject=private sender@example.test raw message body"
        )


def scope() -> MailboxReadScope:
    return MailboxReadScope(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        connection_id=CONNECTION_ID,
        connection_version_id=CONNECTION_VERSION_ID,
        provider_kind="google",
        resource_scope_reference=SCOPE_ID,
        mailbox_subject_reference=SUBJECT_ID,
        mailbox_binding_id=MAILBOX_BINDING_ID,
        authorization_generation=3,
        configuration_generation=4,
        scope_generation=5,
        allowed_operations=(EmailProviderOperation.SYNCHRONIZE,),
    )


def history_result(
    *,
    state: GmailHistoryState = GmailHistoryState.COMPLETE,
    terminal_history_id: str | None = "103",
    error_code: str | None = None,
) -> GmailHistoryResult:
    return GmailHistoryResult(
        requested_start_history_id="98",
        admission_scope_digest=gmail_history_scope_digest(scope()),
        state=state,
        pages_read=2,
        duplicate_count=0,
        malformed_count=0,
        terminal_history_id=terminal_history_id,
        error_code=error_code,
        changes=(
            GmailHistoryChange(
                history_id="99",
                kind=GmailHistoryChangeKind.MESSAGE_ADDED,
                message_id="message-a",
            ),
            GmailHistoryChange(
                history_id="100",
                kind=GmailHistoryChangeKind.LABEL_ADDED,
                message_id="message-a",
                label_id="STARRED",
            ),
            GmailHistoryChange(
                history_id="101",
                kind=GmailHistoryChangeKind.LABEL_REMOVED,
                message_id="message-a",
                label_id="INBOX",
            ),
            GmailHistoryChange(
                history_id="102",
                kind=GmailHistoryChangeKind.MESSAGE_DELETED,
                message_id="message-b",
            ),
        ),
    )


def applier(
    *,
    admission: Admission | None = None,
    effects: Effects | None = None,
    factory: GmailHistoryEffectFactory | None = None,
) -> tuple[GmailIncrementalHistoryApplier, Admission, Effects]:
    admission = admission or Admission()
    effects = effects or Effects()
    coordinator = EmailSyncCoordinator(
        repository=InMemoryEmailSyncRepository(),
        admission=admission,
        effects=effects,
        clock=lambda: datetime(2026, 11, 3, tzinfo=UTC),
    )
    return (
        GmailIncrementalHistoryApplier(
            coordinator,
            scope(),
            factory or EffectFactory(),
        ),
        admission,
        effects,
    )


@pytest.mark.asyncio
async def test_multi_page_history_commits_terminal_cursor_after_all_effect_receipts() -> (
    None
):
    service, _, effects = applier()
    attempt = await service.start(attempt_key="gmail-history-1", start_history_id="98")

    result = await service.apply(
        attempt.attempt_id,
        history_result(),
        start_history_id="98",
    )

    assert result.status is EmailSyncStatus.COMPLETE
    assert result.committed_checkpoint == "103"
    assert result.pages[0].requested_checkpoint == "98"
    assert result.pages[0].next_checkpoint == "103"
    assert result.pages[0].committed is True
    assert len(result.pages[0].effect_receipts) == 4
    assert Counter(effect.kind for effect in effects.calls) == {
        EmailSyncEffectKind.CONTENT: 1,
        EmailSyncEffectKind.STATE: 3,
    }
    assert all(
        effect.state_observation is not None
        for effect in effects.calls
        if effect.kind is EmailSyncEffectKind.STATE
    )


@pytest.mark.asyncio
async def test_duplicate_history_delivery_reuses_receipts_and_logical_effects() -> None:
    service, _, effects = applier()
    attempt = await service.start(
        attempt_key="gmail-history-duplicate", start_history_id="98"
    )
    drained = history_result()

    first = await service.apply(attempt.attempt_id, drained, start_history_id="98")
    replay = await service.apply(attempt.attempt_id, drained, start_history_id="98")

    assert replay == first
    assert len(effects.calls) == 4
    assert len(effects.receipts) == 4


@pytest.mark.asyncio
async def test_partial_item_replay_keeps_cursor_and_skips_already_receipted_effects() -> (
    None
):
    effects = Effects()
    effects.fail_once_at = 2
    service, _, _ = applier(effects=effects)
    attempt = await service.start(
        attempt_key="gmail-history-retry", start_history_id="98"
    )
    drained = history_result()

    partial = await service.apply(attempt.attempt_id, drained, start_history_id="98")
    replay = await service.apply(attempt.attempt_id, drained, start_history_id="98")

    assert partial.status is EmailSyncStatus.INCOMPLETE
    assert partial.committed_checkpoint == "98"
    assert len(partial.pages[0].effect_receipts) == 1
    assert replay.status is EmailSyncStatus.COMPLETE
    assert replay.committed_checkpoint == "103"
    call_counts = Counter(effect.effect_key for effect in effects.calls)
    assert sorted(call_counts.values()) == [1, 1, 1, 2]


@pytest.mark.asyncio
async def test_item_failure_resumes_admitted_range_before_newer_provider_drain() -> None:
    effects = Effects()
    effects.fail_once_at = 2
    service, _, _ = applier(effects=effects)
    attempt = await service.start(
        attempt_key="gmail-history-moving-retry", start_history_id="98"
    )

    partial = await service.apply(
        attempt.attempt_id, history_result(), start_history_id="98"
    )
    newer = history_result().model_copy(
        update={
            "terminal_history_id": "105",
            "changes": (
                *history_result().changes,
                GmailHistoryChange(
                    history_id="104",
                    kind=GmailHistoryChangeKind.LABEL_ADDED,
                    message_id="message-c",
                    label_id="IMPORTANT",
                ),
            ),
        }
    )
    resumed = await service.apply(attempt.attempt_id, newer, start_history_id="98")

    assert partial.committed_checkpoint == "98"
    assert resumed.status is EmailSyncStatus.COMPLETE
    assert resumed.committed_checkpoint == "103"
    assert len(resumed.pages) == 1
    assert len(effects.receipts) == 4


@pytest.mark.asyncio
async def test_regressive_terminal_history_id_holds_checkpoint() -> None:
    service, _, effects = applier()
    attempt = await service.start(
        attempt_key="gmail-history-regressive", start_history_id="98"
    )

    held = await service.apply(
        attempt.attempt_id,
        GmailHistoryResult(
            requested_start_history_id="98",
            admission_scope_digest=gmail_history_scope_digest(scope()),
            state=GmailHistoryState.COMPLETE,
            terminal_history_id="97",
            pages_read=1,
            duplicate_count=0,
            malformed_count=0,
        ),
        start_history_id="98",
    )

    assert held.status is EmailSyncStatus.INCOMPLETE
    assert held.failure_code == "gmail_history_range_invalid"
    assert held.committed_checkpoint == "98"
    assert effects.calls == []


@pytest.mark.asyncio
async def test_result_for_different_start_cursor_holds_checkpoint() -> None:
    service, _, effects = applier()
    attempt = await service.start(
        attempt_key="gmail-history-wrong-range", start_history_id="98"
    )
    wrong_range = history_result().model_copy(
        update={"requested_start_history_id": "99"}
    )

    held = await service.apply(
        attempt.attempt_id,
        wrong_range,
        start_history_id="98",
    )

    assert held.status is EmailSyncStatus.INCOMPLETE
    assert held.failure_code == "gmail_history_range_invalid"
    assert held.committed_checkpoint == "98"
    assert effects.calls == []


@pytest.mark.asyncio
async def test_result_for_different_admission_scope_holds_checkpoint() -> None:
    service, _, effects = applier()
    attempt = await service.start(
        attempt_key="gmail-history-wrong-scope", start_history_id="98"
    )
    other_scope = scope().model_copy(
        update={
            "mailbox_binding_id": UUID("70000000-0000-0000-0000-000000000099"),
            "scope_generation": 6,
        }
    )
    wrong_scope = history_result().model_copy(
        update={"admission_scope_digest": gmail_history_scope_digest(other_scope)}
    )

    held = await service.apply(
        attempt.attempt_id,
        wrong_scope,
        start_history_id="98",
    )

    assert held.status is EmailSyncStatus.INCOMPLETE
    assert held.failure_code == "gmail_history_range_invalid"
    assert held.committed_checkpoint == "98"
    assert effects.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected_status", "expected_failure"),
    (
        (
            history_result(
                state=GmailHistoryState.INCOMPLETE,
                error_code="malformed_history_record",
            ),
            EmailSyncStatus.INCOMPLETE,
            "gmail_malformed_history_record",
        ),
        (
            history_result(terminal_history_id=None),
            EmailSyncStatus.INCOMPLETE,
            "gmail_history_incomplete",
        ),
        (
            history_result(
                state=GmailHistoryState.RECONCILIATION_REQUIRED,
                terminal_history_id=None,
                error_code="history_cursor_stale",
            ),
            EmailSyncStatus.RECONCILIATION_REQUIRED,
            "gmail_history_cursor_stale",
        ),
    ),
)
async def test_partial_or_stale_history_holds_committed_cursor(
    result: GmailHistoryResult,
    expected_status: EmailSyncStatus,
    expected_failure: str,
) -> None:
    service, _, effects = applier()
    attempt = await service.start(attempt_key=expected_failure, start_history_id="98")

    held = await service.apply(attempt.attempt_id, result, start_history_id="98")

    assert held.status is expected_status
    assert held.failure_code == expected_failure
    assert held.committed_checkpoint == "98"
    assert held.pages == ()
    assert effects.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("admission_state", "expected_status"),
    (
        (
            EmailSyncAdmissionState.STALE_GENERATION,
            EmailSyncStatus.RECONCILIATION_REQUIRED,
        ),
        (EmailSyncAdmissionState.REVOKED, EmailSyncStatus.SUSPENDED),
    ),
)
async def test_stale_or_revoked_authority_holds_history_checkpoint(
    admission_state: EmailSyncAdmissionState,
    expected_status: EmailSyncStatus,
) -> None:
    service, _, effects = applier(admission=Admission(admission_state))
    attempt = await service.start(
        attempt_key=f"gmail-history-{admission_state.value}",
        start_history_id="98",
    )

    result = await service.apply(
        attempt.attempt_id,
        history_result(),
        start_history_id="98",
    )

    assert result.status is expected_status
    assert result.committed_checkpoint == "98"
    assert effects.calls == []


@pytest.mark.asyncio
async def test_same_attempt_key_cannot_substitute_another_start_cursor() -> None:
    service, _, _ = applier()
    await service.start(attempt_key="gmail-history-conflict", start_history_id="98")

    with pytest.raises(ValueError, match="conflicts"):
        await service.start(attempt_key="gmail-history-conflict", start_history_id="99")


@pytest.mark.asyncio
async def test_effect_preparation_failure_holds_cursor_without_protected_output() -> (
    None
):
    service, _, effects = applier(factory=ProtectedFailureFactory())
    attempt = await service.start(
        attempt_key="gmail-history-safe-failure",
        start_history_id="98",
    )

    result = await service.apply(
        attempt.attempt_id,
        history_result(),
        start_history_id="98",
    )

    assert result.status is EmailSyncStatus.INCOMPLETE
    assert result.committed_checkpoint == "98"
    assert result.failure_code == "gmail_effect_preparation_failed"
    assert effects.calls == []
    for protected in ("protected", "private", "@example.test", "raw message"):
        assert protected not in repr(result)
