from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
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
    EmailSyncAttempt,
    EmailSyncConcurrentUpdateError,
    EmailSyncEffect,
    EmailSyncEffectReceipt,
    EmailSyncPageState,
    EmailSyncStatus,
)
from spine.application.persistence import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.infrastructure.db.migrations import locate_migration_assets, upgrade_database
from spine.infrastructure.db.operator import bootstrap_database_roles
from spine.infrastructure.db.settings import (
    POSTGRESQL_SEARCH_PATH_OPTIONS,
    MigrationDatabaseSettings,
    OperatorDatabaseSettings,
)
from spine.infrastructure.db.test_harness import TestDatabaseProvision
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
from tests.contracts.persistence.outbox_events import create_outbox_event_registry


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


def _role_url(provision: TestDatabaseProvision, role: str, password: str) -> SecretStr:
    return SecretStr(
        make_url(provision.url.get_secret_value())
        .set(username=role, password=password)
        .render_as_string(hide_password=False)
    )


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def email_sync_repository(
    postgresql_provision: TestDatabaseProvision,
) -> AsyncIterator[tuple[PostgreSQLEmailSyncRepository, PostgreSQLEmailSyncRepository, object, MigrationDatabaseSettings]]:
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
    persistence = PostgreSQLPersistence(
        session_factory=sessions,
        context_verifier=boundary,
        outbox_events=create_outbox_event_registry(),
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
    repository, other_repository, _, _ = email_sync_repository
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
    repository, _, _, _ = email_sync_repository
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
    repository, _, _, _ = email_sync_repository
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
    repository, _, _, _ = email_sync_repository
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
    repository, _, _, _ = email_sync_repository
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
    repository, _, _, _ = email_sync_repository
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
    repository, _, _, _ = email_sync_repository
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


async def test_postgresql_email_sync_rolls_back_checkpoint_without_canonical_receipts(
    email_sync_repository,
) -> None:
    repository, _, _, _ = email_sync_repository
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
    repository, _, _, _ = email_sync_repository
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
    _, _, migration_engine, settings = email_sync_repository
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
