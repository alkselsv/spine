from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from spine.application.email_sync import (
    EMAIL_SYNC_STATE_OBSERVED_CONSUMER_NAME,
    EMAIL_SYNC_STATE_OBSERVED_EVENT_TYPE,
    EMAIL_SYNC_STATE_OBSERVED_SCHEMA_VERSION,
    EmailSyncAdmission,
    EmailSyncAdmissionState,
    EmailSyncAttempt,
    EmailSyncCoordinator,
    EmailSyncEffect,
    EmailSyncEffectReceipt,
    EmailSyncPage,
    EmailSyncStatus,
    EmailSyncStateObservedConsumer,
    EmailSyncStateObservedOutboxPayload,
    InMemoryEmailSyncRepository,
    register_email_sync_delivery_route,
)
from spine.application.diagnostics import DiagnosticContext
from spine.application.persistence.delivery import OutboxDeliveryRouteRegistry
from spine.application.persistence.outbox import OutboxIntent
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


class FailOnceAfterReceiptRepository(InMemoryEmailSyncRepository):
    def __init__(self) -> None:
        super().__init__()
        self.interrupted = False

    async def save(self, attempt: EmailSyncAttempt) -> EmailSyncAttempt:
        if attempt.pages and attempt.pages[0].effect_receipts and not self.interrupted:
            self.interrupted = True
            raise RuntimeError("synthetic interruption")
        return await super().save(attempt)


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
    publish_checkpoint: bool = True,
    effect_keys: tuple[str, ...] = ("effect-one",),
) -> EmailSyncPage:
    effects_by_key = {"effect-one": EFFECT_ONE, "effect-two": EFFECT_TWO}
    return EmailSyncPage(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        page_id=page_id,
        requested_checkpoint=requested_checkpoint,
        next_checkpoint=next_checkpoint,
        terminal=terminal,
        publish_checkpoint=publish_checkpoint,
        effects=tuple(
            EmailSyncEffect(
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
                effect_key=key,
                effect_reference=effects_by_key[key],
            )
            for key in effect_keys
        ),
    )


def coordinator(
    *,
    admission_state: EmailSyncAdmissionState = EmailSyncAdmissionState.ALLOWED,
    effects: FakeEffects | None = None,
    clock: FakeClock | None = None,
    repository: InMemoryEmailSyncRepository | None = None,
) -> tuple[EmailSyncCoordinator, FakeAdmission, FakeEffects, InMemoryEmailSyncRepository]:
    effect_port = effects or FakeEffects()
    repository = repository or InMemoryEmailSyncRepository()
    admission = FakeAdmission(admission_state)
    service = EmailSyncCoordinator(
        repository=repository,
        admission=admission,
        effects=effect_port,
        clock=clock or FakeClock(),
    )
    return service, admission, effect_port, repository


async def apply_page(
    service: EmailSyncCoordinator,
    attempt_id: UUID,
    submitted_page: EmailSyncPage,
    *,
    workspace_id: UUID = WORKSPACE_ID,
    environment_id: UUID = ENVIRONMENT_ID,
) -> EmailSyncAttempt:
    return await service.apply_page(
        attempt_id,
        submitted_page,
        workspace_id=workspace_id,
        environment_id=environment_id,
    )


@pytest.mark.asyncio
async def test_pages_commit_opaque_checkpoints_after_durable_effects() -> None:
    service, _, effects, _ = coordinator()
    attempt = await service.create_attempt(mailbox_scope(), attempt_key="sync-1")

    first = await apply_page(service, attempt.attempt_id, page())
    second = await apply_page(
        service,
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
async def test_staged_page_is_durable_without_publishing_aggregate_checkpoint() -> None:
    service, _, effects, repository = coordinator()
    attempt = await service.create_attempt(mailbox_scope(), attempt_key="sync-staged")
    staged = page(next_checkpoint=None, publish_checkpoint=False).model_copy(
        update={
            "evidence_digest": "a" * 64,
            "evidence_cursor": "provider-private-stage",
        }
    )

    result = await service.apply_page(attempt.attempt_id, staged, workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID)

    assert result.status is EmailSyncStatus.RUNNING
    assert result.committed_checkpoint is None
    assert result.pages[0].committed is True
    assert result.pages[0].evidence_cursor == "provider-private-stage"
    assert effects.calls == ["effect-one"]
    assert await repository.get(attempt.attempt_id, WORKSPACE_ID, ENVIRONMENT_ID) == result


@pytest.mark.asyncio
async def test_record_durable_page_requires_exact_receipt_coverage() -> None:
    service, _, effects, _ = coordinator()
    attempt = await service.create_attempt(mailbox_scope(), attempt_key="sync-receipts")
    staged = page(next_checkpoint=None, publish_checkpoint=False)

    with pytest.raises(ValueError, match="cover every effect"):
        await service.record_durable_page(
            attempt.attempt_id,
            staged,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            receipts=(),
        )

    assert effects.calls == []


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

    result = await apply_page(
        service,
        attempt.attempt_id,
        page(effect_keys=("effect-one", "effect-two")),
    )

    assert result.status is EmailSyncStatus.INCOMPLETE
    assert result.committed_checkpoint is None
    assert result.pages[0].committed is False
    assert result.pages[0].next_checkpoint is None
    stored = await repository.get(
        attempt.attempt_id, WORKSPACE_ID, ENVIRONMENT_ID
    )
    assert stored is not None
    assert stored.committed_checkpoint is None


@pytest.mark.asyncio
async def test_incomplete_page_blocks_later_page_from_skipping_its_effects() -> None:
    effect_port = FakeEffects()
    effect_port.fail_keys.add("effect-one")
    service, _, _, _ = coordinator(effects=effect_port)
    attempt = await service.create_attempt(mailbox_scope(), attempt_key="sync-no-gap")

    failed = await apply_page(service, attempt.attempt_id, page())
    later = await apply_page(
        service,
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

    first = await apply_page(service, attempt.attempt_id, submitted_page)
    replay = await apply_page(service, attempt.attempt_id, submitted_page)

    assert replay == first
    assert effects.calls == ["effect-one"]
    assert replay.pages[0].effect_receipts[0].receipt_reference == "receipt:effect-one"


@pytest.mark.asyncio
async def test_retry_after_effect_receipt_before_checkpoint_commit_converges() -> None:
    repository = FailOnceAfterReceiptRepository()
    service, _, effects, _ = coordinator(repository=repository)
    attempt = await service.create_attempt(mailbox_scope(), attempt_key="sync-crash")
    submitted_page = page()

    # Simulate a process interruption after the external effect is durable but
    # before the coordinator records that receipt.
    effect_port = effects
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        await apply_page(service, attempt.attempt_id, submitted_page)

    result = await apply_page(service, attempt.attempt_id, submitted_page)

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

    result = await apply_page(service, attempt.attempt_id, page())

    assert result.status is EmailSyncStatus.SUSPENDED
    assert result.committed_checkpoint is None
    assert effects.calls == []


@pytest.mark.asyncio
async def test_denied_admission_suspends_without_disclosing_or_applying_effects() -> None:
    service, _, effects, _ = coordinator(admission_state=EmailSyncAdmissionState.DENIED)
    attempt = await service.create_attempt(mailbox_scope(), attempt_key="sync-denied")

    result = await apply_page(service, attempt.attempt_id, page())

    assert result.status is EmailSyncStatus.SUSPENDED
    assert result.committed_checkpoint is None
    assert result.failure_code is None
    assert effects.calls == []


@pytest.mark.asyncio
async def test_attempt_reads_and_commands_are_isolated_by_workspace_and_environment() -> None:
    service, _, effects, repository = coordinator()
    attempt = await service.create_attempt(mailbox_scope(), attempt_key="sync-tenant")
    other_workspace_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    assert await repository.get(attempt.attempt_id, other_workspace_id, ENVIRONMENT_ID) is None
    with pytest.raises(KeyError, match="not found"):
        await apply_page(
            service,
            attempt.attempt_id,
            page(),
            workspace_id=other_workspace_id,
        )

    other_scope = mailbox_scope().model_copy(update={"workspace_id": other_workspace_id})
    other_attempt = await service.create_attempt(other_scope, attempt_key="sync-tenant")
    assert other_attempt.attempt_id != attempt.attempt_id
    assert await repository.get(attempt.attempt_id, WORKSPACE_ID, ENVIRONMENT_ID) == attempt
    assert await repository.get(other_attempt.attempt_id, other_workspace_id, ENVIRONMENT_ID) == other_attempt
    assert effects.calls == []


@pytest.mark.asyncio
async def test_effect_scope_substitution_fails_before_effect_application() -> None:
    service, _, effects, _ = coordinator()
    attempt = await service.create_attempt(mailbox_scope(), attempt_key="sync-effect-scope")
    submitted = page().model_copy(
        update={
            "effects": (
                EmailSyncEffect(
                    workspace_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                    environment_id=ENVIRONMENT_ID,
                    effect_key="effect-one",
                    effect_reference=EFFECT_ONE,
                ),
            )
        }
    )

    result = await apply_page(service, attempt.attempt_id, submitted)

    assert result.status is EmailSyncStatus.FAILED
    assert result.failure_code == "effect_scope_conflict"
    assert effects.calls == []


@pytest.mark.asyncio
async def test_stale_generation_cannot_publish_page_checkpoint() -> None:
    service, admission, effects, _ = coordinator()
    attempt = await service.create_attempt(mailbox_scope(), attempt_key="sync-stale")
    admission.state = EmailSyncAdmissionState.STALE_GENERATION

    result = await apply_page(service, attempt.attempt_id, page())

    assert result.status is EmailSyncStatus.RECONCILIATION_REQUIRED
    assert result.committed_checkpoint is None
    assert effects.calls == []


@pytest.mark.asyncio
async def test_generation_change_before_effect_holds_checkpoint_and_effect() -> None:
    service, admission, effects, _ = coordinator()
    attempt = await service.create_attempt(mailbox_scope(), attempt_key="sync-fenced")
    admission.states = [
        EmailSyncAdmissionState.ALLOWED,
        EmailSyncAdmissionState.STALE_GENERATION,
    ]

    result = await apply_page(service, attempt.attempt_id, page())

    assert result.status is EmailSyncStatus.RECONCILIATION_REQUIRED
    assert result.committed_checkpoint is None
    assert effects.calls == []
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

    cancelled = await service.cancel(
        attempt.attempt_id,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
    )
    resumed = await apply_page(service, attempt.attempt_id, page())

    assert cancelled.status is EmailSyncStatus.SUSPENDED
    assert resumed.status is EmailSyncStatus.SUSPENDED
    assert effects.calls == []


@pytest.mark.asyncio
async def test_running_attempt_can_be_cancelled_without_applying_another_page() -> None:
    service, _, effects, _ = coordinator()
    attempt = await service.create_attempt(mailbox_scope(), attempt_key="sync-cancel-running")
    running = await apply_page(service, attempt.attempt_id, page())

    cancelled = await service.cancel(
        attempt.attempt_id,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
    )
    resumed = await apply_page(
        service,
        attempt.attempt_id,
        page(
            page_id="page-two",
            requested_checkpoint="opaque.cursor.v1:next",
            next_checkpoint="opaque.cursor.v1:last",
        ),
    )

    assert running.status is EmailSyncStatus.RUNNING
    assert cancelled.status is EmailSyncStatus.SUSPENDED
    assert resumed.status is EmailSyncStatus.SUSPENDED
    assert effects.calls == ["effect-one"]


@pytest.mark.asyncio
async def test_state_observation_event_has_explicit_validating_delivery_route() -> None:
    routes = OutboxDeliveryRouteRegistry(version="email-sync-test.v1")
    register_email_sync_delivery_route(routes)
    route = routes.resolve(
        EMAIL_SYNC_STATE_OBSERVED_EVENT_TYPE,
        EMAIL_SYNC_STATE_OBSERVED_SCHEMA_VERSION,
    )
    diagnostic_context = DiagnosticContext(trace_id=EFFECT_ONE)
    intent = OutboxIntent(
        event_id=EFFECT_ONE,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        event_type=EMAIL_SYNC_STATE_OBSERVED_EVENT_TYPE,
        schema_version=EMAIL_SYNC_STATE_OBSERVED_SCHEMA_VERSION,
        payload=EmailSyncStateObservedOutboxPayload(
            observation_id=EFFECT_TWO,
            connection_id=CONNECTION_ID,
            mailbox_binding_id=MAILBOX_BINDING_ID,
            state_kind="label_added",
            provider_event_digest="a" * 64,
        ),
        trace_id=EFFECT_ONE,
    )

    result = await EmailSyncStateObservedConsumer().consume(
        intent,
        diagnostic_context,
    )

    assert route.consumer_name == EMAIL_SYNC_STATE_OBSERVED_CONSUMER_NAME
    assert result.diagnostic_context == diagnostic_context
