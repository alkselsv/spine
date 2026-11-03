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
    EmailSyncPage,
    EmailSyncStatus,
    InMemoryEmailSyncRepository,
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
EFFECT_ONE = UUID("80000000-0000-0000-0000-000000000008")
EFFECT_TWO = UUID("90000000-0000-0000-0000-000000000009")


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 11, 3, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


class FakeAdmission:
    def __init__(
        self,
        state: EmailSyncAdmissionState = EmailSyncAdmissionState.ALLOWED,
        *,
        states: tuple[EmailSyncAdmissionState, ...] = (),
    ):
        self.state = state
        self.states = list(states)
        self.calls: list[EmailSyncAdmission] = []

    async def check(self, admission: EmailSyncAdmission) -> EmailSyncAdmissionState:
        self.calls.append(admission)
        if self.states:
            return self.states.pop(0)
        return self.state


class FakeEffects:
    def __init__(self) -> None:
        self.receipts: dict[str, EmailSyncEffectReceipt] = {}
        self.calls: list[str] = []
        self.fail_keys: set[str] = set()
        self.nondurable_keys: set[str] = set()

    async def apply(self, effect: EmailSyncEffect) -> EmailSyncEffectReceipt:
        self.calls.append(effect.effect_key)
        if effect.effect_key in self.fail_keys:
            raise RuntimeError("synthetic effect failure")
        if effect.effect_key not in self.receipts:
            self.receipts[effect.effect_key] = EmailSyncEffectReceipt(
                effect_key=effect.effect_key,
                receipt_reference=f"receipt:{effect.effect_key}",
                durable=effect.effect_key not in self.nondurable_keys,
            )
        return self.receipts[effect.effect_key]


def mailbox_scope() -> MailboxReadScope:
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


def page(
    *,
    page_id: str = "page-one",
    requested_checkpoint: str | None = None,
    next_checkpoint: str | None = "opaque.cursor.v1:next",
    terminal: bool = False,
    effect_keys: tuple[str, ...] = ("effect-one",),
) -> EmailSyncPage:
    effects_by_key = {"effect-one": EFFECT_ONE, "effect-two": EFFECT_TWO}
    return EmailSyncPage(
        page_id=page_id,
        requested_checkpoint=requested_checkpoint,
        next_checkpoint=next_checkpoint,
        terminal=terminal,
        effects=tuple(
            EmailSyncEffect(effect_key=key, effect_reference=effects_by_key[key])
            for key in effect_keys
        ),
    )


def coordinator(
    *,
    admission_state: EmailSyncAdmissionState = EmailSyncAdmissionState.ALLOWED,
    effects: FakeEffects | None = None,
    clock: FakeClock | None = None,
) -> tuple[EmailSyncCoordinator, FakeAdmission, FakeEffects, InMemoryEmailSyncRepository]:
    effect_port = effects or FakeEffects()
    repository = InMemoryEmailSyncRepository()
    admission = FakeAdmission(admission_state)
    service = EmailSyncCoordinator(
        repository=repository,
        admission=admission,
        effects=effect_port,
        clock=clock or FakeClock(),
    )
    return service, admission, effect_port, repository


@pytest.mark.asyncio
async def test_pages_commit_opaque_checkpoints_after_durable_effects() -> None:
    service, _, effects, _ = coordinator()
    attempt = await service.create_attempt(mailbox_scope(), attempt_key="sync-1")

    first = await service.apply_page(attempt.attempt_id, page())
    second = await service.apply_page(
        attempt.attempt_id,
        page(
            page_id="page-two",
            requested_checkpoint="opaque.cursor.v1:next",
            next_checkpoint="provider-cursor-is-opaque",
            terminal=True,
            effect_keys=("effect-two",),
        ),
    )

    assert first.status is EmailSyncStatus.RUNNING
    assert first.committed_checkpoint == "opaque.cursor.v1:next"
    assert second.status is EmailSyncStatus.COMPLETE
    assert second.committed_checkpoint == "provider-cursor-is-opaque"
    assert effects.calls == ["effect-one", "effect-two"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["exception", "non_durable"])
async def test_page_failure_holds_checkpoint_until_every_effect_is_durable(
    failure_kind: str,
) -> None:
    effect_port = FakeEffects()
    if failure_kind == "exception":
        effect_port.fail_keys.add("effect-two")
    else:
        effect_port.nondurable_keys.add("effect-two")
    service, _, _, repository = coordinator(effects=effect_port)
    attempt = await service.create_attempt(mailbox_scope(), attempt_key="sync-partial")

    result = await service.apply_page(
        attempt.attempt_id,
        page(effect_keys=("effect-one", "effect-two")),
    )

    assert result.status is EmailSyncStatus.INCOMPLETE
    assert result.committed_checkpoint is None
    assert result.pages[0].committed is False
    stored = await repository.get(attempt.attempt_id)
    assert stored is not None
    assert stored.committed_checkpoint is None


@pytest.mark.asyncio
async def test_incomplete_page_blocks_later_page_from_skipping_its_effects() -> None:
    effect_port = FakeEffects()
    effect_port.fail_keys.add("effect-one")
    service, _, _, _ = coordinator(effects=effect_port)
    attempt = await service.create_attempt(mailbox_scope(), attempt_key="sync-no-gap")

    failed = await service.apply_page(attempt.attempt_id, page())
    later = await service.apply_page(
        attempt.attempt_id,
        page(
            page_id="page-two",
            next_checkpoint="opaque.cursor.v1:later",
        ),
    )

    assert failed.status is EmailSyncStatus.INCOMPLETE
    assert later.status is EmailSyncStatus.INCOMPLETE
    assert later.committed_checkpoint is None


@pytest.mark.asyncio
async def test_duplicate_page_and_effect_replay_return_stable_receipts() -> None:
    service, _, effects, _ = coordinator()
    attempt = await service.create_attempt(mailbox_scope(), attempt_key="sync-duplicate")
    submitted_page = page()

    first = await service.apply_page(attempt.attempt_id, submitted_page)
    replay = await service.apply_page(attempt.attempt_id, submitted_page)

    assert replay == first
    assert effects.calls == ["effect-one"]
    assert replay.pages[0].effect_receipts[0].receipt_reference == "receipt:effect-one"


@pytest.mark.asyncio
async def test_retry_after_effect_receipt_before_checkpoint_commit_converges() -> None:
    service, _, effects, repository = coordinator()
    attempt = await service.create_attempt(mailbox_scope(), attempt_key="sync-crash")
    submitted_page = page()

    # Simulate a process interruption after the external effect is durable but
    # before the coordinator records that receipt.
    effect_port = effects
    original_save = repository.save
    interrupted = False

    async def save_with_one_interruption(record):  # type: ignore[no-untyped-def]
        nonlocal interrupted
        if record.pages and record.pages[0].effect_receipts and not interrupted:
            interrupted = True
            raise RuntimeError("synthetic interruption")
        return await original_save(record)

    repository.save = save_with_one_interruption  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        await service.apply_page(attempt.attempt_id, submitted_page)

    repository.save = original_save  # type: ignore[method-assign]
    result = await service.apply_page(attempt.attempt_id, submitted_page)

    assert result.status is EmailSyncStatus.RUNNING
    assert result.committed_checkpoint == "opaque.cursor.v1:next"
    assert effect_port.calls == ["effect-one", "effect-one"]
    assert len(effect_port.receipts) == 1


@pytest.mark.asyncio
async def test_revoked_admission_suspends_without_calling_effects() -> None:
    service, _, effects, _ = coordinator(
        admission_state=EmailSyncAdmissionState.REVOKED
    )
    attempt = await service.create_attempt(mailbox_scope(), attempt_key="sync-revoked")

    result = await service.apply_page(attempt.attempt_id, page())

    assert result.status is EmailSyncStatus.SUSPENDED
    assert result.committed_checkpoint is None
    assert effects.calls == []


@pytest.mark.asyncio
async def test_stale_generation_cannot_publish_page_checkpoint() -> None:
    service, admission, effects, _ = coordinator()
    attempt = await service.create_attempt(mailbox_scope(), attempt_key="sync-stale")
    admission.state = EmailSyncAdmissionState.STALE_GENERATION

    result = await service.apply_page(attempt.attempt_id, page())

    assert result.status is EmailSyncStatus.RECONCILIATION_REQUIRED
    assert result.committed_checkpoint is None
    assert effects.calls == []


@pytest.mark.asyncio
async def test_generation_change_after_effects_still_holds_checkpoint() -> None:
    service, admission, effects, _ = coordinator()
    attempt = await service.create_attempt(mailbox_scope(), attempt_key="sync-fenced")
    admission.states = [
        EmailSyncAdmissionState.ALLOWED,
        EmailSyncAdmissionState.STALE_GENERATION,
    ]

    result = await service.apply_page(attempt.attempt_id, page())

    assert result.status is EmailSyncStatus.RECONCILIATION_REQUIRED
    assert result.committed_checkpoint is None
    assert effects.calls == ["effect-one"]
    assert all(
        request.connection_version_id == CONNECTION_VERSION_ID
        and request.authorization_generation == 3
        and request.configuration_generation == 4
        and request.scope_generation == 5
        for request in admission.calls
    )


@pytest.mark.asyncio
async def test_cancelled_attempt_is_suspended_and_cannot_resume() -> None:
    service, _, effects, _ = coordinator()
    attempt = await service.create_attempt(mailbox_scope(), attempt_key="sync-cancel")

    cancelled = await service.cancel(attempt.attempt_id)
    resumed = await service.apply_page(attempt.attempt_id, page())

    assert cancelled.status is EmailSyncStatus.SUSPENDED
    assert resumed.status is EmailSyncStatus.SUSPENDED
    assert effects.calls == []

