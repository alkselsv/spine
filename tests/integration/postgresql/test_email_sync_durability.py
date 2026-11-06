from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from datetime import datetime, timezone
import hashlib
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from alembic import command
from alembic.config import Config

from spine.application.email_sync import (
    EmailSyncAdmission,
    EmailSyncAdmissionState,
    EmailSyncAttempt,
    EmailSyncConcurrentUpdateError,
    EmailSyncCoordinator,
    EmailSyncEffect,
    EmailSyncEffectReceipt,
    EmailSyncPage,
    EmailSyncPageKind,
    EmailSyncPageState,
    EmailSyncStatus,
)
from spine.application.gmail_initial_snapshot import (
    GmailHistoryBaseline,
    GmailHistoryBaselineState,
    GmailInitialSnapshotCommand,
    GmailInitialSnapshotCoordinator,
    GmailSnapshotMessageCommand,
    GmailSnapshotPage,
    gmail_snapshot_effect_key,
)
from spine.application.gmail_history_cursor_recovery import (
    GmailHistoryCursorRecoveryCommand,
    GmailHistoryCursorRecoveryCoordinator,
    GmailHistoryCursorRecoveryProfile,
    GmailHistoryCursorRecoveryState,
)
from spine.connectors.gmail_checkpoints import (
    decode_gmail_history_checkpoint,
    encode_gmail_history_checkpoint,
    encode_gmail_snapshot_page_checkpoint,
)
from spine.connectors.gmail import GmailRawMessageResult, GmailRawMessageStatus
from spine.connectors.gmail_snapshot import GmailCanonicalSnapshotMessageAdapter
from spine.application.connector_ingestion import (
    ConnectorIngestionOrchestrator,
    build_connector_canonical_acceptance_command,
)
from spine.application.diagnostics import AuditEventRegistry
from spine.application.yandex_mail_snapshot import (
    YandexMailInitialSnapshotCommand,
    YandexMailInitialSnapshotCoordinator,
    YandexMailSnapshotFolderCode,
    YandexMailSnapshotFolderDiscovery,
    YandexMailSnapshotFolderGeneration,
    YandexMailSnapshotGenerationCode,
    YandexMailSnapshotMessageMetadata,
    YandexMailSnapshotMessageCommand,
    YandexMailSnapshotUidPage,
    YandexMailSnapshotUidPageCode,
)
from spine.application.yandex_mail_reconciliation import (
    YandexMailReconciliationCommand,
    YandexMailReconciliationCoordinator,
    YandexMailReconciliationFolderEvidence,
    YandexMailReconciliationMessage,
)
from spine.application.yandex_mail_uidvalidity_reset import (
    YandexMailUidValidityResetCommand,
    YandexMailUidValidityResetCoordinator,
    YandexMailUidValidityResetProfile,
    YandexMailUidValidityResetState,
)
from spine.application.persistence import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.application.persistence.canonical import CanonicalAcceptanceCommand
from spine.application.persistence.errors import CanonicalEligibilityError
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.storage.upload import OriginalUploadOrchestrator
from spine.connectors.yandex_mail_message import (
    YandexMailRawMessageFetchCode,
    YandexMailRawMessageFetchResult,
)
from spine.connectors.yandex_mail_snapshot import (
    YandexMailCanonicalSnapshotMessageAdapter,
)
from spine.infrastructure.db.migrations import locate_migration_assets, upgrade_database
from spine.infrastructure.db.operator import bootstrap_database_roles
from spine.infrastructure.db.settings import (
    POSTGRESQL_SEARCH_PATH_OPTIONS,
    MigrationDatabaseSettings,
    OperatorDatabaseSettings,
    TestDatabaseSettings,
)
from spine.infrastructure.db.test_harness import (
    TestDatabaseProvision,
    install_container_marker,
    provision_test_database,
)
from spine.infrastructure.object_storage.contracts import AuthorizedReadGrantRegistry
from spine.infrastructure.object_storage.testing import (
    DeterministicOriginalObjectStoreFake,
)
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.persistence.postgresql import PostgreSQLPersistence
from spine.infrastructure.persistence.postgresql_email_sync import (
    PostgreSQLEmailSyncRepository,
)
from spine.connectors.yandex_mail_incremental import (
    YandexMailIncrementalCheckpoint,
    decode_yandex_mail_incremental_checkpoint,
    encode_yandex_mail_incremental_checkpoint,
)
from spine.connectors.gmail_history import (
    GmailHistoryPageRequest,
    GmailHistoryProviderError,
    GmailHistoryReader,
    GmailHistoryResult,
    GmailHistoryState,
    gmail_history_scope_digest,
)
from spine.connectors.gmail_incremental import GmailIncrementalHistoryApplier
from spine.domain.connections.email_scope import (
    EmailProviderOperation,
    MailboxReadScope,
)
from spine.domain.sources.email_lifecycle import (
    EmailLifecycleCoverage,
    EmailLifecycleLineage,
    EmailLifecycleOutcome,
    EmailLifecycleReceipt,
)
from spine.domain.sources.imap import ImapMessageKey
from tests.contracts.persistence.outbox_events import create_outbox_event_registry
from tests.contracts.persistence.test_canonical_acceptance_contract import (
    _activate_policy as activate_canonical_policy,
)

pytestmark = [pytest.mark.postgresql, pytest.mark.asyncio(loop_scope="session")]
MIGRATION_ROLE = "spine_migration"
RUNTIME_ROLE = "spine_runtime"
DISPATCHER_ROLE = "spine_dispatcher"
MIGRATION_PASSWORD = "migration-email-test"
RUNTIME_PASSWORD = "runtime-email-test"
DISPATCHER_PASSWORD = "dispatcher-email-test"
WORKSPACE_ID = UUID("8a000000-0000-0000-0000-000000000001")
OTHER_WORKSPACE_ID = UUID("8a000000-0000-0000-0000-000000000002")
ENVIRONMENT_ID = UUID("8b000000-0000-0000-0000-000000000001")
OTHER_ENVIRONMENT_ID = UUID("8b000000-0000-0000-0000-000000000002")
SERVICE_ID = UUID("8c000000-0000-0000-0000-000000000001")
_RAW_MESSAGE = b"From: synthetic@example.test\r\n\r\nSynthetic body.\r\n"


def _role_url(provision: TestDatabaseProvision, role: str, password: str) -> SecretStr:
    return SecretStr(
        make_url(provision.url.get_secret_value())
        .set(username=role, password=password)
        .render_as_string(hide_password=False)
    )


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def email_sync_repository(
    postgresql_provision: TestDatabaseProvision,
) -> AsyncIterator[tuple[object, ...]]:
    await bootstrap_database_roles(
        OperatorDatabaseSettings(
            url=postgresql_provision.url,
            database_name=postgresql_provision.target.database_name,
            migration_role=MIGRATION_ROLE,
            migration_password=MIGRATION_PASSWORD,
            runtime_role=RUNTIME_ROLE,
            runtime_password=RUNTIME_PASSWORD,
            dispatcher_role=DISPATCHER_ROLE,
            dispatcher_password=DISPATCHER_PASSWORD,
        )
    )
    migration_url = _role_url(postgresql_provision, MIGRATION_ROLE, MIGRATION_PASSWORD)
    migration_settings = MigrationDatabaseSettings(
        url=migration_url,
        migration_role=MIGRATION_ROLE,
        runtime_role=RUNTIME_ROLE,
        dispatcher_role=DISPATCHER_ROLE,
    )
    await asyncio.to_thread(upgrade_database, migration_settings)
    migration_engine = create_async_engine(
        migration_url.get_secret_value(), hide_parameters=True
    )
    runtime_engine = create_async_engine(
        _role_url(
            postgresql_provision, RUNTIME_ROLE, RUNTIME_PASSWORD
        ).get_secret_value(),
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    async with migration_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO spine.workspaces (id, slug, display_name) VALUES "
                "(:workspace, 'email-sync-a', 'Email Sync A'), "
                "(:other_workspace, 'email-sync-b', 'Email Sync B')"
            ),
            {"workspace": WORKSPACE_ID, "other_workspace": OTHER_WORKSPACE_ID},
        )
        await connection.execute(
            text(
                "INSERT INTO spine.environments (id, workspace_id, kind, display_name) VALUES "
                "(:environment, :workspace, 'development', 'Email Sync'), "
                "(:other_environment, :other_workspace, 'development', 'Email Sync Other')"
            ),
            {
                "environment": ENVIRONMENT_ID,
                "workspace": WORKSPACE_ID,
                "other_environment": OTHER_ENVIRONMENT_ID,
                "other_workspace": OTHER_WORKSPACE_ID,
            },
        )

    sessions = async_sessionmaker(
        runtime_engine, expire_on_commit=False, autoflush=False
    )
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=UUID("8d000000-0000-0000-0000-000000000001"),
        secret=b"synthetic-email-sync-postgresql-context",
    )
    outbox_events = create_outbox_event_registry()
    audit_events = AuditEventRegistry.with_default_families()
    persistence = PostgreSQLPersistence(
        session_factory=sessions,
        context_verifier=boundary,
        outbox_events=outbox_events,
        audit_events=audit_events,
    )

    def repository(
        workspace_id: UUID, environment_id: UUID
    ) -> PostgreSQLEmailSyncRepository:
        def context_factory(operation: PersistenceOperation, trace_id: UUID):
            return boundary.worker(
                scope=EnvironmentScope(
                    workspace_id=workspace_id, environment_id=environment_id
                ),
                service_principal_id=SERVICE_ID,
                purpose=PersistencePurpose("email_sync_test"),
                operation=operation,
                trace_id=trace_id,
            )

        return PostgreSQLEmailSyncRepository(
            persistence.tenant_uow_factory, context_factory
        )

    try:
        yield (
            repository(WORKSPACE_ID, ENVIRONMENT_ID),
            repository(OTHER_WORKSPACE_ID, OTHER_ENVIRONMENT_ID),
            migration_engine,
            migration_settings,
            persistence,
            boundary,
            outbox_events,
            audit_events,
        )
    finally:
        await runtime_engine.dispose()
        async with migration_engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA IF EXISTS spine CASCADE"))
        await migration_engine.dispose()


def _attempt(attempt_id: UUID | None = None) -> EmailSyncAttempt:
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    admission = EmailSyncAdmission(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        connection_id=uuid4(),
        connection_version_id=uuid4(),
        resource_scope_reference=uuid4(),
        mailbox_subject_reference=uuid4(),
        mailbox_binding_id=uuid4(),
        authorization_generation=1,
        configuration_generation=1,
        scope_generation=1,
    )
    return EmailSyncAttempt(
        attempt_id=attempt_id or uuid4(),
        attempt_key=f"attempt-{uuid4().hex}",
        admission=admission,
        status=EmailSyncStatus.PENDING,
        created_at=now,
        updated_at=now,
    )


async def test_postgresql_email_sync_persists_attempt_and_scope_isolation(
    email_sync_repository,
) -> None:
    repository, other_repository, *_ = email_sync_repository
    attempt = _attempt()
    stored = await repository.save(attempt)
    loaded = await repository.get(attempt.attempt_id, WORKSPACE_ID, ENVIRONMENT_ID)

    assert stored == attempt
    assert loaded == attempt
    assert (
        await other_repository.get(
            attempt.attempt_id, OTHER_WORKSPACE_ID, OTHER_ENVIRONMENT_ID
        )
        is None
    )


async def test_postgresql_email_sync_cas_rejects_stale_worker(
    email_sync_repository,
) -> None:
    repository, *_ = email_sync_repository
    original = _attempt()
    await repository.save(original)
    current = await repository.get(original.attempt_id, WORKSPACE_ID, ENVIRONMENT_ID)
    assert current is not None
    advanced = await repository.save(
        current.model_copy(update={"status": EmailSyncStatus.RUNNING})
    )
    assert advanced.revision == current.revision + 1

    with pytest.raises(EmailSyncConcurrentUpdateError):
        await repository.save(
            original.model_copy(update={"status": EmailSyncStatus.FAILED})
        )


async def test_postgresql_email_sync_cas_race_has_one_winner(
    email_sync_repository,
) -> None:
    repository, *_ = email_sync_repository
    attempt = _attempt()
    await repository.save(attempt)
    current = await repository.get(attempt.attempt_id, WORKSPACE_ID, ENVIRONMENT_ID)
    assert current is not None
    start = asyncio.Event()

    async def transition(status: EmailSyncStatus):
        await start.wait()
        return await repository.save(current.model_copy(update={"status": status}))

    first = asyncio.create_task(transition(EmailSyncStatus.RUNNING))
    second = asyncio.create_task(transition(EmailSyncStatus.INCOMPLETE))
    start.set()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)

    assert sum(isinstance(value, EmailSyncAttempt) for value in outcomes) == 1
    assert (
        sum(isinstance(value, EmailSyncConcurrentUpdateError) for value in outcomes)
        == 1
    )
    stored = await repository.get(attempt.attempt_id, WORKSPACE_ID, ENVIRONMENT_ID)
    assert stored is not None
    assert stored.revision == 2


async def test_postgresql_email_sync_cas_rejects_changed_generation(
    email_sync_repository,
) -> None:
    repository, *_ = email_sync_repository
    attempt = _attempt()
    await repository.save(attempt)
    stale_binding = attempt.model_copy(
        update={
            "admission": attempt.admission.model_copy(update={"scope_generation": 2}),
        }
    )

    with pytest.raises(EmailSyncConcurrentUpdateError):
        await repository.save(stale_binding)


async def test_postgresql_email_sync_replays_durable_effect_receipt_after_restart(
    email_sync_repository,
) -> None:
    repository, *_ = email_sync_repository
    attempt = _attempt()
    page = EmailSyncPageState(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        page_id="page-1",
        page_digest="a" * 64,
        requested_checkpoint=None,
        terminal=False,
        effects=(
            EmailSyncEffect(
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
                effect_key="effect-1",
                effect_reference=uuid4(),
            ),
        ),
        effect_receipts=(
            EmailSyncEffectReceipt(
                effect_key="effect-1", receipt_reference="receipt-1", durable=True
            ),
        ),
    )
    await repository.save(attempt.model_copy(update={"pages": (page,)}))

    resumed = await repository.get(attempt.attempt_id, WORKSPACE_ID, ENVIRONMENT_ID)

    assert resumed is not None
    assert resumed.pages == (page,)
    assert resumed.committed_checkpoint is None


async def test_postgresql_email_sync_round_trips_lifecycle_lineage_receipts(
    email_sync_repository,
) -> None:
    repository, other_repository, *_ = email_sync_repository
    attempt = _attempt()
    receipt = EmailLifecycleReceipt(
        receipt_id=UUID("8e000000-0000-0000-0000-000000000001"),
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        scan_id=UUID("8e000000-0000-0000-0000-000000000002"),
        outcome=EmailLifecycleOutcome.MOVE_OR_COPY_CANDIDATE,
        coverage=EmailLifecycleCoverage.COMPLETE_AUTHORIZED,
        lineage=EmailLifecycleLineage.DISTINCT_IDENTITY_CANDIDATE,
        provider_identity="im1:" + "a" * 52,
        candidate_provider_identity="im1:" + "b" * 52,
        source_object_id=UUID("8e000000-0000-0000-0000-000000000003"),
        source_revision_id=UUID("8e000000-0000-0000-0000-000000000004"),
        candidate_source_object_id=UUID("8e000000-0000-0000-0000-000000000005"),
        candidate_source_revision_id=UUID("8e000000-0000-0000-0000-000000000006"),
        similarity_basis="content_sha256",
        similarity_digest="a" * 64,
        identity_continuity_proven=False,
        authoritative_absence=False,
        tombstone_authorized=False,
        observed_at=datetime(2026, 11, 4, tzinfo=timezone.utc),
    )
    page = EmailSyncPageState(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        page_kind=EmailSyncPageKind.LIFECYCLE_RECONCILIATION,
        page_id="lifecycle.scan-1",
        page_digest="d" * 64,
        requested_checkpoint=None,
        terminal=False,
        publish_checkpoint=False,
        effects=(),
        lifecycle_receipts=(receipt,),
        committed=True,
    )
    await repository.save(attempt.model_copy(update={"pages": (page,)}))

    resumed = await repository.get(attempt.attempt_id, WORKSPACE_ID, ENVIRONMENT_ID)

    assert resumed is not None
    assert resumed.pages[0].lifecycle_receipts == (receipt,)
    assert resumed.pages[0].lifecycle_receipts[0].tombstone_authorized is False
    assert (
        await other_repository.get(
            attempt.attempt_id, OTHER_WORKSPACE_ID, OTHER_ENVIRONMENT_ID
        )
        is None
    )


async def test_postgresql_reconciliation_persists_move_evidence_and_replays(
    email_sync_repository,
) -> None:
    repository, *_ = email_sync_repository
    folder = UUID("8e000000-0000-0000-0000-000000000010")
    connection = UUID("8e000000-0000-0000-0000-000000000011")
    mailbox = UUID("8e000000-0000-0000-0000-000000000012")
    scope = MailboxReadScope(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        connection_id=connection,
        connection_version_id=UUID("8e000000-0000-0000-0000-000000000013"),
        provider_kind="yandex_mail",
        resource_scope_reference=UUID("8e000000-0000-0000-0000-000000000014"),
        mailbox_subject_reference=UUID("8e000000-0000-0000-0000-000000000015"),
        mailbox_binding_id=mailbox,
        authorization_generation=1,
        configuration_generation=1,
        scope_generation=1,
        allowed_folder_references=(folder,),
        allowed_operations=tuple(EmailProviderOperation),
    )

    class Admission:
        async def check(self, admission):
            del admission
            return EmailSyncAdmissionState.ALLOWED

    class Effects:
        async def apply(self, effect):
            del effect
            raise AssertionError("reconciliation has no canonical effects")

    sync = EmailSyncCoordinator(
        repository=repository,
        admission=Admission(),
        effects=Effects(),
    )
    attempt = await sync.create_attempt(scope, attempt_key="issue-852-postgresql")
    prefix = f"imap.folder.{folder.hex}"
    for page in (
        EmailSyncPage(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            page_kind=EmailSyncPageKind.FOLDER_GENERATION,
            page_id=f"{prefix}.generation",
            requested_checkpoint=None,
            publish_checkpoint=False,
            evidence_digest="a" * 64,
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
            evidence_digest="b" * 64,
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

    def message(
        uid: int, *, last_absence_receipt_id: UUID | None = None
    ) -> YandexMailReconciliationMessage:
        return YandexMailReconciliationMessage(
            key=ImapMessageKey(
                workspace_id=WORKSPACE_ID,
                connection_id=connection,
                mailbox_binding_id=mailbox,
                folder_binding_id=folder,
                uid_validity=101,
                uid=uid,
            ),
            source_object_id=UUID(int=30_000 + uid),
            source_revision_id=UUID(int=40_000 + uid),
            content_sha256="c" * 64,
            last_absence_receipt_id=last_absence_receipt_id,
        )

    command = YandexMailReconciliationCommand(
        scope=scope,
        attempt_id=attempt.attempt_id,
        scan_id=UUID("8e000000-0000-0000-0000-000000000016"),
        scan_completed_at=datetime(2026, 11, 4, tzinfo=timezone.utc),
        coverage=EmailLifecycleCoverage.COMPLETE_AUTHORIZED,
        folder_evidence=(
            YandexMailReconciliationFolderEvidence(
                folder_reference=folder,
                generation_page_id=f"{prefix}.generation",
                folder_complete_page_id=f"{prefix}.complete",
            ),
        ),
        previous=(message(1),),
        current=(message(2),),
    )
    coordinator = YandexMailReconciliationCoordinator(sync=sync)

    completed = await coordinator.reconcile(command)
    replay = await coordinator.reconcile(command)

    assert completed.receipts == replay.receipts
    assert {receipt.outcome for receipt in replay.receipts} == {
        EmailLifecycleOutcome.OBSERVED,
        EmailLifecycleOutcome.ABSENCE_EVIDENCE,
        EmailLifecycleOutcome.MOVE_OR_COPY_CANDIDATE,
    }
    assert len(
        tuple(
            page
            for page in replay.attempt.pages
            if page.page_kind is EmailSyncPageKind.LIFECYCLE_RECONCILIATION
        )
    ) == 1

    absence = next(
        receipt
        for receipt in completed.receipts
        if receipt.outcome is EmailLifecycleOutcome.ABSENCE_EVIDENCE
    )
    reappeared = message(1, last_absence_receipt_id=absence.receipt_id)
    reappearance_command = command.model_copy(
        update={
            "scan_id": UUID("8e000000-0000-0000-0000-000000000017"),
            "previous": (reappeared,),
            "current": (reappeared,),
        }
    )

    reappearance = await coordinator.reconcile(reappearance_command)

    assert len(reappearance.receipts) == 1
    assert reappearance.receipts[0].outcome is EmailLifecycleOutcome.REAPPEARED
    assert reappearance.receipts[0].predecessor_receipt_id == absence.receipt_id


async def test_postgresql_email_sync_publishes_checkpoint_for_complete_empty_page(
    email_sync_repository,
) -> None:
    repository, *_ = email_sync_repository
    attempt = _attempt()
    await repository.save(attempt)
    current = await repository.get(attempt.attempt_id, WORKSPACE_ID, ENVIRONMENT_ID)
    assert current is not None
    page = EmailSyncPageState(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        page_id="empty-page",
        page_digest="c" * 64,
        requested_checkpoint=None,
        next_checkpoint="opaque-successor",
        terminal=False,
        effects=(),
        committed=True,
    )

    committed = await repository.save(
        current.model_copy(
            update={
                "pages": (page,),
                "committed_checkpoint": "opaque-successor",
                "status": EmailSyncStatus.RUNNING,
            }
        )
    )

    assert committed.committed_checkpoint == "opaque-successor"
    loaded = await repository.get(attempt.attempt_id, WORKSPACE_ID, ENVIRONMENT_ID)
    assert loaded is not None
    assert loaded.committed_checkpoint == "opaque-successor"
    assert loaded.pages == (page,)

    next_page = EmailSyncPageState(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        page_id="empty-page-2",
        page_digest="e" * 64,
        requested_checkpoint="opaque-successor",
        next_checkpoint="opaque-successor-2",
        terminal=False,
        effects=(),
        committed=True,
    )
    advanced = await repository.save(
        loaded.model_copy(
            update={
                "pages": (page, next_page),
                "committed_checkpoint": "opaque-successor-2",
            }
        )
    )

    assert advanced.committed_checkpoint == "opaque-successor-2"
    final = await repository.get(attempt.attempt_id, WORKSPACE_ID, ENVIRONMENT_ID)
    assert final is not None
    assert final.pages == (page, next_page)


async def test_postgresql_email_sync_round_trips_generation_bound_imap_checkpoint(
    email_sync_repository,
) -> None:
    repository, *_ = email_sync_repository
    attempt = _attempt()
    await repository.save(attempt)
    current = await repository.get(attempt.attempt_id, WORKSPACE_ID, ENVIRONMENT_ID)
    assert current is not None
    checkpoint = YandexMailIncrementalCheckpoint(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        connection_id=attempt.admission.connection_id,
        connection_version_id=attempt.admission.connection_version_id,
        mailbox_binding_id=attempt.admission.mailbox_binding_id,
        folder_reference=uuid4(),
        authorization_generation=attempt.admission.authorization_generation,
        configuration_generation=attempt.admission.configuration_generation,
        scope_generation=attempt.admission.scope_generation,
        uid_validity=101,
        next_uid=42,
        baseline_kind="snapshot",
        baseline_receipt_reference="snapshot-receipt-postgresql",
    )
    encoded = encode_yandex_mail_incremental_checkpoint(checkpoint)
    page = EmailSyncPageState(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        page_id="imap-baseline-page",
        page_digest="f" * 64,
        requested_checkpoint=None,
        next_checkpoint=encoded,
        terminal=False,
        effects=(),
        committed=True,
    )

    await repository.save(
        current.model_copy(
            update={
                "pages": (page,),
                "committed_checkpoint": encoded,
                "status": EmailSyncStatus.RUNNING,
            }
        )
    )
    restored = await repository.get(attempt.attempt_id, WORKSPACE_ID, ENVIRONMENT_ID)

    assert restored is not None
    assert restored.pages == (page,)
    assert (
        decode_yandex_mail_incremental_checkpoint(restored.committed_checkpoint or "")
        == checkpoint
    )


async def test_postgresql_email_sync_persists_folder_stages_before_one_aggregate_checkpoint(
    email_sync_repository,
) -> None:
    repository, *_ = email_sync_repository
    attempt = _attempt()
    await repository.save(attempt)
    current = await repository.get(attempt.attempt_id, WORKSPACE_ID, ENVIRONMENT_ID)
    assert current is not None
    generation = EmailSyncPageState(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        page_id="imap.folder.synthetic.generation",
        page_digest="1" * 64,
        requested_checkpoint=None,
        terminal=False,
        publish_checkpoint=False,
        evidence_digest="2" * 64,
        evidence_cursor="imap-generation:v1:101:1",
        effects=(),
        committed=True,
    )
    uid_page = EmailSyncPageState(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        page_id="imap.folder.synthetic.uids.1",
        page_digest="3" * 64,
        requested_checkpoint=None,
        terminal=False,
        publish_checkpoint=False,
        evidence_digest="4" * 64,
        evidence_cursor="terminal",
        effects=(),
        committed=True,
    )
    staged = await repository.save(
        current.model_copy(
            update={
                "pages": (generation, uid_page),
                "status": EmailSyncStatus.RUNNING,
            }
        )
    )

    assert staged.committed_checkpoint is None
    reloaded = await repository.get(attempt.attempt_id, WORKSPACE_ID, ENVIRONMENT_ID)
    assert reloaded is not None
    assert reloaded.pages == (generation, uid_page)
    terminal = EmailSyncPageState(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        page_id="imap.aggregate.synthetic",
        page_digest="5" * 64,
        requested_checkpoint=None,
        next_checkpoint=f"imap-snapshot:v1:{'6' * 64}",
        terminal=True,
        evidence_digest="6" * 64,
        effects=(),
        committed=True,
    )
    completed = await repository.save(
        reloaded.model_copy(
            update={
                "pages": (*reloaded.pages, terminal),
                "committed_checkpoint": terminal.next_checkpoint,
                "status": EmailSyncStatus.COMPLETE,
            }
        )
    )

    assert completed.committed_checkpoint == terminal.next_checkpoint
    final = await repository.get(attempt.attempt_id, WORKSPACE_ID, ENVIRONMENT_ID)
    assert final is not None
    assert sum(page.terminal for page in final.pages) == 1
    assert all(page.committed for page in final.pages)


async def test_postgresql_yandex_snapshot_coordinates_two_empty_folders_and_replays(
    email_sync_repository,
) -> None:
    repository, *_ = email_sync_repository
    first_folder = UUID("8e000000-0000-0000-0000-000000000001")
    second_folder = UUID("8e000000-0000-0000-0000-000000000002")
    scope = MailboxReadScope(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        connection_id=UUID("8f000000-0000-0000-0000-000000000001"),
        connection_version_id=UUID("8f000000-0000-0000-0000-000000000002"),
        provider_kind="yandex_mail",
        resource_scope_reference=UUID("8f000000-0000-0000-0000-000000000003"),
        mailbox_subject_reference=UUID("8f000000-0000-0000-0000-000000000004"),
        mailbox_binding_id=UUID("8f000000-0000-0000-0000-000000000005"),
        authorization_generation=1,
        configuration_generation=1,
        scope_generation=1,
        allowed_folder_references=(first_folder, second_folder),
        allowed_operations=tuple(EmailProviderOperation),
    )

    class Admission:
        async def check(self, admission):
            del admission
            return EmailSyncAdmissionState.ALLOWED

    class Effects:
        async def apply(self, effect):
            del effect
            raise AssertionError("empty folders have no canonical message effect")

    class EmptyFolderReader:
        calls = 0

        async def discover_folders(self, mailbox_scope):
            self.calls += 1
            return YandexMailSnapshotFolderDiscovery(
                workspace_id=mailbox_scope.workspace_id,
                environment_id=mailbox_scope.environment_id,
                complete=True,
                code=YandexMailSnapshotFolderCode.COMPLETE,
                discovered_count=2,
                approved_count=2,
                folders=(first_folder, second_folder),
            )

        async def observe_folder_generation(self, mailbox_scope, folder_reference):
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
                uid_validity=101,
                uid_next=1,
                read_only=True,
            )

        async def enumerate_uid_page(self, request):
            mailbox_scope = request.scope
            return YandexMailSnapshotUidPage(
                complete=True,
                code=YandexMailSnapshotUidPageCode.COMPLETE,
                workspace_id=mailbox_scope.workspace_id,
                environment_id=mailbox_scope.environment_id,
                connection_id=mailbox_scope.connection_id,
                connection_version_id=mailbox_scope.connection_version_id,
                mailbox_binding_id=mailbox_scope.mailbox_binding_id,
                folder_reference=request.folder_reference,
                authorization_generation=mailbox_scope.authorization_generation,
                configuration_generation=mailbox_scope.configuration_generation,
                scope_generation=mailbox_scope.scope_generation,
                uid_validity=request.uid_validity,
                covered_uid_range=None,
            )

    class NoMessages:
        async def apply(self, message):
            del message
            raise AssertionError("empty folders have no messages")

    reader = EmptyFolderReader()
    coordinator = YandexMailInitialSnapshotCoordinator(
        sync=EmailSyncCoordinator(
            repository=repository,
            admission=Admission(),
            effects=Effects(),
        ),
        reader=reader,
        messages=NoMessages(),
    )
    snapshot_command = YandexMailInitialSnapshotCommand(
        scope=scope,
        attempt_key="issue-849-postgresql-empty-folders",
        uid_page_size=10,
        uid_page_response_bytes=4096,
        max_uid_pages_per_folder=2,
        max_message_bytes=4096,
        access_policy_version_id=UUID("8f000000-0000-0000-0000-000000000006"),
    )

    completed = await coordinator.run(snapshot_command)
    replay = await coordinator.run(snapshot_command)

    assert completed.attempt.status is EmailSyncStatus.COMPLETE
    assert completed.completed_folders == 2
    assert completed.completed_uid_pages == 2
    assert sum(page.terminal for page in completed.attempt.pages) == 1
    assert replay.attempt.attempt_id == completed.attempt.attempt_id
    assert replay.attempt.status is EmailSyncStatus.COMPLETE
    assert replay.attempt.committed_checkpoint == completed.attempt.committed_checkpoint
    assert {page.page_id for page in replay.attempt.pages} == {
        page.page_id for page in completed.attempt.pages
    }
    assert reader.calls == 1


class _SnapshotAdmission:
    def __init__(self, *, stale_after: int | None = None) -> None:
        self.calls = 0
        self.stale_after = stale_after

    async def check(self, admission):
        del admission
        self.calls += 1
        if self.stale_after is not None and self.calls > self.stale_after:
            return EmailSyncAdmissionState.STALE_GENERATION
        return EmailSyncAdmissionState.ALLOWED


class _SnapshotEffects:
    async def apply(self, effect):
        del effect
        raise AssertionError("scripted empty UID coverage has no message effects")


class _BlockingResetRaceEffects:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def apply(self, effect: EmailSyncEffect) -> EmailSyncEffectReceipt:
        self.started.set()
        await self.release.wait()
        return EmailSyncEffectReceipt(
            effect_key=effect.effect_key,
            receipt_reference="stale-effect-receipt",
            durable=True,
        )


class _ScriptedImapSnapshotReader:
    def __init__(
        self,
        folders: tuple[UUID, ...],
        *,
        partial_discovery: bool = False,
        page_failure: YandexMailSnapshotUidPageCode | None = None,
        message_uid: int | None = None,
        uid_validity: int = 101,
    ) -> None:
        self.folders = folders
        self.partial_discovery = partial_discovery
        self.page_failure = page_failure
        self.message_uid = message_uid
        self.uid_validity = uid_validity
        self.page_calls = 0

    async def discover_folders(self, mailbox_scope):
        return YandexMailSnapshotFolderDiscovery(
            workspace_id=mailbox_scope.workspace_id,
            environment_id=mailbox_scope.environment_id,
            complete=not self.partial_discovery,
            code=(
                YandexMailSnapshotFolderCode.PARTIAL_RESPONSE
                if self.partial_discovery
                else YandexMailSnapshotFolderCode.COMPLETE
            ),
            discovered_count=len(self.folders),
            approved_count=len(self.folders),
            folders=self.folders if not self.partial_discovery else (),
        )

    async def observe_folder_generation(self, mailbox_scope, folder_reference):
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
            uid_validity=self.uid_validity,
            uid_next=1 if self.message_uid is None else self.message_uid + 1,
            read_only=True,
        )

    async def enumerate_uid_page(self, request):
        self.page_calls += 1
        failure = self.page_failure
        self.page_failure = None
        code = failure or YandexMailSnapshotUidPageCode.COMPLETE
        return YandexMailSnapshotUidPage(
            complete=failure is None,
            code=code,
            workspace_id=request.scope.workspace_id,
            environment_id=request.scope.environment_id,
            connection_id=request.scope.connection_id,
            connection_version_id=request.scope.connection_version_id,
            mailbox_binding_id=request.scope.mailbox_binding_id,
            folder_reference=request.folder_reference,
            authorization_generation=request.scope.authorization_generation,
            configuration_generation=request.scope.configuration_generation,
            scope_generation=request.scope.scope_generation,
            uid_validity=(
                request.uid_validity + 1
                if code is YandexMailSnapshotUidPageCode.UIDVALIDITY_CHANGED
                else request.uid_validity
            ),
            covered_uid_range=(
                None
                if failure is not None or self.message_uid is None
                else (request.start_uid, request.end_uid)
            ),
            messages=(
                ()
                if failure is not None or self.message_uid is None
                else (
                    YandexMailSnapshotMessageMetadata(
                        uid=self.message_uid,
                        size_octets=len(_RAW_MESSAGE),
                    ),
                )
            ),
        )


class _UnusedObservationReader:
    async def read(self, command, context):
        del command, context
        raise AssertionError("the message adapter supplies its observation reader")


class _RawMessageAdmission:
    def __init__(self, scope: MailboxReadScope, folder: UUID) -> None:
        self.result = YandexMailRawMessageFetchResult(
            code=YandexMailRawMessageFetchCode.COMPLETE,
            complete=True,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            connection_id=scope.connection_id,
            connection_version_id=scope.connection_version_id,
            mailbox_binding_id=scope.mailbox_binding_id,
            folder_reference=folder,
            authorization_generation=scope.authorization_generation,
            configuration_generation=scope.configuration_generation,
            scope_generation=scope.scope_generation,
            uid_validity=101,
            uid=1,
            raw_bytes=_RAW_MESSAGE,
            content_length=len(_RAW_MESSAGE),
            content_sha256=hashlib.sha256(_RAW_MESSAGE).hexdigest(),
            declared_octets=len(_RAW_MESSAGE),
            envelope=b'("Synthetic")',
            internal_date=datetime(2026, 11, 3, tzinfo=timezone.utc),
            fetch_data_item="BODY.PEEK[]",
            seen_preserved=True,
            mutation_evidence_complete=True,
        )

    async def read(self, command, context):
        del command, context
        return self.result


class _GmailRawMessageAdmission:
    def __init__(self, scope: MailboxReadScope, label: UUID) -> None:
        self.result = GmailRawMessageResult(
            status=GmailRawMessageStatus.ACCEPTED,
            mailbox_scope=scope,
            requested_message_id="message-a",
            authorized_label_reference=label,
            message_id="message-a",
            raw_mime=_RAW_MESSAGE,
            sha256=hashlib.sha256(_RAW_MESSAGE).hexdigest(),
            byte_length=len(_RAW_MESSAGE),
            observed_label_references=(label,),
            internal_date_ms=1_762_128_000_000,
            history_id="101",
            thread_id="thread-a",
        )

    async def read(self, command, context):
        del command, context
        return self.result


class _CrashAfterCanonicalAcceptance:
    def __init__(self, messages, *, process_crash: bool = False) -> None:
        self.messages = messages
        self.receipts = []
        self.errors = []
        self.crashed = False
        self.process_crash = process_crash

    async def apply(self, command):
        try:
            receipt = await self.messages.apply(command)
        except Exception as error:
            self.errors.append((repr(error), repr(error.__cause__)))
            raise
        self.receipts.append(receipt)
        if not self.crashed:
            self.crashed = True
            if self.process_crash:
                raise _PostgreSQLProcessCrash(
                    "synthetic process crash after canonical acceptance"
                )
            raise RuntimeError("synthetic crash after canonical acceptance")
        return receipt


async def _provision_eligible_canonical_message_source(
    *,
    messages,
    command,
    migration_engine,
    persistence,
    context_factory,
    outbox_events,
    activation_number: int = 84900,
) -> UUID | None:
    """Prepare source/policy prerequisites without adding crash-test assertions."""

    try:
        await messages.apply(command)
    except CanonicalEligibilityError:
        pass
    else:
        return None
    async with migration_engine.connect() as connection:
        source_object_id = await connection.scalar(
            text(
                "SELECT source_object_id FROM spine.source_objects "
                "WHERE workspace_id = :workspace AND environment_id = :environment "
                "AND connection_id = :connection"
            ),
            {
                "workspace": WORKSPACE_ID,
                "environment": ENVIRONMENT_ID,
                "connection": command.scope.connection_id,
            },
        )
    if not isinstance(source_object_id, UUID):
        raise RuntimeError("canonical message source was not provisioned")
    policy_context = context_factory(
        PersistenceOperation("access_policy_activation"),
        UUID("9d000000-0000-0000-0000-000000000004"),
    )
    async with persistence.uow_factory(policy_context) as uow:
        source_object = await uow.sources.resolve_source(source_object_id)
        await uow.rollback()
    if source_object is None:
        raise RuntimeError("canonical message source was not persisted")
    return await activate_canonical_policy(
        SimpleNamespace(
            uow_factory=persistence.uow_factory,
            outbox_events=outbox_events,
        ),
        context=policy_context,
        source_object=source_object,
        number=activation_number,
    )


class _CrashAfterPostgreSQLSave:
    def __init__(self, repository, page_kind: EmailSyncPageKind) -> None:
        self.repository = repository
        self.page_kind = page_kind
        self.crashed = False

    async def get(self, attempt_id, workspace_id, environment_id):
        return await self.repository.get(attempt_id, workspace_id, environment_id)

    async def save(self, attempt):
        saved = await self.repository.save(attempt)
        if not self.crashed and any(
            page.page_kind is self.page_kind and page.committed for page in saved.pages
        ):
            self.crashed = True
            raise RuntimeError(f"synthetic crash after {self.page_kind.value}")
        return saved

    async def fence_for_reconciliation(
        self, attempt: EmailSyncAttempt, *, failure_code: str
    ) -> EmailSyncAttempt:
        return await self.repository.fence_for_reconciliation(
            attempt,
            failure_code=failure_code,
        )


def _postgresql_snapshot(
    repository,
    *,
    attempt_key: str,
    reader: _ScriptedImapSnapshotReader,
    admission: _SnapshotAdmission | None = None,
    messages=None,
):
    folders = reader.folders
    scope = MailboxReadScope(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        connection_id=UUID("9f000000-0000-0000-0000-000000000001"),
        connection_version_id=UUID("9f000000-0000-0000-0000-000000000002"),
        provider_kind="yandex_mail",
        resource_scope_reference=UUID("9f000000-0000-0000-0000-000000000003"),
        mailbox_subject_reference=UUID("9f000000-0000-0000-0000-000000000004"),
        mailbox_binding_id=UUID("9f000000-0000-0000-0000-000000000005"),
        authorization_generation=1,
        configuration_generation=1,
        scope_generation=1,
        allowed_folder_references=folders,
        allowed_operations=tuple(EmailProviderOperation),
    )
    coordinator = YandexMailInitialSnapshotCoordinator(
        sync=EmailSyncCoordinator(
            repository=repository,
            admission=admission or _SnapshotAdmission(),
            effects=_SnapshotEffects(),
        ),
        reader=reader,
        messages=messages or _SnapshotEffects(),
    )
    return coordinator, YandexMailInitialSnapshotCommand(
        scope=scope,
        attempt_key=attempt_key,
        uid_page_size=10,
        uid_page_response_bytes=4096,
        max_uid_pages_per_folder=2,
        max_message_bytes=4096,
        access_policy_version_id=UUID("9f000000-0000-0000-0000-000000000006"),
    )


async def test_postgresql_scripted_imap_partial_folder_coverage_stays_incomplete(
    email_sync_repository,
) -> None:
    repository, *_ = email_sync_repository
    folder = UUID("9e000000-0000-0000-0000-000000000001")
    reader = _ScriptedImapSnapshotReader((folder,), partial_discovery=True)
    coordinator, command = _postgresql_snapshot(
        repository,
        attempt_key="issue-849-postgresql-partial",
        reader=reader,
    )

    result = await coordinator.run(command)

    assert result.attempt.status is EmailSyncStatus.INCOMPLETE
    assert result.attempt.committed_checkpoint is None
    assert result.attempt.pages == ()


@pytest.mark.parametrize(
    ("code", "expected_status"),
    (
        (YandexMailSnapshotUidPageCode.SERVER_REJECTED, EmailSyncStatus.COMPLETE),
        (
            YandexMailSnapshotUidPageCode.UIDVALIDITY_CHANGED,
            EmailSyncStatus.RECONCILIATION_REQUIRED,
        ),
    ),
)
async def test_postgresql_scripted_imap_reconnect_and_reset_are_durable(
    email_sync_repository,
    code: YandexMailSnapshotUidPageCode,
    expected_status: EmailSyncStatus,
) -> None:
    repository, *_ = email_sync_repository
    folder = UUID("9e000000-0000-0000-0000-000000000002")
    reader = _ScriptedImapSnapshotReader((folder,), page_failure=code)
    coordinator, command = _postgresql_snapshot(
        repository,
        attempt_key=f"issue-849-postgresql-{code.value}",
        reader=reader,
    )

    first = await coordinator.run(command)
    result = await coordinator.run(command)

    assert first.attempt.committed_checkpoint is None
    assert result.attempt.status is expected_status
    if code is YandexMailSnapshotUidPageCode.SERVER_REJECTED:
        assert reader.page_calls == 2
        assert sum(page.terminal for page in result.attempt.pages) == 1
    else:
        assert not any(page.terminal for page in result.attempt.pages)


async def test_postgresql_scripted_imap_stale_generation_fences_checkpoint(
    email_sync_repository,
) -> None:
    repository, *_ = email_sync_repository
    folder = UUID("9e000000-0000-0000-0000-000000000003")
    reader = _ScriptedImapSnapshotReader((folder,))
    coordinator, command = _postgresql_snapshot(
        repository,
        attempt_key="issue-849-postgresql-stale",
        reader=reader,
        admission=_SnapshotAdmission(stale_after=4),
    )

    result = await coordinator.run(command)

    assert result.attempt.status is EmailSyncStatus.RECONCILIATION_REQUIRED
    assert result.attempt.committed_checkpoint is None
    assert not any(page.terminal and page.committed for page in result.attempt.pages)


def _uidvalidity_reset_command(
    snapshot: YandexMailInitialSnapshotCommand,
    *,
    stale_attempt_id: UUID,
) -> YandexMailUidValidityResetCommand:
    return YandexMailUidValidityResetCommand(
        scope=snapshot.scope,
        stale_attempt_id=stale_attempt_id,
        folder_reference=snapshot.scope.allowed_folder_references[0],
        stale_uid_validity=101,
        observed_uid_validity=102,
    )


def _uidvalidity_reset_profile(
    snapshot: YandexMailInitialSnapshotCommand,
) -> YandexMailUidValidityResetProfile:
    return YandexMailUidValidityResetProfile(
        uid_page_size=snapshot.uid_page_size,
        uid_page_response_bytes=snapshot.uid_page_response_bytes,
        max_uid_pages_per_folder=snapshot.max_uid_pages_per_folder,
        max_message_bytes=snapshot.max_message_bytes,
        access_policy_version_id=snapshot.access_policy_version_id,
    )


async def test_postgresql_uidvalidity_reset_replay_has_one_successor_generation(
    email_sync_repository,
) -> None:
    repository, *_ = email_sync_repository
    folder = UUID("9e000000-0000-0000-0000-000000000020")
    reader = _ScriptedImapSnapshotReader((folder,), uid_validity=102)
    snapshot, snapshot_command = _postgresql_snapshot(
        repository,
        attempt_key="issue-851-unused-snapshot-key",
        reader=reader,
    )
    stale = await snapshot.sync.create_attempt(
        snapshot_command.scope,
        attempt_key="issue-851-stale-incremental",
    )
    reset = YandexMailUidValidityResetCoordinator(
        sync=snapshot.sync,
        reconciliation=snapshot,
        profile=_uidvalidity_reset_profile(snapshot_command),
    )
    command = _uidvalidity_reset_command(
        snapshot_command,
        stale_attempt_id=stale.attempt_id,
    )

    first = await reset.reconcile(command)
    replay = await reset.reconcile(command)
    fenced = await snapshot.sync.get_attempt(
        stale.attempt_id,
        workspace_id=snapshot_command.scope.workspace_id,
        environment_id=snapshot_command.scope.environment_id,
    )
    successor = await snapshot.sync.get_attempt(
        first.reconciliation_attempt_id,
        workspace_id=snapshot_command.scope.workspace_id,
        environment_id=snapshot_command.scope.environment_id,
    )

    assert first.state is YandexMailUidValidityResetState.COMPLETE
    assert replay == first
    assert fenced.status is EmailSyncStatus.RECONCILIATION_REQUIRED
    assert fenced.committed_checkpoint is None
    assert successor.status is EmailSyncStatus.COMPLETE
    assert sum(page.terminal for page in successor.pages) == 1
    assert reader.page_calls == 1


async def test_postgresql_concurrent_duplicate_reset_signals_share_successor(
    email_sync_repository,
) -> None:
    repository, *_ = email_sync_repository
    folder = UUID("9e000000-0000-0000-0000-000000000023")
    reader = _ScriptedImapSnapshotReader((folder,), uid_validity=102)
    snapshot, snapshot_command = _postgresql_snapshot(
        repository,
        attempt_key="issue-851-unused-concurrent",
        reader=reader,
    )
    stale = await snapshot.sync.create_attempt(
        snapshot_command.scope,
        attempt_key="issue-851-stale-concurrent",
    )
    reset = YandexMailUidValidityResetCoordinator(
        sync=snapshot.sync,
        reconciliation=snapshot,
        profile=_uidvalidity_reset_profile(snapshot_command),
    )
    command = _uidvalidity_reset_command(
        snapshot_command,
        stale_attempt_id=stale.attempt_id,
    )

    first, duplicate = await asyncio.gather(
        reset.reconcile(command),
        reset.reconcile(command),
    )
    replay = await reset.reconcile(command)

    assert first.reset_id == duplicate.reset_id
    assert first.reconciliation_attempt_id == duplicate.reconciliation_attempt_id
    assert first.reconciliation_attempt_id is not None
    assert replay.reconciliation_attempt_id == first.reconciliation_attempt_id
    assert {
        first.state,
        duplicate.state,
    } <= {
        YandexMailUidValidityResetState.RECONCILING,
        YandexMailUidValidityResetState.COMPLETE,
    }


async def test_postgresql_uidvalidity_reset_during_stale_effect_holds_cursor(
    email_sync_repository,
) -> None:
    repository, *_ = email_sync_repository
    folder = UUID("9e000000-0000-0000-0000-000000000021")
    reader = _ScriptedImapSnapshotReader((folder,), uid_validity=102)
    race_effects = _BlockingResetRaceEffects()
    fence_time = datetime(2026, 11, 4, 12, 30, tzinfo=timezone.utc)
    sync = EmailSyncCoordinator(
        repository=repository,
        admission=_SnapshotAdmission(),
        effects=race_effects,
        clock=lambda: fence_time,
    )
    snapshot = YandexMailInitialSnapshotCoordinator(
        sync=sync,
        reader=reader,
        messages=_SnapshotEffects(),
    )
    _, snapshot_command = _postgresql_snapshot(
        repository,
        attempt_key="issue-851-unused-race",
        reader=reader,
    )
    stale = await sync.create_attempt(
        snapshot_command.scope,
        attempt_key="issue-851-stale-effect-race",
    )
    stale_page = EmailSyncPage(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        page_id="stale-page-in-flight",
        requested_checkpoint=None,
        next_checkpoint="must-not-advance",
        effects=(
            EmailSyncEffect(
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
                effect_key="stale.effect.in.flight",
                effect_reference=UUID("9e000000-0000-0000-0000-000000000022"),
            ),
        ),
    )
    stale_worker = asyncio.create_task(
        sync.apply_page(
            stale.attempt_id,
            stale_page,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
        )
    )
    await race_effects.started.wait()
    reset = YandexMailUidValidityResetCoordinator(
        sync=sync,
        reconciliation=snapshot,
        profile=_uidvalidity_reset_profile(snapshot_command),
    )
    command = _uidvalidity_reset_command(
        snapshot_command,
        stale_attempt_id=stale.attempt_id,
    )

    recovered = await reset.reconcile(command)
    race_effects.release.set()
    with pytest.raises(EmailSyncConcurrentUpdateError):
        await stale_worker
    fenced = await sync.get_attempt(
        stale.attempt_id,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
    )

    assert recovered.state is YandexMailUidValidityResetState.COMPLETE
    assert fenced.status is EmailSyncStatus.RECONCILIATION_REQUIRED
    assert fenced.committed_checkpoint is None
    assert fenced.updated_at == fence_time
    assert all(not page.effect_receipts for page in fenced.pages)


@pytest.mark.parametrize(
    "page_kind",
    (
        EmailSyncPageKind.FOLDER_GENERATION,
        EmailSyncPageKind.UID_PAGE,
        EmailSyncPageKind.AGGREGATE_CHECKPOINT,
    ),
)
async def test_postgresql_uidvalidity_reset_crash_replays_bounded_reconciliation(
    email_sync_repository,
    page_kind: EmailSyncPageKind,
) -> None:
    repository, *_ = email_sync_repository
    folder = UUID(
        int=UUID("9e000000-0000-0000-0000-000000000030").int
        + list(EmailSyncPageKind).index(page_kind)
    )
    reader = _ScriptedImapSnapshotReader((folder,), uid_validity=102)
    crashing = _CrashAfterPostgreSQLSave(repository, page_kind)
    snapshot, snapshot_command = _postgresql_snapshot(
        crashing,
        attempt_key=f"issue-851-unused-{page_kind.value}",
        reader=reader,
    )
    stale = await snapshot.sync.create_attempt(
        snapshot_command.scope,
        attempt_key=f"issue-851-stale-{page_kind.value}",
    )
    reset = YandexMailUidValidityResetCoordinator(
        sync=snapshot.sync,
        reconciliation=snapshot,
        profile=_uidvalidity_reset_profile(snapshot_command),
    )
    command = _uidvalidity_reset_command(
        snapshot_command,
        stale_attempt_id=stale.attempt_id,
    )

    with pytest.raises(RuntimeError, match=page_kind.value):
        await reset.reconcile(command)
    recovered = await reset.reconcile(command)
    fenced = await snapshot.sync.get_attempt(
        stale.attempt_id,
        workspace_id=snapshot_command.scope.workspace_id,
        environment_id=snapshot_command.scope.environment_id,
    )

    assert recovered.state is YandexMailUidValidityResetState.COMPLETE
    assert fenced.status is EmailSyncStatus.RECONCILIATION_REQUIRED
    assert fenced.committed_checkpoint is None


async def test_postgresql_scripted_imap_item_crash_replays_one_canonical_effect(
    email_sync_repository,
) -> None:
    (
        repository,
        _,
        migration_engine,
        _,
        persistence,
        boundary,
        outbox_events,
        audit_events,
    ) = email_sync_repository
    folder = UUID("9e000000-0000-0000-0000-000000000004")
    reader = _ScriptedImapSnapshotReader((folder,), message_uid=1)
    _, snapshot_command = _postgresql_snapshot(
        repository,
        attempt_key="issue-849-postgresql-item-crash",
        reader=reader,
    )

    active_policy_id: UUID | None = None

    def context_factory(
        operation: PersistenceOperation, trace_id: UUID
    ) -> TrustedPersistenceContext:
        return boundary.worker(
            scope=EnvironmentScope(
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
            ),
            service_principal_id=SERVICE_ID,
            purpose=PersistencePurpose(
                "connector.ingestion.v1"
                if operation.value == "original.upload"
                else "contract_test"
            ),
            operation=operation,
            trace_id=trace_id,
        )

    def canonical_command_factory(**values):
        built = build_connector_canonical_acceptance_command(
            outbox_events=outbox_events,
            audit_events=audit_events,
            **values,
        )
        return CanonicalAcceptanceCommand.create(
            source=built.source,
            revision=built.revision,
            provenance=built.provenance,
            admission_decision_id=built.admission_decision_id,
            access_policy_version_id=active_policy_id,
            expected_current_revision_id=built.expected_current_revision_id,
            idempotency_key=built.idempotency_key,
            canonical_acceptance_id=built.canonical_acceptance_id,
            audit_event_id=built.audit_event_id,
            outbox_event_id=built.outbox_event_id,
            accepted_at=built.accepted_at,
            audit_event=built.audit_event,
            outbox_intent=built.outbox_intent,
            lifecycle_query=built.lifecycle_query,
            processing_admission=built.processing_admission,
            operation=built.digest.operation,
        )

    ingestion = ConnectorIngestionOrchestrator(
        uow_factory=persistence.uow_factory,
        observation_reader=_UnusedObservationReader(),
        uploader=OriginalUploadOrchestrator(
            uow_factory=persistence.uow_factory,
            object_store=DeterministicOriginalObjectStoreFake(
                grant_registry=AuthorizedReadGrantRegistry(),
                generation_factory=lambda: UUID("9d000000-0000-0000-0000-000000000003"),
                clock=lambda: datetime(2026, 11, 3, tzinfo=timezone.utc),
                max_chunk_bytes=4096,
                max_object_bytes=8192,
                read_chunk_bytes=64,
            ),
            identity_factory=iter(
                (
                    UUID("9d000000-0000-0000-0000-000000000001"),
                    UUID("9d000000-0000-0000-0000-000000000002"),
                )
            ).__next__,
        ),
        read_context_factory=context_factory,
        context_factory=context_factory,
        canonical_command_factory=canonical_command_factory,
        outbox_events=outbox_events,
        audit_events=audit_events,
        read_operation=PersistenceOperation("mailbox.read_raw_message"),
        admission_reference_type="mailbox_read_admission",
        clock=lambda: datetime(2026, 11, 3, tzinfo=timezone.utc),
    )
    durable_messages = YandexMailCanonicalSnapshotMessageAdapter(
        ingestion=ingestion,
        raw_messages=_RawMessageAdmission(snapshot_command.scope, folder),
    )
    effect_key = f"imap.message.{folder.hex}.101.1"
    message_command = YandexMailSnapshotMessageCommand(
        scope=snapshot_command.scope,
        folder_reference=folder,
        uid_validity=101,
        uid=1,
        size_octets=len(_RAW_MESSAGE),
        max_message_bytes=snapshot_command.max_message_bytes,
        access_policy_version_id=snapshot_command.access_policy_version_id,
        effect_key=effect_key,
    )
    active_policy_id = await _provision_eligible_canonical_message_source(
        messages=durable_messages,
        command=message_command,
        migration_engine=migration_engine,
        persistence=persistence,
        context_factory=context_factory,
        outbox_events=outbox_events,
    )
    crashing_messages = _CrashAfterCanonicalAcceptance(durable_messages)
    coordinator, snapshot_command = _postgresql_snapshot(
        repository,
        attempt_key="issue-849-postgresql-item-crash",
        reader=reader,
        messages=crashing_messages,
    )

    interrupted = await coordinator.run(snapshot_command)
    recovered = await coordinator.run(snapshot_command)

    assert interrupted.attempt.status is EmailSyncStatus.INCOMPLETE
    assert recovered.attempt.status is EmailSyncStatus.COMPLETE, (
        recovered.attempt.failure_code,
        crashing_messages.receipts,
        crashing_messages.errors,
    )
    assert recovered.durable_message_effects == 1
    assert len(crashing_messages.receipts) == 2
    assert (
        crashing_messages.receipts[0].canonical_acceptance_id
        == crashing_messages.receipts[1].canonical_acceptance_id
    )
    uid_pages = tuple(
        page
        for page in recovered.attempt.pages
        if page.page_kind is EmailSyncPageKind.UID_PAGE
    )
    assert len(uid_pages) == 1
    assert len(uid_pages[0].effect_receipts) == 1
    assert sum(page.terminal for page in recovered.attempt.pages) == 1
    async with migration_engine.connect() as connection:
        acceptance_count = await connection.scalar(
            text(
                "SELECT count(*) FROM spine.canonical_acceptances "
                "WHERE workspace_id = :workspace AND environment_id = :environment"
            ),
            {"workspace": WORKSPACE_ID, "environment": ENVIRONMENT_ID},
        )
    assert acceptance_count == 1


@pytest.mark.parametrize(
    "page_kind",
    (
        EmailSyncPageKind.FOLDER_GENERATION,
        EmailSyncPageKind.UID_PAGE,
        EmailSyncPageKind.FOLDER_COMPLETE,
        EmailSyncPageKind.AGGREGATE_CHECKPOINT,
    ),
)
async def test_postgresql_scripted_imap_crash_replays_one_terminal_checkpoint(
    email_sync_repository,
    page_kind: EmailSyncPageKind,
) -> None:
    repository, *_ = email_sync_repository
    folder = UUID(
        int=UUID("9e000000-0000-0000-0000-000000000010").int
        + list(EmailSyncPageKind).index(page_kind)
    )
    reader = _ScriptedImapSnapshotReader((folder,))
    crashing = _CrashAfterPostgreSQLSave(repository, page_kind)
    coordinator, command = _postgresql_snapshot(
        crashing,
        attempt_key=f"issue-849-postgresql-crash-{page_kind.value}",
        reader=reader,
    )

    with pytest.raises(RuntimeError, match=page_kind.value):
        await coordinator.run(command)
    recovered = await coordinator.run(command)

    assert recovered.attempt.status is EmailSyncStatus.COMPLETE, (
        recovered.attempt.failure_code,
        crashing_messages.receipts,
        crashing_messages.errors,
    )
    assert sum(page.terminal for page in recovered.attempt.pages) == 1


class _PostgreSQLGmailReader:
    def __init__(
        self, scope: MailboxReadScope, *, message_ids: tuple[str, ...] = ()
    ) -> None:
        self.scope = scope
        self.message_ids = message_ids
        self.second_page = encode_gmail_snapshot_page_checkpoint(scope, "second-page")

    async def capture_history_baseline(self, mailbox_scope):
        assert mailbox_scope == self.scope
        return GmailHistoryBaseline(
            state=GmailHistoryBaselineState.CAPTURED,
            admission_scope_digest=gmail_history_scope_digest(self.scope),
            checkpoint=encode_gmail_history_checkpoint(self.scope, "100"),
        )

    async def enumerate_snapshot_page(self, request):
        if request.checkpoint is None:
            return GmailSnapshotPage(
                requested_checkpoint=None,
                message_ids=self.message_ids,
                next_checkpoint=self.second_page,
            )
        return GmailSnapshotPage(
            requested_checkpoint=self.second_page,
            next_checkpoint=None,
        )

    def history_result(
        self,
        start_history_id,
    ):
        return GmailHistoryResult(
            requested_start_history_id=start_history_id,
            admission_scope_digest=gmail_history_scope_digest(self.scope),
            state=GmailHistoryState.COMPLETE,
            terminal_history_id="101",
            pages_read=1,
            duplicate_count=0,
            malformed_count=0,
        )


class _PostgreSQLGmailHistory:
    def __init__(self, reader, applier) -> None:
        self.reader = reader
        self.applier = applier

    async def stage(
        self,
        attempt_id,
        mailbox_scope,
        *,
        baseline_checkpoint,
        max_results,
        max_pages,
        covered_snapshot_effect_keys,
    ):
        del max_results, max_pages, covered_snapshot_effect_keys
        start_history_id = decode_gmail_history_checkpoint(
            mailbox_scope, baseline_checkpoint
        )
        return await self.applier.stage(
            attempt_id,
            self.reader.history_result(start_history_id),
            start_history_id=start_history_id,
        )


class _UnusedGmailMessages:
    async def apply(self, message):
        del message
        raise AssertionError("synthetic PostgreSQL Gmail pages are empty")


class _UnusedGmailHistoryFactory:
    async def prepare(self, mailbox_scope, change):
        del mailbox_scope, change
        raise AssertionError("synthetic PostgreSQL Gmail history is empty")


class _CrashAfterPostgreSQLPageOccurrence(_CrashAfterPostgreSQLSave):
    def __init__(self, repository, page_kind, occurrence):
        super().__init__(repository, page_kind)
        self.occurrence = occurrence

    async def save(self, attempt):
        saved = await self.repository.save(attempt)
        reached = sum(
            page.page_kind is self.page_kind and page.committed for page in saved.pages
        )
        if not self.crashed and reached >= self.occurrence:
            self.crashed = True
            raise _PostgreSQLProcessCrash(
                f"synthetic crash after {self.page_kind.value}:{self.occurrence}"
            )
        return saved


class _PostgreSQLProcessCrash(BaseException):
    pass


def _postgresql_gmail_snapshot(
    repository,
    *,
    attempt_key: str,
    messages=None,
    message_ids: tuple[str, ...] = (),
):
    scope = MailboxReadScope(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        connection_id=UUID("ae000000-0000-0000-0000-000000000001"),
        connection_version_id=UUID("ae000000-0000-0000-0000-000000000002"),
        provider_kind="google",
        resource_scope_reference=UUID("ae000000-0000-0000-0000-000000000003"),
        mailbox_subject_reference=UUID("ae000000-0000-0000-0000-000000000004"),
        mailbox_binding_id=UUID("ae000000-0000-0000-0000-000000000005"),
        authorization_generation=1,
        configuration_generation=1,
        scope_generation=1,
        allowed_label_references=(UUID("ae000000-0000-0000-0000-000000000006"),),
        allowed_operations=tuple(EmailProviderOperation),
    )
    sync = EmailSyncCoordinator(
        repository=repository,
        admission=_SnapshotAdmission(),
        effects=_SnapshotEffects(),
    )
    reader = _PostgreSQLGmailReader(scope, message_ids=message_ids)
    history_applier = GmailIncrementalHistoryApplier(
        sync,
        scope,
        _UnusedGmailHistoryFactory(),
    )
    coordinator = GmailInitialSnapshotCoordinator(
        sync=sync,
        reader=reader,
        messages=messages or _UnusedGmailMessages(),
        history=_PostgreSQLGmailHistory(reader, history_applier),
    )
    return coordinator, GmailInitialSnapshotCommand(
        scope=scope,
        attempt_key=attempt_key,
        snapshot_page_size=10,
        max_snapshot_pages=3,
        history_page_size=10,
        max_history_pages=2,
        max_message_bytes=4096,
        access_policy_version_id=UUID("ae000000-0000-0000-0000-000000000007"),
    )


@pytest.mark.parametrize(
    ("page_kind", "occurrence"),
    (
        (EmailSyncPageKind.HISTORY_BASELINE, 1),
        (EmailSyncPageKind.SNAPSHOT_PAGE, 1),
        (EmailSyncPageKind.SNAPSHOT_PAGE, 2),
        (EmailSyncPageKind.HISTORY_DRAIN, 1),
        (EmailSyncPageKind.AGGREGATE_CHECKPOINT, 1),
    ),
)
async def test_postgresql_gmail_snapshot_crash_replays_every_durable_boundary(
    email_sync_repository,
    page_kind: EmailSyncPageKind,
    occurrence: int,
) -> None:
    repository, *_ = email_sync_repository
    crashing = _CrashAfterPostgreSQLPageOccurrence(
        repository,
        page_kind,
        occurrence,
    )
    coordinator, command = _postgresql_gmail_snapshot(
        crashing,
        attempt_key=(f"issue-838-postgresql-{page_kind.value}-{occurrence}"),
    )

    with pytest.raises(
        _PostgreSQLProcessCrash,
        match=f"{page_kind.value}:{occurrence}",
    ):
        await coordinator.run(command)
    recovered = await coordinator.run(command)

    assert recovered.attempt.status is EmailSyncStatus.COMPLETE
    assert (
        decode_gmail_history_checkpoint(
            command.scope, recovered.attempt.committed_checkpoint
        )
        == "101"
    )
    assert sum(page.terminal for page in recovered.attempt.pages) == 1
    assert all(page.committed for page in recovered.attempt.pages)


async def test_postgresql_gmail_history_cursor_recovery_preserves_old_cursor(
    email_sync_repository,
) -> None:
    repository, *_ = email_sync_repository
    snapshot, snapshot_command = _postgresql_gmail_snapshot(
        repository,
        attempt_key="issue-839-postgresql-recovery-snapshot",
    )
    history_applier = GmailIncrementalHistoryApplier(
        snapshot.sync,
        snapshot_command.scope,
        _UnusedGmailHistoryFactory(),
    )
    stale = await history_applier.start(
        attempt_key="issue-839-postgresql-stale-history",
        start_history_id="98",
    )

    class _History404:
        async def list_history(
            self, request: GmailHistoryPageRequest
        ) -> Mapping[str, object]:
            del request
            raise GmailHistoryProviderError(404)

    history_404 = await GmailHistoryReader(
        _History404(), scope=snapshot_command.scope, max_pages=1
    ).read(start_history_id="98", max_results=10)
    held = await history_applier.apply(
        stale.attempt_id,
        history_404,
        start_history_id="98",
    )
    assert held.status is EmailSyncStatus.RECONCILIATION_REQUIRED
    assert held.committed_checkpoint == "98"
    recovery = GmailHistoryCursorRecoveryCoordinator(
        sync=snapshot.sync,
        reconciliation=snapshot,
        profile=GmailHistoryCursorRecoveryProfile(
            snapshot_page_size=snapshot_command.snapshot_page_size,
            max_snapshot_pages=snapshot_command.max_snapshot_pages,
            history_page_size=snapshot_command.history_page_size,
            max_history_pages=snapshot_command.max_history_pages,
            max_message_bytes=snapshot_command.max_message_bytes,
            access_policy_version_id=snapshot_command.access_policy_version_id,
        ),
    )
    recovery_command = GmailHistoryCursorRecoveryCommand(
        scope=snapshot_command.scope,
        stale_attempt_id=stale.attempt_id,
        stale_checkpoint="98",
    )

    first = await recovery.reconcile(recovery_command)
    replay = await recovery.reconcile(recovery_command)
    fenced = await snapshot.sync.get_attempt(
        stale.attempt_id,
        workspace_id=snapshot_command.scope.workspace_id,
        environment_id=snapshot_command.scope.environment_id,
    )
    successor = await snapshot.sync.get_attempt(
        first.reconciliation_attempt_id,
        workspace_id=snapshot_command.scope.workspace_id,
        environment_id=snapshot_command.scope.environment_id,
    )

    assert first.state is GmailHistoryCursorRecoveryState.COMPLETE
    assert replay == first
    assert fenced.status is EmailSyncStatus.RECONCILIATION_REQUIRED
    assert fenced.committed_checkpoint == "98"
    assert successor.status is EmailSyncStatus.COMPLETE
    assert successor.committed_checkpoint != "98"
    assert sorted(page.page_kind.value for page in successor.pages) == sorted(
        page_kind.value
        for page_kind in (
            EmailSyncPageKind.HISTORY_BASELINE,
            EmailSyncPageKind.SNAPSHOT_PAGE,
            EmailSyncPageKind.SNAPSHOT_PAGE,
            EmailSyncPageKind.HISTORY_DRAIN,
            EmailSyncPageKind.AGGREGATE_CHECKPOINT,
        )
    )
    assert all(not page.lifecycle_receipts for page in successor.pages)


async def test_postgresql_gmail_history_cursor_recovery_crash_replays_successor(
    email_sync_repository,
) -> None:
    repository, *_ = email_sync_repository
    crashing = _CrashAfterPostgreSQLPageOccurrence(
        repository, EmailSyncPageKind.SNAPSHOT_PAGE, 1
    )
    snapshot, snapshot_command = _postgresql_gmail_snapshot(
        crashing,
        attempt_key="issue-839-postgresql-crash-recovery-snapshot",
    )
    history_applier = GmailIncrementalHistoryApplier(
        snapshot.sync,
        snapshot_command.scope,
        _UnusedGmailHistoryFactory(),
    )
    stale = await history_applier.start(
        attempt_key="issue-839-postgresql-crash-stale-history",
        start_history_id="98",
    )

    class _History404:
        async def list_history(
            self, request: GmailHistoryPageRequest
        ) -> Mapping[str, object]:
            del request
            raise GmailHistoryProviderError(404)

    history_404 = await GmailHistoryReader(
        _History404(), scope=snapshot_command.scope, max_pages=1
    ).read(start_history_id="98", max_results=10)
    await history_applier.apply(
        stale.attempt_id,
        history_404,
        start_history_id="98",
    )
    recovery = GmailHistoryCursorRecoveryCoordinator(
        sync=snapshot.sync,
        reconciliation=snapshot,
        profile=GmailHistoryCursorRecoveryProfile(
            snapshot_page_size=snapshot_command.snapshot_page_size,
            max_snapshot_pages=snapshot_command.max_snapshot_pages,
            history_page_size=snapshot_command.history_page_size,
            max_history_pages=snapshot_command.max_history_pages,
            max_message_bytes=snapshot_command.max_message_bytes,
            access_policy_version_id=snapshot_command.access_policy_version_id,
        ),
    )
    recovery_command = GmailHistoryCursorRecoveryCommand(
        scope=snapshot_command.scope,
        stale_attempt_id=stale.attempt_id,
        stale_checkpoint="98",
    )

    with pytest.raises(_PostgreSQLProcessCrash, match="snapshot_page:1"):
        await recovery.reconcile(recovery_command)
    recovered = await recovery.reconcile(recovery_command)

    assert recovered.state is GmailHistoryCursorRecoveryState.COMPLETE
    successor = await snapshot.sync.get_attempt(
        recovered.reconciliation_attempt_id,
        workspace_id=snapshot_command.scope.workspace_id,
        environment_id=snapshot_command.scope.environment_id,
    )
    assert successor.status is EmailSyncStatus.COMPLETE
    assert successor.committed_checkpoint != "98"
    assert sum(page.terminal for page in successor.pages) == 1


@pytest_asyncio.fixture(loop_scope="session")
async def historical_email_sync_database() -> AsyncIterator[TestDatabaseProvision]:
    settings = TestDatabaseSettings(use_testcontainers=True)
    provision = provision_test_database(
        settings,
        run_id=f"email-history-{uuid4().hex[:12]}",
    )
    if not provision.is_explicit:
        await install_container_marker(provision)
    try:
        yield provision
    finally:
        if provision.container is not None:
            provision.container.stop()


async def test_email_sync_migration_supports_downgrade_and_upgrade(
    historical_email_sync_database: TestDatabaseProvision,
) -> None:
    provision = historical_email_sync_database
    await bootstrap_database_roles(
        OperatorDatabaseSettings(
            url=provision.url,
            database_name=provision.target.database_name,
            migration_role=MIGRATION_ROLE,
            migration_password=MIGRATION_PASSWORD,
            runtime_role=RUNTIME_ROLE,
            runtime_password=RUNTIME_PASSWORD,
            dispatcher_role=DISPATCHER_ROLE,
            dispatcher_password=DISPATCHER_PASSWORD,
        )
    )
    settings = MigrationDatabaseSettings(
        url=_role_url(provision, MIGRATION_ROLE, MIGRATION_PASSWORD),
        migration_role=MIGRATION_ROLE,
        runtime_role=RUNTIME_ROLE,
        dispatcher_role=DISPATCHER_ROLE,
    )
    config_path, script_location = locate_migration_assets()
    config = Config(str(config_path))
    config.set_main_option("script_location", str(script_location))
    config.attributes["migration_settings"] = settings

    await asyncio.to_thread(command.upgrade, config, "20270112_111")
    migration_engine = create_async_engine(
        settings.url.get_secret_value(), hide_parameters=True
    )
    workspace_id = UUID("8a000000-0000-0000-0000-000000000091")
    environment_id = UUID("8b000000-0000-0000-0000-000000000091")
    attempt_id = UUID("8c000000-0000-0000-0000-000000000091")
    try:
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO spine.workspaces (id, slug, display_name) "
                    "VALUES (:workspace, 'historical-email-sync', 'Historical')"
                ),
                {"workspace": workspace_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO spine.environments "
                    "(id, workspace_id, kind, display_name) "
                    "VALUES (:environment, :workspace, 'development', 'Historical')"
                ),
                {"environment": environment_id, "workspace": workspace_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO spine.email_sync_attempts "
                    "(workspace_id, environment_id, attempt_id, attempt_key, revision, "
                    "authorization_generation, configuration_generation, scope_generation, "
                    "committed_checkpoint, record, created_at, updated_at) VALUES "
                    "(:workspace, :environment, :attempt, 'historical-attempt', 1, 1, 1, 1, "
                    "'cursor:historical:2', '{\"schema_version\":\"historical\"}'::jsonb, "
                    "now(), now())"
                ),
                {
                    "workspace": workspace_id,
                    "environment": environment_id,
                    "attempt": attempt_id,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO spine.email_sync_pages "
                    "(workspace_id, environment_id, attempt_id, page_id, page_digest, committed, record) "
                    "VALUES (:workspace, :environment, :attempt, 'historical-page', "
                    "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', true, "
                    "'{\"effects\":[]}'::jsonb)"
                ),
                {
                    "workspace": workspace_id,
                    "environment": environment_id,
                    "attempt": attempt_id,
                },
            )
        await asyncio.to_thread(command.downgrade, config, "20261227_95")
        async with migration_engine.connect() as connection:
            absent = await connection.scalar(
                text("SELECT to_regclass('spine.email_sync_attempts') IS NULL")
            )
            revision = await connection.scalar(
                text("SELECT version_num FROM spine.alembic_version")
            )
        assert absent is True
        assert revision == "20261227_95"

        await asyncio.to_thread(command.upgrade, config, "20270112_111")
        async with migration_engine.connect() as connection:
            present = await connection.scalar(
                text("SELECT to_regclass('spine.email_sync_attempts') IS NOT NULL")
            )
            revision = await connection.scalar(
                text("SELECT version_num FROM spine.alembic_version")
            )
        assert present is True
        assert revision == "20270112_111"
    finally:
        await migration_engine.dispose()


async def test_email_sync_erasure_migration_downgrade_is_irreversible(
    email_sync_repository,
) -> None:
    _, _, migration_engine, settings, *_ = email_sync_repository
    config_path, script_location = locate_migration_assets()
    config = Config(str(config_path))
    config.set_main_option("script_location", str(script_location))
    config.attributes["migration_settings"] = settings

    with pytest.raises(RuntimeError, match="erasure migration is irreversible"):
        await asyncio.to_thread(command.downgrade, config, "20270114_113")
    async with migration_engine.connect() as connection:
        revision = await connection.scalar(
            text("SELECT version_num FROM spine.alembic_version")
        )
        function_present = await connection.scalar(
            text("SELECT to_regprocedure('spine.email_sync_page_digest_v2(jsonb)') IS NOT NULL")
        )
    assert revision == "20270115_114"
    assert function_present is True


async def test_postgresql_gmail_crash_after_canonical_acceptance_has_one_effect(
    email_sync_repository,
) -> None:
    (
        repository,
        _,
        migration_engine,
        _,
        persistence,
        boundary,
        outbox_events,
        audit_events,
    ) = email_sync_repository
    _, snapshot_command = _postgresql_gmail_snapshot(
        repository,
        attempt_key="issue-838-gmail-accepted-effect-setup",
    )
    label = snapshot_command.scope.allowed_label_references[0]
    active_policy_id: UUID | None = None

    def context_factory(
        operation: PersistenceOperation, trace_id: UUID
    ) -> TrustedPersistenceContext:
        return boundary.worker(
            scope=EnvironmentScope(
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
            ),
            service_principal_id=SERVICE_ID,
            purpose=PersistencePurpose(
                "connector.ingestion.v1"
                if operation.value == "original.upload"
                else "contract_test"
            ),
            operation=operation,
            trace_id=trace_id,
        )

    def canonical_command_factory(**values):
        built = build_connector_canonical_acceptance_command(
            outbox_events=outbox_events,
            audit_events=audit_events,
            **values,
        )
        return CanonicalAcceptanceCommand.create(
            source=built.source,
            revision=built.revision,
            provenance=built.provenance,
            admission_decision_id=built.admission_decision_id,
            access_policy_version_id=active_policy_id,
            expected_current_revision_id=built.expected_current_revision_id,
            idempotency_key=built.idempotency_key,
            canonical_acceptance_id=built.canonical_acceptance_id,
            audit_event_id=built.audit_event_id,
            outbox_event_id=built.outbox_event_id,
            accepted_at=built.accepted_at,
            audit_event=built.audit_event,
            outbox_intent=built.outbox_intent,
            lifecycle_query=built.lifecycle_query,
            processing_admission=built.processing_admission,
            operation=built.digest.operation,
        )

    ingestion = ConnectorIngestionOrchestrator(
        uow_factory=persistence.uow_factory,
        observation_reader=_UnusedObservationReader(),
        uploader=OriginalUploadOrchestrator(
            uow_factory=persistence.uow_factory,
            object_store=DeterministicOriginalObjectStoreFake(
                grant_registry=AuthorizedReadGrantRegistry(),
                generation_factory=lambda: UUID("ad000000-0000-0000-0000-000000000003"),
                clock=lambda: datetime(2026, 11, 3, tzinfo=timezone.utc),
                max_chunk_bytes=4096,
                max_object_bytes=8192,
                read_chunk_bytes=64,
            ),
            identity_factory=iter(
                (
                    UUID("ad000000-0000-0000-0000-000000000001"),
                    UUID("ad000000-0000-0000-0000-000000000002"),
                )
            ).__next__,
        ),
        read_context_factory=context_factory,
        context_factory=context_factory,
        canonical_command_factory=canonical_command_factory,
        outbox_events=outbox_events,
        audit_events=audit_events,
        read_operation=PersistenceOperation("mailbox.read_raw_message"),
        admission_reference_type="mailbox_read_admission",
        clock=lambda: datetime(2026, 11, 3, tzinfo=timezone.utc),
    )
    durable_messages = GmailCanonicalSnapshotMessageAdapter(
        ingestion=ingestion,
        raw_messages=_GmailRawMessageAdmission(snapshot_command.scope, label),
        authorized_label_reference=label,
        clock=lambda: datetime(2026, 11, 3, tzinfo=timezone.utc),
    )
    message_command = GmailSnapshotMessageCommand(
        scope=snapshot_command.scope,
        message_id="message-a",
        max_message_bytes=snapshot_command.max_message_bytes,
        access_policy_version_id=snapshot_command.access_policy_version_id,
        effect_key=gmail_snapshot_effect_key(snapshot_command.scope, "message-a"),
    )
    active_policy_id = await _provision_eligible_canonical_message_source(
        messages=durable_messages,
        command=message_command,
        migration_engine=migration_engine,
        persistence=persistence,
        context_factory=context_factory,
        outbox_events=outbox_events,
        activation_number=83800,
    )
    crashing_messages = _CrashAfterCanonicalAcceptance(
        durable_messages, process_crash=True
    )
    coordinator, command = _postgresql_gmail_snapshot(
        repository,
        attempt_key="issue-838-gmail-accepted-effect-crash",
        messages=crashing_messages,
        message_ids=("message-a",),
    )

    with pytest.raises(
        _PostgreSQLProcessCrash,
        match="process crash after canonical acceptance",
    ):
        await coordinator.run(command)
    interrupted = await coordinator.sync.create_attempt(
        command.scope,
        attempt_key=command.attempt_key,
    )
    recovered = await coordinator.run(command)

    assert interrupted.status is EmailSyncStatus.RUNNING
    assert interrupted.committed_checkpoint is None
    assert recovered.attempt.status is EmailSyncStatus.COMPLETE, (
        recovered.attempt.failure_code,
        crashing_messages.receipts,
        crashing_messages.errors,
    )
    assert recovered.snapshot_messages == 1
    assert len(crashing_messages.receipts) == 2
    assert (
        crashing_messages.receipts[0].canonical_acceptance_id
        == crashing_messages.receipts[1].canonical_acceptance_id
    )
    async with migration_engine.connect() as connection:
        acceptance_count = await connection.scalar(
            text(
                "SELECT count(*) FROM spine.canonical_acceptances AS acceptance "
                "JOIN spine.source_objects AS source "
                "ON source.source_object_id = acceptance.source_object_id "
                "WHERE acceptance.workspace_id = :workspace "
                "AND acceptance.environment_id = :environment "
                "AND source.connection_id = :connection"
            ),
            {
                "workspace": WORKSPACE_ID,
                "environment": ENVIRONMENT_ID,
                "connection": command.scope.connection_id,
            },
        )
    assert acceptance_count == 1


async def test_postgresql_email_sync_rolls_back_checkpoint_without_canonical_receipts(
    email_sync_repository,
) -> None:
    repository, *_ = email_sync_repository
    attempt = _attempt()
    page = EmailSyncPageState(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        page_id="page-with-unaccepted-effect",
        page_digest="b" * 64,
        requested_checkpoint=None,
        next_checkpoint="opaque-next",
        terminal=False,
        effects=(
            EmailSyncEffect(
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
                effect_key="effect-unaccepted",
                effect_reference=uuid4(),
            ),
        ),
        effect_receipts=(
            EmailSyncEffectReceipt(
                effect_key="effect-unaccepted",
                receipt_reference="receipt-2",
                durable=True,
            ),
        ),
        committed=True,
    )
    await repository.save(attempt)

    with pytest.raises(Exception) as error:
        await repository.save(
            attempt.model_copy(
                update={"pages": (page,), "committed_checkpoint": "opaque-next"}
            )
        )

    rendered_error = str(error.value)
    assert "opaque-next" not in rendered_error
    assert "page-with-unaccepted-effect" not in rendered_error
    assert "effect-unaccepted" not in rendered_error

    loaded = await repository.get(attempt.attempt_id, WORKSPACE_ID, ENVIRONMENT_ID)
    assert loaded is not None
    assert loaded.committed_checkpoint is None
    assert loaded.pages == ()


async def test_postgresql_email_sync_holds_checkpoint_for_partial_page(
    email_sync_repository,
) -> None:
    repository, *_ = email_sync_repository
    attempt = _attempt()
    await repository.save(attempt)
    current = await repository.get(attempt.attempt_id, WORKSPACE_ID, ENVIRONMENT_ID)
    assert current is not None
    page = EmailSyncPageState(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        page_id="partial-page",
        page_digest="d" * 64,
        requested_checkpoint=None,
        next_checkpoint="must-not-advance",
        terminal=False,
        effects=(
            EmailSyncEffect(
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
                effect_key="accepted-effect",
                effect_reference=uuid4(),
            ),
            EmailSyncEffect(
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
                effect_key="missing-effect",
                effect_reference=uuid4(),
            ),
        ),
        effect_receipts=(
            EmailSyncEffectReceipt(
                effect_key="accepted-effect",
                receipt_reference="accepted-receipt",
                durable=True,
            ),
        ),
        committed=True,
    )

    with pytest.raises(Exception):
        await repository.save(
            current.model_copy(
                update={
                    "pages": (page,),
                    "committed_checkpoint": "must-not-advance",
                }
            )
        )

    stored = await repository.get(attempt.attempt_id, WORKSPACE_ID, ENVIRONMENT_ID)
    assert stored is not None
    assert stored.committed_checkpoint is None
    assert stored.pages == ()
