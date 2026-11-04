from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from spine.application.email_sync import (
    EmailSyncAdmission,
    EmailSyncAdmissionState,
    EmailSyncAttempt,
    EmailSyncCoordinator,
    EmailSyncEffect,
    EmailSyncEffectReceipt,
    EmailSyncStatus,
    InMemoryEmailSyncRepository,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.yandex_mail_uidvalidity_reset import (
    YandexMailUidValidityResetCommand,
    YandexMailUidValidityResetReceipt,
    YandexMailUidValidityResetState,
)
from spine.connectors.yandex_mail_generation import (
    YandexMailFolderGenerationCode,
    YandexMailFolderGenerationObservation,
)
from spine.connectors.yandex_mail_incremental import (
    YandexMailIncrementalBaseline,
    YandexMailIncrementalCheckpoint,
    YandexMailIncrementalPollState,
    YandexMailIncrementalPoller,
    decode_yandex_mail_incremental_checkpoint,
    encode_yandex_mail_incremental_checkpoint,
    yandex_mail_uid_effect_key,
)
from spine.connectors.yandex_mail_uid_enumeration import (
    YandexMailUidEnumerationPageRequest,
    YandexMailUidEnumerationPageResult,
    YandexMailUidMessageMetadata,
    YandexMailUidPageCode,
)
from spine.domain.connections.email_scope import (
    EmailProviderOperation,
    MailboxReadScope,
)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


FOLDER_ID = _id(11)


def _scope(**changes: object) -> MailboxReadScope:
    values: dict[str, object] = {
        "workspace_id": _id(1),
        "environment_id": _id(2),
        "connection_id": _id(3),
        "connection_version_id": _id(4),
        "provider_kind": "yandex_mail",
        "resource_scope_reference": _id(5),
        "mailbox_subject_reference": _id(6),
        "mailbox_binding_id": _id(7),
        "authorization_generation": 8,
        "configuration_generation": 9,
        "scope_generation": 10,
        "allowed_folder_references": (FOLDER_ID,),
        "allowed_operations": tuple(EmailProviderOperation),
    }
    values.update(changes)
    return MailboxReadScope(**values)


def _generation(
    *,
    uid_validity: int = 101,
    uid_next: int = 14,
    scope: MailboxReadScope | None = None,
    complete: bool = True,
    code: YandexMailFolderGenerationCode = YandexMailFolderGenerationCode.COMPLETE,
) -> YandexMailFolderGenerationObservation:
    scope = scope or _scope()
    return YandexMailFolderGenerationObservation(
        complete=complete,
        code=code,
        workspace_id=scope.workspace_id,
        environment_id=scope.environment_id,
        connection_id=scope.connection_id,
        connection_version_id=scope.connection_version_id,
        mailbox_binding_id=scope.mailbox_binding_id,
        folder_reference=FOLDER_ID,
        authorization_generation=scope.authorization_generation,
        configuration_generation=scope.configuration_generation,
        scope_generation=scope.scope_generation,
        uid_validity=uid_validity,
        uid_next=uid_next,
        selection_command="EXAMINE",
        read_only=True,
    )


def _uid_page(
    request: YandexMailUidEnumerationPageRequest,
    *,
    uids: tuple[int, ...],
    complete: bool = True,
    code: YandexMailUidPageCode = YandexMailUidPageCode.COMPLETE,
) -> YandexMailUidEnumerationPageResult:
    return YandexMailUidEnumerationPageResult(
        complete=complete,
        code=code,
        workspace_id=request.scope.workspace_id,
        environment_id=request.scope.environment_id,
        connection_id=request.scope.connection_id,
        connection_version_id=request.scope.connection_version_id,
        mailbox_binding_id=request.scope.mailbox_binding_id,
        folder_reference=FOLDER_ID,
        authorization_generation=request.scope.authorization_generation,
        configuration_generation=request.scope.configuration_generation,
        scope_generation=request.scope.scope_generation,
        uid_validity=request.uid_validity,
        covered_uid_range=(request.start_uid, request.end_uid) if complete else None,
        messages=tuple(
            YandexMailUidMessageMetadata(
                uid=uid,
                flags=("\\Seen", "private-flag-that-is-not-an-effect"),
                size_octets=uid * 10,
            )
            for uid in uids
        ),
        continuation_uid=request.continuation_uid if complete else None,
    )


class _Admission:
    def __init__(self) -> None:
        self.states: list[EmailSyncAdmissionState] = []

    async def check(self, admission: EmailSyncAdmission) -> EmailSyncAdmissionState:
        del admission
        if self.states:
            return self.states.pop(0)
        return EmailSyncAdmissionState.ALLOWED


class _Effects:
    def __init__(self) -> None:
        self.described: list[int] = []
        self.applied: list[str] = []
        self.failures_remaining = 0

    async def effect_for_uid(
        self,
        *,
        scope: MailboxReadScope,
        folder_reference: UUID,
        uid_validity: int,
        uid: int,
    ) -> EmailSyncEffect:
        self.described.append(uid)
        key = yandex_mail_uid_effect_key(
            scope=scope,
            folder_reference=folder_reference,
            uid_validity=uid_validity,
            uid=uid,
        )
        return EmailSyncEffect(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            effect_key=key,
            effect_reference=uuid5(NAMESPACE_URL, f"acceptance:{key}"),
        )

    async def apply(self, effect: EmailSyncEffect) -> EmailSyncEffectReceipt:
        self.applied.append(effect.effect_key)
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("private provider payload must not escape")
        return EmailSyncEffectReceipt(
            effect_key=effect.effect_key,
            receipt_reference=f"receipt:{effect.effect_reference.hex}",
            durable=True,
        )


class _Reader:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.commands: list[object] = []

    async def read(self, command, context):
        del context
        self.commands.append(command)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(command)
        return response


class _Baselines:
    def __init__(self, verified: bool = True) -> None:
        self.verified = verified
        self.calls: list[YandexMailIncrementalBaseline] = []

    async def verify(self, baseline, context) -> bool:
        del context
        self.calls.append(baseline)
        return self.verified


class _Resets:
    def __init__(self) -> None:
        self.calls: list[YandexMailUidValidityResetCommand] = []
        self.fail = False
        self.state = YandexMailUidValidityResetState.RECONCILING

    async def reconcile(
        self, command: YandexMailUidValidityResetCommand
    ) -> YandexMailUidValidityResetReceipt:
        self.calls.append(command)
        if self.fail:
            raise RuntimeError("private reset detail")
        return YandexMailUidValidityResetReceipt(
            reset_id=uuid5(NAMESPACE_URL, command.model_dump_json()),
            stale_attempt_id=command.stale_attempt_id,
            reconciliation_attempt_id=uuid5(
                NAMESPACE_URL, f"successor:{command.model_dump_json()}"
            ),
            folder_reference=command.folder_reference,
            stale_uid_validity=command.stale_uid_validity,
            observed_uid_validity=command.observed_uid_validity,
            state=self.state,
        )


class _FailAfterDurableReceiptRepository(InMemoryEmailSyncRepository):
    def __init__(self) -> None:
        super().__init__()
        self.interrupted = False

    async def save(self, attempt):
        if (
            not self.interrupted
            and attempt.pages
            and attempt.pages[-1].effect_receipts
        ):
            self.interrupted = True
            raise RuntimeError("synthetic crash after durable canonical effect")
        return await super().save(attempt)


class _StaleCursorBeforeFenceRepository(InMemoryEmailSyncRepository):
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
                        "committed_checkpoint": "stale-cursor-must-not-survive",
                    }
                )
            )
        return await super().fence_for_reconciliation(
            attempt,
            failure_code=failure_code,
        )


def _composition(
    reader: _Reader,
    *,
    repository: InMemoryEmailSyncRepository | None = None,
    effects: _Effects | None = None,
    admission: _Admission | None = None,
    baselines: _Baselines | None = None,
    resets: _Resets | None = None,
):
    repository = repository or InMemoryEmailSyncRepository()
    effects = effects or _Effects()
    admission = admission or _Admission()
    baselines = baselines or _Baselines()
    resets = resets or _Resets()
    coordinator = EmailSyncCoordinator(
        repository=repository,
        admission=admission,
        effects=effects,
        clock=lambda: datetime(2026, 11, 3, tzinfo=UTC),
    )
    poller = YandexMailIncrementalPoller(
        reader=reader,
        coordinator=coordinator,
        effect_factory=effects,
        baseline_verifier=baselines,
        uidvalidity_reset=resets,
    )
    return poller, coordinator, repository, effects, admission, baselines


def _context() -> TrustedPersistenceContext:
    return cast(TrustedPersistenceContext, object())


async def _attempt(coordinator: EmailSyncCoordinator, scope: MailboxReadScope):
    return await coordinator.create_attempt(scope, attempt_key="imap-folder-11")


def _baseline(
    scope: MailboxReadScope | None = None,
    *,
    uid_validity: int = 101,
    next_uid: int = 10,
) -> YandexMailIncrementalBaseline:
    return YandexMailIncrementalBaseline.from_scope(
        scope or _scope(),
        folder_reference=FOLDER_ID,
        uid_validity=uid_validity,
        next_uid=next_uid,
        coverage_kind="snapshot",
        coverage_receipt_reference="snapshot-receipt-11",
    )


@pytest.mark.asyncio
async def test_new_uid_gap_commits_exact_effects_before_covered_successor() -> None:
    scope = _scope()
    reader = _Reader(
        _generation(uid_next=14),
        lambda request: _uid_page(request, uids=(10, 12)),
    )
    poller, coordinator, _, effects, _, baselines = _composition(reader)
    attempt = await _attempt(coordinator, scope)

    result = await poller.poll(
        attempt.attempt_id,
        scope=scope,
        folder_reference=FOLDER_ID,
        context=_context(),
        max_items=10,
        max_response_bytes=4096,
        baseline=_baseline(scope),
    )

    checkpoint = decode_yandex_mail_incremental_checkpoint(
        result.attempt.committed_checkpoint or ""
    )
    assert result.state is YandexMailIncrementalPollState.APPLIED
    assert result.covered_uid_range == (10, 13)
    assert result.discovered_uids == (10, 12)
    assert checkpoint.next_uid == 14
    assert effects.described == [10, 12]
    assert len(effects.applied) == 2
    assert result.periodic_reconciliation_required is True
    assert result.covers_flag_changes is False
    assert result.covers_folder_changes is False
    assert result.proves_deletions is False
    assert all("private-flag" not in key for key in effects.applied)
    assert baselines.calls == [_baseline(scope)]


@pytest.mark.asyncio
async def test_no_change_commits_initial_baseline_then_reuses_it_without_uid_page() -> None:
    scope = _scope()
    reader = _Reader(_generation(uid_next=10), _generation(uid_next=10))
    poller, coordinator, _, effects, _, _ = _composition(reader)
    attempt = await _attempt(coordinator, scope)

    first = await poller.poll(
        attempt.attempt_id,
        scope=scope,
        folder_reference=FOLDER_ID,
        context=_context(),
        max_items=10,
        max_response_bytes=4096,
        baseline=_baseline(scope),
    )
    second = await poller.poll(
        attempt.attempt_id,
        scope=scope,
        folder_reference=FOLDER_ID,
        context=_context(),
        max_items=10,
        max_response_bytes=4096,
    )

    assert first.state is YandexMailIncrementalPollState.NO_CHANGE
    assert second.state is YandexMailIncrementalPollState.NO_CHANGE
    assert second.attempt.committed_checkpoint == first.attempt.committed_checkpoint
    assert len(second.attempt.pages) == 1
    assert effects.applied == []
    assert len(reader.commands) == 2


@pytest.mark.asyncio
async def test_first_uid_after_empty_baseline_uses_a_distinct_page_identity() -> None:
    scope = _scope()
    reader = _Reader(
        _generation(uid_next=10),
        _generation(uid_next=11),
        lambda request: _uid_page(request, uids=(10,)),
    )
    poller, coordinator, _, effects, _, _ = _composition(reader)
    attempt = await _attempt(coordinator, scope)

    empty = await poller.poll(
        attempt.attempt_id,
        scope=scope,
        folder_reference=FOLDER_ID,
        context=_context(),
        max_items=10,
        max_response_bytes=4096,
        baseline=_baseline(scope),
    )
    arrived = await poller.poll(
        attempt.attempt_id,
        scope=scope,
        folder_reference=FOLDER_ID,
        context=_context(),
        max_items=10,
        max_response_bytes=4096,
    )

    assert empty.state is YandexMailIncrementalPollState.NO_CHANGE
    assert arrived.state is YandexMailIncrementalPollState.APPLIED
    assert len(arrived.attempt.pages) == 2
    assert arrived.attempt.pages[0].page_id != arrived.attempt.pages[1].page_id
    assert decode_yandex_mail_incremental_checkpoint(
        arrived.attempt.committed_checkpoint or ""
    ).next_uid == 11
    assert len(effects.applied) == 1


@pytest.mark.asyncio
async def test_effect_crash_holds_checkpoint_and_reconnect_replays_same_page() -> None:
    scope = _scope()
    reader = _Reader(
        _generation(uid_next=12),
        lambda request: _uid_page(request, uids=(10,)),
        _generation(uid_next=12),
        lambda request: _uid_page(request, uids=(10,)),
    )
    repository = InMemoryEmailSyncRepository()
    effects = _Effects()
    effects.failures_remaining = 1
    poller, coordinator, _, _, admission, _ = _composition(
        reader, repository=repository, effects=effects
    )
    attempt = await _attempt(coordinator, scope)
    baseline = _baseline(scope)

    interrupted = await poller.poll(
        attempt.attempt_id,
        scope=scope,
        folder_reference=FOLDER_ID,
        context=_context(),
        max_items=2,
        max_response_bytes=4096,
        baseline=baseline,
    )
    restarted, _, _, _, _, _ = _composition(
        reader,
        repository=repository,
        effects=effects,
        admission=admission,
    )
    replay = await restarted.poll(
        attempt.attempt_id,
        scope=scope,
        folder_reference=FOLDER_ID,
        context=_context(),
        max_items=2,
        max_response_bytes=4096,
        baseline=baseline,
    )

    assert interrupted.state is YandexMailIncrementalPollState.INCOMPLETE
    assert interrupted.attempt.committed_checkpoint is None
    assert replay.state is YandexMailIncrementalPollState.APPLIED
    assert replay.attempt.committed_checkpoint is not None
    assert replay.attempt.failure_code is None
    assert effects.described == [10, 10]
    assert len(effects.applied) == 2


@pytest.mark.asyncio
async def test_crash_after_durable_effect_before_receipt_replays_without_advancing() -> None:
    scope = _scope()
    reader = _Reader(
        _generation(uid_next=11),
        lambda request: _uid_page(request, uids=(10,)),
        _generation(uid_next=11),
        lambda request: _uid_page(request, uids=(10,)),
    )
    repository = _FailAfterDurableReceiptRepository()
    effects = _Effects()
    poller, coordinator, _, _, admission, baselines = _composition(
        reader, repository=repository, effects=effects
    )
    attempt = await _attempt(coordinator, scope)

    with pytest.raises(RuntimeError, match="synthetic crash"):
        await poller.poll(
            attempt.attempt_id,
            scope=scope,
            folder_reference=FOLDER_ID,
            context=_context(),
            max_items=10,
            max_response_bytes=4096,
            baseline=_baseline(scope),
        )
    held = await repository.get(
        attempt.attempt_id, scope.workspace_id, scope.environment_id
    )
    assert held is not None
    assert held.committed_checkpoint is None

    restarted, _, _, _, _, _ = _composition(
        reader,
        repository=repository,
        effects=effects,
        admission=admission,
        baselines=baselines,
    )
    replay = await restarted.poll(
        attempt.attempt_id,
        scope=scope,
        folder_reference=FOLDER_ID,
        context=_context(),
        max_items=10,
        max_response_bytes=4096,
        baseline=_baseline(scope),
    )

    assert replay.state is YandexMailIncrementalPollState.APPLIED
    assert replay.attempt.committed_checkpoint is not None
    assert effects.applied[0] == effects.applied[1]


@pytest.mark.asyncio
async def test_partial_batch_and_provider_outage_hold_until_safe_reconnect() -> None:
    scope = _scope()
    reader = _Reader(
        _generation(uid_next=12),
        lambda request: _uid_page(
            request,
            uids=(10,),
            complete=False,
            code=YandexMailUidPageCode.PARTIAL_BATCH,
        ),
        RuntimeError("token=private subject=secret@example.test"),
        _generation(uid_next=12),
        lambda request: _uid_page(request, uids=(10,)),
    )
    poller, coordinator, _, effects, _, _ = _composition(reader)
    attempt = await _attempt(coordinator, scope)
    baseline = _baseline(scope)

    partial = await poller.poll(
        attempt.attempt_id,
        scope=scope,
        folder_reference=FOLDER_ID,
        context=_context(),
        max_items=2,
        max_response_bytes=4096,
        baseline=baseline,
    )
    outage = await poller.poll(
        attempt.attempt_id,
        scope=scope,
        folder_reference=FOLDER_ID,
        context=_context(),
        max_items=2,
        max_response_bytes=4096,
        baseline=baseline,
    )
    recovered = await poller.poll(
        attempt.attempt_id,
        scope=scope,
        folder_reference=FOLDER_ID,
        context=_context(),
        max_items=2,
        max_response_bytes=4096,
        baseline=baseline,
    )

    assert partial.attempt.committed_checkpoint is None
    assert outage.attempt.committed_checkpoint is None
    assert recovered.state is YandexMailIncrementalPollState.APPLIED
    assert recovered.attempt.committed_checkpoint is not None
    assert effects.applied
    assert "private" not in repr(partial)
    assert "secret@example.test" not in repr(outage)


@pytest.mark.asyncio
async def test_uidvalidity_and_stale_admission_fence_successor_publication() -> None:
    scope = _scope()
    reader = _Reader(
        _generation(
            uid_validity=102,
            complete=False,
            code=YandexMailFolderGenerationCode.UIDVALIDITY_CHANGED,
        )
    )
    resets = _Resets()
    repository = _StaleCursorBeforeFenceRepository()
    poller, coordinator, _, effects, _, _ = _composition(
        reader,
        repository=repository,
        resets=resets,
    )
    attempt = await _attempt(coordinator, scope)

    changed = await poller.poll(
        attempt.attempt_id,
        scope=scope,
        folder_reference=FOLDER_ID,
        context=_context(),
        max_items=10,
        max_response_bytes=4096,
        baseline=_baseline(scope),
    )

    assert changed.state is YandexMailIncrementalPollState.RECONCILIATION_REQUIRED
    assert changed.attempt.committed_checkpoint is None
    assert effects.applied == []
    assert changed.reset_receipt is not None
    assert changed.reset_receipt.observed_uid_validity == 102
    assert len(resets.calls) == 1

    reader = _Reader(
        _generation(uid_next=11),
        lambda request: _uid_page(request, uids=(10,)),
    )
    poller, coordinator, _, effects, admission, _ = _composition(reader)
    attempt = await _attempt(coordinator, scope)
    admission.states = [
        EmailSyncAdmissionState.ALLOWED,
        EmailSyncAdmissionState.STALE_GENERATION,
    ]
    stale = await poller.poll(
        attempt.attempt_id,
        scope=scope,
        folder_reference=FOLDER_ID,
        context=_context(),
        max_items=10,
        max_response_bytes=4096,
        baseline=_baseline(scope),
    )

    assert stale.state is YandexMailIncrementalPollState.RECONCILIATION_REQUIRED
    assert stale.attempt.committed_checkpoint is None
    assert effects.applied == []


@pytest.mark.asyncio
async def test_uidvalidity_reset_during_uid_page_starts_reconciliation() -> None:
    scope = _scope()

    def reset_page(request: YandexMailUidEnumerationPageRequest):
        return _uid_page(request, uids=(), complete=False).model_copy(
            update={
                "code": YandexMailUidPageCode.UIDVALIDITY_CHANGED,
                "uid_validity": 102,
            }
        )

    reader = _Reader(_generation(uid_next=11), reset_page)
    resets = _Resets()
    poller, coordinator, _, effects, _, _ = _composition(reader, resets=resets)
    attempt = await _attempt(coordinator, scope)

    result = await poller.poll(
        attempt.attempt_id,
        scope=scope,
        folder_reference=FOLDER_ID,
        context=_context(),
        max_items=10,
        max_response_bytes=4096,
        baseline=_baseline(scope),
    )

    assert result.state is YandexMailIncrementalPollState.RECONCILIATION_REQUIRED
    assert result.attempt.committed_checkpoint is None
    assert result.reset_receipt is not None
    assert len(resets.calls) == 1
    assert effects.described == []
    assert effects.applied == []


@pytest.mark.asyncio
async def test_uidvalidity_reset_failure_is_typed_and_retried_without_disclosure() -> None:
    scope = _scope()
    reset_generation = _generation(
        uid_validity=102,
        complete=False,
        code=YandexMailFolderGenerationCode.UIDVALIDITY_CHANGED,
    )
    reader = _Reader(reset_generation, reset_generation)
    resets = _Resets()
    resets.fail = True
    poller, coordinator, _, effects, _, _ = _composition(reader, resets=resets)
    attempt = await _attempt(coordinator, scope)

    failed = await poller.poll(
        attempt.attempt_id,
        scope=scope,
        folder_reference=FOLDER_ID,
        context=_context(),
        max_items=10,
        max_response_bytes=4096,
        baseline=_baseline(scope),
    )
    resets.fail = False
    recovered = await poller.poll(
        attempt.attempt_id,
        scope=scope,
        folder_reference=FOLDER_ID,
        context=_context(),
        max_items=10,
        max_response_bytes=4096,
        baseline=_baseline(scope),
    )

    assert failed.state is YandexMailIncrementalPollState.RECONCILIATION_REQUIRED
    assert failed.reset_state is YandexMailUidValidityResetState.INCOMPLETE
    assert failed.attempt.failure_code == "uidvalidity_reset_incomplete"
    assert failed.reset_receipt is None
    assert "private reset detail" not in repr(failed)
    assert recovered.reset_state is YandexMailUidValidityResetState.RECONCILING
    assert recovered.reset_receipt is not None
    assert recovered.attempt.failure_code == "uidvalidity_reset_started"
    assert len(resets.calls) == 2
    assert effects.applied == []


@pytest.mark.asyncio
async def test_incomplete_uidvalidity_reset_receipt_is_retried() -> None:
    scope = _scope()
    reset_generation = _generation(
        uid_validity=102,
        complete=False,
        code=YandexMailFolderGenerationCode.UIDVALIDITY_CHANGED,
    )
    reader = _Reader(reset_generation, reset_generation)
    resets = _Resets()
    resets.state = YandexMailUidValidityResetState.INCOMPLETE
    poller, coordinator, _, effects, _, _ = _composition(reader, resets=resets)
    attempt = await _attempt(coordinator, scope)

    incomplete = await poller.poll(
        attempt.attempt_id,
        scope=scope,
        folder_reference=FOLDER_ID,
        context=_context(),
        max_items=10,
        max_response_bytes=4096,
        baseline=_baseline(scope),
    )
    resets.state = YandexMailUidValidityResetState.RECONCILING
    replay = await poller.poll(
        attempt.attempt_id,
        scope=scope,
        folder_reference=FOLDER_ID,
        context=_context(),
        max_items=10,
        max_response_bytes=4096,
        baseline=_baseline(scope),
    )

    assert incomplete.reset_state is YandexMailUidValidityResetState.INCOMPLETE
    assert incomplete.attempt.failure_code == "uidvalidity_reset_incomplete"
    assert replay.reset_state is YandexMailUidValidityResetState.RECONCILING
    assert replay.attempt.failure_code == "uidvalidity_reset_started"
    assert len(resets.calls) == 2
    assert effects.applied == []


@pytest.mark.asyncio
async def test_stale_uid_page_scope_cannot_describe_or_publish_effects() -> None:
    scope = _scope()

    def stale_page(request: YandexMailUidEnumerationPageRequest):
        return _uid_page(request, uids=(10,)).model_copy(
            update={"scope_generation": 99}
        )

    reader = _Reader(_generation(uid_next=11), stale_page)
    poller, coordinator, _, effects, _, _ = _composition(reader)
    attempt = await _attempt(coordinator, scope)

    result = await poller.poll(
        attempt.attempt_id,
        scope=scope,
        folder_reference=FOLDER_ID,
        context=_context(),
        max_items=10,
        max_response_bytes=4096,
        baseline=_baseline(scope),
    )

    assert result.state is YandexMailIncrementalPollState.INCOMPLETE
    assert result.attempt.committed_checkpoint is None
    assert effects.described == []
    assert effects.applied == []


@pytest.mark.asyncio
async def test_unverified_or_wrong_scope_baseline_cannot_publish() -> None:
    scope = _scope()
    reader = _Reader()
    baselines = _Baselines(verified=False)
    poller, coordinator, _, effects, _, _ = _composition(
        reader, baselines=baselines
    )
    attempt = await _attempt(coordinator, scope)

    unverified = await poller.poll(
        attempt.attempt_id,
        scope=scope,
        folder_reference=FOLDER_ID,
        context=_context(),
        max_items=10,
        max_response_bytes=4096,
        baseline=_baseline(scope),
    )

    assert unverified.state is YandexMailIncrementalPollState.RECONCILIATION_REQUIRED
    assert unverified.attempt.committed_checkpoint is None
    assert effects.applied == []
    assert reader.commands == []

    verified_baselines = _Baselines(verified=True)
    poller, coordinator, _, _, _, _ = _composition(
        reader, baselines=verified_baselines
    )
    wrong_attempt = await coordinator.create_attempt(
        scope, attempt_key="imap-folder-11-wrong-baseline"
    )
    wrong_scope = await poller.poll(
        wrong_attempt.attempt_id,
        scope=scope,
        folder_reference=FOLDER_ID,
        context=_context(),
        max_items=10,
        max_response_bytes=4096,
        baseline=_baseline(
            scope.model_copy(update={"scope_generation": 99})
        ),
    )
    assert wrong_scope.state is YandexMailIncrementalPollState.RECONCILIATION_REQUIRED
    assert wrong_scope.attempt.committed_checkpoint is None
    assert verified_baselines.calls == []
    assert reader.commands == []


@pytest.mark.asyncio
async def test_denied_scope_and_other_workspace_do_not_reach_provider() -> None:
    denied_scope = _scope(
        allowed_operations=(EmailProviderOperation.SYNCHRONIZE,)
    )
    reader = _Reader()
    poller, coordinator, _, effects, _, _ = _composition(reader)
    denied_attempt = await _attempt(coordinator, denied_scope)

    denied = await poller.poll(
        denied_attempt.attempt_id,
        scope=denied_scope,
        folder_reference=FOLDER_ID,
        context=_context(),
        max_items=10,
        max_response_bytes=4096,
        baseline=_baseline(denied_scope),
    )

    assert denied.state is YandexMailIncrementalPollState.SUSPENDED
    assert denied.attempt.committed_checkpoint is None
    assert reader.commands == []
    assert effects.applied == []

    allowed_scope = _scope()
    allowed_attempt = await _attempt(coordinator, allowed_scope)
    other_scope = _scope(workspace_id=_id(99))
    with pytest.raises(KeyError, match="not found"):
        await poller.poll(
            allowed_attempt.attempt_id,
            scope=other_scope,
            folder_reference=FOLDER_ID,
            context=_context(),
            max_items=10,
            max_response_bytes=4096,
            baseline=_baseline(other_scope),
        )
    assert reader.commands == []


@pytest.mark.asyncio
async def test_reconciliation_hold_does_not_read_provider_or_report_no_change() -> None:
    scope = _scope()
    reader = _Reader(
        _generation(
            uid_validity=102,
            complete=False,
            code=YandexMailFolderGenerationCode.UIDVALIDITY_CHANGED,
        )
    )
    poller, coordinator, _, _, _, _ = _composition(reader)
    attempt = await _attempt(coordinator, scope)
    held = await poller.poll(
        attempt.attempt_id,
        scope=scope,
        folder_reference=FOLDER_ID,
        context=_context(),
        max_items=10,
        max_response_bytes=4096,
        baseline=_baseline(scope),
    )
    replay = await poller.poll(
        attempt.attempt_id,
        scope=scope,
        folder_reference=FOLDER_ID,
        context=_context(),
        max_items=10,
        max_response_bytes=4096,
        baseline=_baseline(scope),
    )

    assert held.state is YandexMailIncrementalPollState.RECONCILIATION_REQUIRED
    assert replay.state is YandexMailIncrementalPollState.RECONCILIATION_REQUIRED
    assert len(reader.commands) == 1


def test_checkpoint_round_trip_is_scope_bound_and_explicitly_reconciliation_only() -> None:
    scope = _scope()
    checkpoint = YandexMailIncrementalCheckpoint.from_scope(
        scope,
        folder_reference=FOLDER_ID,
        uid_validity=101,
        next_uid=42,
        baseline_kind="snapshot",
        baseline_receipt_reference="snapshot-receipt-11",
    )

    restored = decode_yandex_mail_incremental_checkpoint(
        encode_yandex_mail_incremental_checkpoint(checkpoint)
    )

    assert restored == checkpoint
    assert restored.matches_scope(scope, FOLDER_ID)
    assert restored.matches_scope(
        scope.model_copy(update={"scope_generation": 99}), FOLDER_ID
    ) is False
    assert restored.periodic_reconciliation_required is True
    assert restored.covers_flag_changes is False
    assert restored.covers_folder_changes is False
    assert restored.proves_deletions is False


def test_effect_identity_changes_across_environment_and_connection_version() -> None:
    scope = _scope()
    base = yandex_mail_uid_effect_key(
        scope=scope,
        folder_reference=FOLDER_ID,
        uid_validity=101,
        uid=42,
    )

    other_environment = yandex_mail_uid_effect_key(
        scope=scope.model_copy(update={"environment_id": _id(99)}),
        folder_reference=FOLDER_ID,
        uid_validity=101,
        uid=42,
    )
    other_connection_version = yandex_mail_uid_effect_key(
        scope=scope.model_copy(update={"connection_version_id": _id(98)}),
        folder_reference=FOLDER_ID,
        uid_validity=101,
        uid=42,
    )

    assert len({base, other_environment, other_connection_version}) == 3
