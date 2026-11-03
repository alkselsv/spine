from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
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
    EmailSyncPageKind,
    EmailSyncPageState,
    EmailSyncStatus,
)
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
)
from spine.infrastructure.db.test_harness import TestDatabaseProvision
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
from spine.domain.connections.email_scope import (
    EmailProviderOperation,
    MailboxReadScope,
)
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
    migration_engine = create_async_engine(migration_url.get_secret_value(), hide_parameters=True)
    runtime_engine = create_async_engine(
        _role_url(postgresql_provision, RUNTIME_ROLE, RUNTIME_PASSWORD).get_secret_value(),
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

    sessions = async_sessionmaker(runtime_engine, expire_on_commit=False, autoflush=False)
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

    def repository(workspace_id: UUID, environment_id: UUID) -> PostgreSQLEmailSyncRepository:
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


async def test_postgresql_email_sync_cas_rejects_stale_worker(email_sync_repository) -> None:
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
        await repository.save(original.model_copy(update={"status": EmailSyncStatus.FAILED}))


async def test_postgresql_email_sync_cas_race_has_one_winner(email_sync_repository) -> None:
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
    assert sum(isinstance(value, EmailSyncConcurrentUpdateError) for value in outcomes) == 1
    stored = await repository.get(attempt.attempt_id, WORKSPACE_ID, ENVIRONMENT_ID)
    assert stored is not None
    assert stored.revision == 2


async def test_postgresql_email_sync_cas_rejects_changed_generation(email_sync_repository) -> None:
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
    restored = await repository.get(
        attempt.attempt_id, WORKSPACE_ID, ENVIRONMENT_ID
    )

    assert restored is not None
    assert restored.pages == (page,)
    assert decode_yandex_mail_incremental_checkpoint(
        restored.committed_checkpoint or ""
    ) == checkpoint


async def test_postgresql_email_sync_persists_folder_stages_before_one_aggregate_checkpoint(
    email_sync_repository,
) -> None:
    repository, *_ = email_sync_repository
    attempt = _attempt()
    await repository.save(attempt)
    current = await repository.get(
        attempt.attempt_id, WORKSPACE_ID, ENVIRONMENT_ID
    )
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
    reloaded = await repository.get(
        attempt.attempt_id, WORKSPACE_ID, ENVIRONMENT_ID
    )
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


class _ScriptedImapSnapshotReader:
    def __init__(
        self,
        folders: tuple[UUID, ...],
        *,
        partial_discovery: bool = False,
        page_failure: YandexMailSnapshotUidPageCode | None = None,
        message_uid: int | None = None,
    ) -> None:
        self.folders = folders
        self.partial_discovery = partial_discovery
        self.page_failure = page_failure
        self.message_uid = message_uid
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
            uid_validity=101,
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


class _CrashAfterCanonicalAcceptance:
    def __init__(self, messages: YandexMailCanonicalSnapshotMessageAdapter) -> None:
        self.messages = messages
        self.receipts = []
        self.errors = []
        self.crashed = False

    async def apply(self, command):
        try:
            receipt = await self.messages.apply(command)
        except Exception as error:
            self.errors.append((repr(error), repr(error.__cause__)))
            raise
        self.receipts.append(receipt)
        if not self.crashed:
            self.crashed = True
            raise RuntimeError("synthetic crash after canonical acceptance")
        return receipt


async def _provision_eligible_canonical_message_source(
    *,
    messages: YandexMailCanonicalSnapshotMessageAdapter,
    command: YandexMailSnapshotMessageCommand,
    migration_engine,
    persistence,
    context_factory,
    outbox_events,
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
                "WHERE workspace_id = :workspace AND environment_id = :environment"
            ),
            {"workspace": WORKSPACE_ID, "environment": ENVIRONMENT_ID},
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
        number=84900,
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
            page.page_kind is self.page_kind and page.committed
            for page in saved.pages
        ):
            self.crashed = True
            raise RuntimeError(f"synthetic crash after {self.page_kind.value}")
        return saved


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
                generation_factory=lambda: UUID(
                    "9d000000-0000-0000-0000-000000000003"
                ),
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
    folder = UUID(int=UUID("9e000000-0000-0000-0000-000000000010").int + list(EmailSyncPageKind).index(page_kind))
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

    assert recovered.attempt.status is EmailSyncStatus.COMPLETE
    assert sum(page.terminal for page in recovered.attempt.pages) == 1


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
                effect_key="effect-unaccepted", receipt_reference="receipt-2", durable=True
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


async def test_email_sync_migration_supports_downgrade_and_upgrade(email_sync_repository) -> None:
    _, _, migration_engine, settings, *_ = email_sync_repository
    config_path, script_location = locate_migration_assets()
    config = Config(str(config_path))
    config.set_main_option("script_location", str(script_location))
    config.attributes["migration_settings"] = settings

    await asyncio.to_thread(command.downgrade, config, "20261227_95")
    async with migration_engine.connect() as connection:
        absent = await connection.scalar(
            text("SELECT to_regclass('spine.email_sync_attempts') IS NULL")
        )
    assert absent is True

    await asyncio.to_thread(upgrade_database, settings)
