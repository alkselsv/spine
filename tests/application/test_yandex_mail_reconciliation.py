from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.email_sync import (
    EmailSyncAdmission,
    EmailSyncAdmissionState,
    EmailSyncCoordinator,
    EmailSyncEffect,
    EmailSyncPage,
    EmailSyncPageKind,
    EmailSyncStatus,
    InMemoryEmailSyncRepository,
)
from spine.application.yandex_mail_reconciliation import (
    EmailLifecyclePersistenceError,
    EmailLifecycleCoverage,
    EmailLifecycleLineage,
    EmailLifecycleOutcome,
    YandexMailReconciliationCommand,
    YandexMailReconciliationCoordinator,
    YandexMailReconciliationFolderEvidence,
    YandexMailReconciliationMessage,
)
from spine.domain.connections.email_scope import (
    EmailProviderOperation,
    MailboxReadScope,
)
from spine.domain.sources.email_lifecycle import EmailLifecycleReceipt
from spine.domain.sources.imap import ImapMessageKey

NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)
WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
ENVIRONMENT_ID = UUID("20000000-0000-0000-0000-000000000002")
CONNECTION_ID = UUID("30000000-0000-0000-0000-000000000003")
CONNECTION_VERSION_ID = UUID("40000000-0000-0000-0000-000000000004")
MAILBOX_ID = UUID("50000000-0000-0000-0000-000000000005")
FOLDER_ID = UUID("60000000-0000-0000-0000-000000000006")
SCOPE_ID = UUID("70000000-0000-0000-0000-000000000007")
SUBJECT_ID = UUID("80000000-0000-0000-0000-000000000008")
SCAN_ID = UUID("90000000-0000-0000-0000-000000000009")


class Admission:
    def __init__(
        self, state: EmailSyncAdmissionState = EmailSyncAdmissionState.ALLOWED
    ) -> None:
        self.state = state

    async def check(self, admission: EmailSyncAdmission) -> EmailSyncAdmissionState:
        del admission
        return self.state


class UnusedEffects:
    async def apply(self, effect: EmailSyncEffect):
        del effect
        raise AssertionError("lifecycle evidence is already durable")


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
        authorization_generation=1,
        configuration_generation=2,
        scope_generation=3,
        allowed_folder_references=(FOLDER_ID,),
        allowed_operations=tuple(EmailProviderOperation),
    )


def message(
    uid: int,
    *,
    source_seed: int | None = None,
    message_id_digest: str | None = None,
    content_sha256: str | None = None,
    last_absence_receipt_id: UUID | None = None,
    uid_validity: int = 101,
) -> YandexMailReconciliationMessage:
    seed = source_seed if source_seed is not None else uid
    return YandexMailReconciliationMessage(
        key=ImapMessageKey(
            workspace_id=WORKSPACE_ID,
            connection_id=CONNECTION_ID,
            mailbox_binding_id=MAILBOX_ID,
            folder_binding_id=FOLDER_ID,
            uid_validity=uid_validity,
            uid=uid,
        ),
        source_object_id=UUID(int=10_000 + seed),
        source_revision_id=UUID(int=20_000 + seed),
        message_id_digest=message_id_digest,
        content_sha256=content_sha256,
        last_absence_receipt_id=last_absence_receipt_id,
    )


class CrashAfterLifecycleSave(InMemoryEmailSyncRepository):
    def __init__(self) -> None:
        super().__init__()
        self.crashed = False

    async def save(self, attempt):
        saved = await super().save(attempt)
        if not self.crashed and any(
            page.page_kind is EmailSyncPageKind.LIFECYCLE_RECONCILIATION
            and page.committed
            for page in saved.pages
        ):
            self.crashed = True
            raise RuntimeError("synthetic crash after lifecycle save")
        return saved


async def service(
    *,
    repository: InMemoryEmailSyncRepository | None = None,
    admission: Admission | None = None,
) -> tuple[YandexMailReconciliationCoordinator, UUID]:
    sync = EmailSyncCoordinator(
        repository=repository or InMemoryEmailSyncRepository(),
        admission=admission or Admission(),
        effects=UnusedEffects(),
        clock=lambda: NOW,
    )
    attempt = await sync.create_attempt(scope(), attempt_key="issue-852")
    prefix = f"imap.folder.{FOLDER_ID.hex}"
    for page in (
        EmailSyncPage(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            page_kind=EmailSyncPageKind.FOLDER_GENERATION,
            page_id=f"{prefix}.generation",
            requested_checkpoint=None,
            publish_checkpoint=False,
            evidence_digest="d" * 64,
            evidence_cursor="imap-generation:v1:101:1000",
            effects=(),
        ),
        EmailSyncPage(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            page_kind=EmailSyncPageKind.FOLDER_COMPLETE,
            page_id=f"{prefix}.complete",
            requested_checkpoint=None,
            publish_checkpoint=False,
            evidence_digest="e" * 64,
            effects=(),
        ),
    ):
        attempt = await sync.record_durable_page(
            attempt.attempt_id,
            page,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            receipts=(),
        )
    return YandexMailReconciliationCoordinator(sync=sync), attempt.attempt_id


def command(
    attempt_id: UUID,
    *,
    coverage: EmailLifecycleCoverage = EmailLifecycleCoverage.COMPLETE_AUTHORIZED,
    previous: tuple[YandexMailReconciliationMessage, ...] = (),
    current: tuple[YandexMailReconciliationMessage, ...] = (),
    scan_id: UUID = SCAN_ID,
    command_scope: MailboxReadScope | None = None,
) -> YandexMailReconciliationCommand:
    selected_scope = command_scope or scope()
    folder_evidence = (
        (
            YandexMailReconciliationFolderEvidence(
                folder_reference=FOLDER_ID,
                generation_page_id=f"imap.folder.{FOLDER_ID.hex}.generation",
                folder_complete_page_id=f"imap.folder.{FOLDER_ID.hex}.complete",
            ),
        )
        if coverage is EmailLifecycleCoverage.COMPLETE_AUTHORIZED
        else ()
    )
    return YandexMailReconciliationCommand(
        scope=selected_scope,
        attempt_id=attempt_id,
        scan_id=scan_id,
        scan_completed_at=NOW,
        coverage=coverage,
        folder_evidence=folder_evidence,
        previous=previous,
        current=current,
    )


@pytest.mark.asyncio
async def test_complete_scan_links_copy_candidate_without_claiming_identity_or_deletion() -> (
    None
):
    coordinator, attempt_id = await service()
    prior = message(1, message_id_digest="a" * 64)
    copied = message(2, message_id_digest="a" * 64)

    result = await coordinator.reconcile(
        command(attempt_id, previous=(prior,), current=(prior, copied))
    )

    candidate = next(
        receipt
        for receipt in result.receipts
        if receipt.outcome is EmailLifecycleOutcome.MOVE_OR_COPY_CANDIDATE
    )
    assert candidate.lineage is EmailLifecycleLineage.DISTINCT_IDENTITY_CANDIDATE
    assert candidate.identity_continuity_proven is False
    assert candidate.tombstone_authorized is False
    assert not any(
        receipt.outcome is EmailLifecycleOutcome.ABSENCE_EVIDENCE
        for receipt in result.receipts
    )


@pytest.mark.asyncio
async def test_move_like_scan_keeps_absence_and_similarity_as_separate_evidence() -> (
    None
):
    coordinator, attempt_id = await service()
    prior = message(1, content_sha256="b" * 64)
    moved = message(2, content_sha256="b" * 64)

    result = await coordinator.reconcile(
        command(attempt_id, previous=(prior,), current=(moved,))
    )

    assert {receipt.outcome for receipt in result.receipts} == {
        EmailLifecycleOutcome.OBSERVED,
        EmailLifecycleOutcome.ABSENCE_EVIDENCE,
        EmailLifecycleOutcome.MOVE_OR_COPY_CANDIDATE,
    }
    absence = next(
        receipt
        for receipt in result.receipts
        if receipt.outcome is EmailLifecycleOutcome.ABSENCE_EVIDENCE
    )
    candidate = next(
        receipt
        for receipt in result.receipts
        if receipt.outcome is EmailLifecycleOutcome.MOVE_OR_COPY_CANDIDATE
    )
    assert absence.authoritative_absence is True
    assert absence.tombstone_authorized is False
    assert candidate.predecessor_receipt_id == absence.receipt_id
    assert candidate.identity_continuity_proven is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("coverage", "outcome"),
    [
        (EmailLifecycleCoverage.PERMISSION_LOSS, EmailLifecycleOutcome.PERMISSION_LOSS),
        (EmailLifecycleCoverage.PROVIDER_OUTAGE, EmailLifecycleOutcome.PROVIDER_OUTAGE),
        (EmailLifecycleCoverage.INCOMPLETE_SCAN, EmailLifecycleOutcome.INCOMPLETE_SCAN),
    ],
)
async def test_unusable_coverage_records_distinct_outcome_without_absence(
    coverage: EmailLifecycleCoverage,
    outcome: EmailLifecycleOutcome,
) -> None:
    admission = Admission()
    coordinator, attempt_id = await service(admission=admission)
    if coverage is EmailLifecycleCoverage.PERMISSION_LOSS:
        admission.state = EmailSyncAdmissionState.DENIED

    result = await coordinator.reconcile(
        command(attempt_id, coverage=coverage, previous=(message(1),), current=())
    )

    assert tuple(receipt.outcome for receipt in result.receipts) == (outcome,)
    assert result.receipts[0].authoritative_absence is False
    assert result.receipts[0].tombstone_authorized is False


@pytest.mark.asyncio
async def test_duplicate_similarity_never_merges_provider_sources() -> None:
    coordinator, attempt_id = await service()
    first = message(1, message_id_digest="c" * 64)
    second = message(2, message_id_digest="c" * 64)

    result = await coordinator.reconcile(command(attempt_id, current=(first, second)))

    observed = [
        receipt
        for receipt in result.receipts
        if receipt.outcome is EmailLifecycleOutcome.OBSERVED
    ]
    assert {receipt.source_object_id for receipt in observed} == {
        first.source_object_id,
        second.source_object_id,
    }
    assert all(receipt.identity_continuity_proven is False for receipt in observed)


@pytest.mark.asyncio
async def test_exact_reappearance_preserves_lineage_and_replay_is_idempotent() -> None:
    coordinator, attempt_id = await service()
    prior = message(1)
    absence = await coordinator.reconcile(
        command(
            attempt_id,
            previous=(prior,),
            scan_id=UUID("90000000-0000-0000-0000-000000000010"),
        )
    )
    absence_id = absence.receipts[0].receipt_id
    prior = message(1, last_absence_receipt_id=absence_id)

    first = await coordinator.reconcile(
        command(attempt_id, previous=(prior,), current=(prior,))
    )
    replay = await coordinator.reconcile(
        command(attempt_id, previous=(prior,), current=(prior,))
    )

    receipt = first.receipts[0]
    assert receipt.outcome is EmailLifecycleOutcome.REAPPEARED
    assert receipt.lineage is EmailLifecycleLineage.PRESERVED_IDENTITY
    assert receipt.identity_continuity_proven is True
    assert receipt.predecessor_receipt_id == absence_id
    assert replay.receipts == first.receipts
    assert replay.attempt.pages == first.attempt.pages
    assert replay.attempt.status is EmailSyncStatus.RUNNING
    assert (
        replay.attempt.pages[-1].page_kind is EmailSyncPageKind.LIFECYCLE_RECONCILIATION
    )


@pytest.mark.asyncio
async def test_forged_reappearance_predecessor_is_rejected() -> None:
    coordinator, attempt_id = await service()
    forged = message(1, last_absence_receipt_id=UUID(int=999))

    with pytest.raises(ValueError, match="predecessor evidence is unavailable"):
        await coordinator.reconcile(
            command(attempt_id, previous=(forged,), current=(forged,))
        )


@pytest.mark.asyncio
async def test_uidvalidity_change_cannot_produce_authoritative_absence() -> None:
    coordinator, attempt_id = await service()

    with pytest.raises(ValueError, match="UIDVALIDITY change"):
        await coordinator.reconcile(
            command(attempt_id, previous=(message(1, uid_validity=202),))
        )


@pytest.mark.asyncio
async def test_attempt_binding_must_exactly_match_command_scope() -> None:
    coordinator, attempt_id = await service()
    mismatched_scope = scope().model_copy(
        update={
            "connection_version_id": UUID(
                "40000000-0000-0000-0000-000000000099"
            )
        }
    )

    with pytest.raises(ValueError, match="does not match its mailbox scope"):
        await coordinator.reconcile(
            command(attempt_id, command_scope=mismatched_scope)
        )


@pytest.mark.asyncio
async def test_permission_loss_is_recorded_after_admission_revocation() -> None:
    admission = Admission()
    coordinator, attempt_id = await service(admission=admission)
    admission.state = EmailSyncAdmissionState.DENIED

    result = await coordinator.reconcile(
        command(
            attempt_id,
            coverage=EmailLifecycleCoverage.PERMISSION_LOSS,
            previous=(message(1),),
        )
    )

    assert result.attempt.status is EmailSyncStatus.SUSPENDED
    assert result.attempt.failure_code == "permission_loss"
    assert result.receipts[0].outcome is EmailLifecycleOutcome.PERMISSION_LOSS


@pytest.mark.asyncio
async def test_revocation_takes_priority_over_claimed_provider_outage() -> None:
    admission = Admission()
    coordinator, attempt_id = await service(admission=admission)
    admission.state = EmailSyncAdmissionState.REVOKED
    reconciliation = command(
        attempt_id, coverage=EmailLifecycleCoverage.PROVIDER_OUTAGE
    )

    result = await coordinator.reconcile(reconciliation)
    replay = await coordinator.reconcile(reconciliation)

    assert result.attempt.status is EmailSyncStatus.SUSPENDED
    assert result.attempt.failure_code == "permission_loss"
    assert result.receipts[0].coverage is EmailLifecycleCoverage.PERMISSION_LOSS
    assert result.receipts[0].outcome is EmailLifecycleOutcome.PERMISSION_LOSS
    assert replay.receipts == result.receipts
    assert replay.attempt.pages == result.attempt.pages


@pytest.mark.asyncio
async def test_stale_generation_blocks_lifecycle_evidence() -> None:
    admission = Admission()
    coordinator, attempt_id = await service(admission=admission)
    admission.state = EmailSyncAdmissionState.STALE_GENERATION

    result = await coordinator.reconcile(
        command(attempt_id, coverage=EmailLifecycleCoverage.PROVIDER_OUTAGE)
    )

    assert result.attempt.status is EmailSyncStatus.RECONCILIATION_REQUIRED
    assert result.attempt.failure_code == "stale_generation"
    assert result.receipts == ()


@pytest.mark.asyncio
async def test_permission_loss_cannot_be_claimed_while_admission_is_allowed() -> None:
    coordinator, attempt_id = await service()

    with pytest.raises(ValueError, match="permission loss requires denied"):
        await coordinator.reconcile(
            command(attempt_id, coverage=EmailLifecycleCoverage.PERMISSION_LOSS)
        )


@pytest.mark.asyncio
async def test_terminal_attempt_never_returns_unpersisted_receipts() -> None:
    coordinator, attempt_id = await service()
    await coordinator.sync.fail(
        attempt_id,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        failure_code="synthetic_failure",
    )

    with pytest.raises(EmailLifecyclePersistenceError):
        await coordinator.reconcile(command(attempt_id, current=(message(1),)))


@pytest.mark.asyncio
async def test_crash_after_durable_save_replays_the_same_receipts() -> None:
    repository = CrashAfterLifecycleSave()
    coordinator, attempt_id = await service(repository=repository)
    reconciliation = command(attempt_id, current=(message(1),))

    with pytest.raises(RuntimeError, match="synthetic crash"):
        await coordinator.reconcile(reconciliation)

    replay = await coordinator.reconcile(reconciliation)

    lifecycle_pages = tuple(
        page
        for page in replay.attempt.pages
        if page.page_kind is EmailSyncPageKind.LIFECYCLE_RECONCILIATION
    )
    assert len(lifecycle_pages) == 1
    assert lifecycle_pages[0].lifecycle_receipts == replay.receipts


@pytest.mark.asyncio
async def test_committed_replay_ignores_later_admission_revocation() -> None:
    admission = Admission()
    coordinator, attempt_id = await service(admission=admission)
    reconciliation = command(attempt_id, current=(message(1),))
    completed = await coordinator.reconcile(reconciliation)
    admission.state = EmailSyncAdmissionState.REVOKED

    replay = await coordinator.reconcile(reconciliation)

    assert replay.receipts == completed.receipts
    assert replay.attempt.status is EmailSyncStatus.RUNNING


@pytest.mark.asyncio
async def test_scan_outcome_must_match_its_coverage() -> None:
    coordinator, attempt_id = await service()
    result = await coordinator.reconcile(
        command(attempt_id, coverage=EmailLifecycleCoverage.PROVIDER_OUTAGE)
    )
    payload = result.receipts[0].model_dump(mode="python")
    payload["coverage"] = EmailLifecycleCoverage.COMPLETE_AUTHORIZED

    with pytest.raises(ValueError, match="outcome must match its coverage"):
        EmailLifecycleReceipt.model_validate(payload)


def test_reconciliation_command_rejects_duplicate_or_mismatched_provider_identity() -> (
    None
):
    duplicate = message(1)
    with pytest.raises(ValueError, match="current message identities must be unique"):
        command(UUID(int=1), current=(duplicate, duplicate))

    wrong_workspace = message(2).model_copy(
        update={
            "key": message(2).key.model_copy(
                update={"workspace_id": UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")}
            )
        }
    )
    with pytest.raises(ValueError, match="exact mailbox scope"):
        command(UUID(int=1), current=(wrong_workspace,))
