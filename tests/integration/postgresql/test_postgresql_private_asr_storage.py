from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import event, insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from spine.application.persistence.context import (
    EnvironmentScope, PersistenceOperation, PersistencePurpose,
    TrustedPersistenceContext,
)
from spine.domain.evaluations.private_asr_corpus import PrivateReference
from spine.infrastructure.db.migrations import upgrade_database
from spine.infrastructure.db.operator import bootstrap_database_roles
from spine.infrastructure.db.settings import (
    POSTGRESQL_SEARCH_PATH_OPTIONS, MigrationDatabaseSettings,
    OperatorDatabaseSettings,
)
from spine.infrastructure.db.test_harness import TestDatabaseProvision
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.persistence.postgresql_mappings import audit_events, environments, workspaces
from spine.infrastructure.private_asr.postgresql_storage import (
    PostgreSQLPrivateASRStorage, PrivateASRStorageDenied, private_asr_artifacts,
)
from tests.application.test_private_asr_corpus import service, source_revision


pytestmark = [pytest.mark.postgresql, pytest.mark.asyncio(loop_scope="session")]

MIGRATION_ROLE = "spine_migration"
RUNTIME_ROLE = "spine_runtime"
DISPATCHER_ROLE = "spine_dispatcher"
MIGRATION_PASSWORD = "migration-asr-storage-secret"
RUNTIME_PASSWORD = "runtime-asr-storage-secret"
DISPATCHER_PASSWORD = "dispatcher-asr-storage-secret"
NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)


def _role_url(provision: TestDatabaseProvision, role: str, password: str) -> SecretStr:
    value = make_url(provision.url.get_secret_value()).set(
        username=role, password=password,
    )
    return SecretStr(value.render_as_string(hide_password=False))


class _Database:
    def __init__(self, migration: AsyncEngine, runtime: AsyncEngine) -> None:
        self.migration = migration
        self.runtime = runtime


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def private_asr_database(
    postgresql_provision: TestDatabaseProvision,
) -> AsyncIterator[_Database]:
    operator = OperatorDatabaseSettings(
        url=postgresql_provision.url,
        database_name=postgresql_provision.target.database_name,
        migration_role=MIGRATION_ROLE, migration_password=MIGRATION_PASSWORD,
        runtime_role=RUNTIME_ROLE, runtime_password=RUNTIME_PASSWORD,
        dispatcher_role=DISPATCHER_ROLE, dispatcher_password=DISPATCHER_PASSWORD,
    )
    await bootstrap_database_roles(operator)
    migration_url = _role_url(postgresql_provision, MIGRATION_ROLE, MIGRATION_PASSWORD)
    await asyncio.to_thread(upgrade_database, MigrationDatabaseSettings(
        url=migration_url, migration_role=MIGRATION_ROLE, runtime_role=RUNTIME_ROLE,
        dispatcher_role=DISPATCHER_ROLE,
    ))
    migration = create_async_engine(migration_url.get_secret_value(), hide_parameters=True)
    runtime = create_async_engine(
        _role_url(postgresql_provision, RUNTIME_ROLE, RUNTIME_PASSWORD).get_secret_value(),
        pool_size=5, max_overflow=0, pool_reset_on_return="rollback",
        hide_parameters=True, connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    try:
        yield _Database(migration, runtime)
    finally:
        await runtime.dispose()
        await migration.dispose()


@pytest.mark.asyncio
async def test_postgresql_private_asr_store_fails_closed_without_canonical_retention_terms(
    private_asr_database: _Database,
) -> None:
    request, admission, authority, policies, _, _, _, _ = service()
    source, revision = source_revision()
    workspace_id, environment_id = uuid4(), uuid4()
    source = source.model_copy(update={
        "source_object_id": uuid4(), "workspace_id": workspace_id,
        "environment_id": environment_id,
    })
    revision = revision.model_copy(update={
        "revision_id": uuid4(), "source_object_id": source.source_object_id,
        "workspace_id": workspace_id, "environment_id": environment_id,
    })
    request = request.model_copy(update={
        "source_object": source, "source_revision": revision,
        "workspace_id": workspace_id, "environment_id": environment_id,
        "allowed_environment_id": environment_id,
    })
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=uuid4(), secret=b"synthetic-private-asr-storage-context-key",
    )
    context: TrustedPersistenceContext = boundary.interactive(
        scope=EnvironmentScope(workspace_id=workspace_id, environment_id=environment_id),
        acting_subject_id=UUID(int=11),
        purpose=PersistencePurpose("asr_model_evaluation"),
        operation=PersistenceOperation("run_private_asr_evaluation"),
        trace_id=uuid4(),
    )
    admitted = await admission.admit(request, context=context)
    store = PostgreSQLPrivateASRStorage(
        sessions=async_sessionmaker(
            private_asr_database.runtime, expire_on_commit=False, autoflush=False,
        ),
        contexts=boundary, authority=authority, access_policies=policies,
        audit_receipts=object(), clock=lambda: NOW,
    )

    with pytest.raises(PrivateASRStorageDenied, match="storage authorization denied"):
        await store.begin(admitted, context=context)
    async with async_sessionmaker(private_asr_database.runtime)() as session, session.begin():
        await store._set_scope(session, context)
        assert (await session.execute(select(private_asr_artifacts.c.artifact_id).where(
            private_asr_artifacts.c.workspace_id == workspace_id,
            private_asr_artifacts.c.environment_id == environment_id,
        ))).scalars().all() == []

    cleanup_context = boundary.worker(
        scope=EnvironmentScope(workspace_id=workspace_id, environment_id=environment_id),
        service_principal_id=UUID(int=12),
        purpose=PersistencePurpose("asr_model_evaluation"),
        operation=PersistenceOperation("private_asr_retention_expiration"),
        trace_id=uuid4(),
    )
    opaque_ref = PrivateReference(reference_id=uuid4(), reference_kind="private_asr_report")
    with pytest.raises(PrivateASRStorageDenied, match="storage authorization denied"):
        await store.expire(opaque_ref, policy_ref=admitted.retention_profile_ref, context=cleanup_context)

    artifact_id, audit_id, trace_id = uuid4(), uuid4(), uuid4()
    async with private_asr_database.migration.begin() as connection:
        await connection.execute(insert(workspaces).values(
            id=workspace_id, slug=f"asr-{workspace_id.hex[:12]}",
            display_name="Synthetic ASR test",
        ))
        await connection.execute(insert(environments).values(
            id=environment_id, workspace_id=workspace_id, kind="development",
            display_name="Synthetic ASR test environment",
        ))
        await connection.execute(insert(audit_events).values(
            audit_event_id=audit_id, workspace_id=workspace_id,
            environment_id=environment_id, event_type="command.outcome",
            schema_version=1, origin="interactive", acting_subject_id=UUID(int=11),
            service_principal_id=None, trace_id=trace_id, correlation_id=None,
            causation_id=None, occurred_at=NOW, appended_at=NOW, target_type=None,
            target_id=None, target_schema_version=None, outcome="recorded",
            reason="synthetic_private_asr_storage_test",
            producer_deduplication_id=None,
            payload={"command_type": "synthetic_private_asr_storage"},
        ))
        await connection.execute(insert(private_asr_artifacts).values(
            artifact_id=artifact_id, workspace_id=workspace_id,
            environment_id=environment_id, purpose="asr_model_evaluation",
            kind="original", status="committed",
            byte_length=len(b"synthetic private bytes"),
            payload=b"synthetic private bytes",
            retention_profile_ref=admitted.retention_profile_ref.reference_id,
            audit_receipt_ref=audit_id,
            expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
            admitted_record=None, created_at=NOW,
        ))

    statements: list[str] = []

    def capture_statement(conn, cursor, statement, parameters, execution_context, executemany):
        statements.append(statement.lower())

    event.listen(private_asr_database.runtime.sync_engine, "before_cursor_execute", capture_statement)
    try:
        callback_called = False

        async def authorize_report_commit() -> None:
            nonlocal callback_called
            callback_called = True

        with pytest.raises(PrivateASRStorageDenied, match="storage authorization denied"):
            await store.store_private_report(
                b"synthetic private report payload", report_id=uuid4(),
                idempotency_key="synthetic-private-report-key", source_bindings=(),
                retention_profile_ref=admitted.retention_profile_ref,
                audit_receipt_ref=admitted.audit_receipt_ref,
                context=context, authorize_commit=authorize_report_commit,
            )
        assert not callback_called

        denial_messages = []
        for reference_id in (artifact_id, uuid4()):
            reference = PrivateReference(
                reference_id=reference_id, reference_kind="private_asr_original",
            )
            with pytest.raises(PrivateASRStorageDenied) as denial:
                await store.read_private_reference(reference, context=context)
            denial_messages.append(str(denial.value))
    finally:
        event.remove(
            private_asr_database.runtime.sync_engine,
            "before_cursor_execute", capture_statement,
        )
    assert denial_messages[0] == denial_messages[1]
    assert str(artifact_id) not in denial_messages[0]
    assert str(admitted.source_revision.revision_id) not in denial_messages[0]
    assert not any(
        "select spine.private_asr_artifacts.payload" in statement
        for statement in statements
    )

    async with async_sessionmaker(private_asr_database.runtime)() as session, session.begin():
        rls = (await session.execute(text(
            """SELECT role.rolbypassrls, relation.relrowsecurity,
                      relation.relforcerowsecurity
                 FROM pg_roles AS role
                 CROSS JOIN pg_class AS relation
                 JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                WHERE role.rolname = current_user
                  AND namespace.nspname = 'spine'
                  AND relation.relname = 'private_asr_artifacts'"""
        ))).one()
        assert tuple(rls) == (False, True, True)
        await store._set_scope(session, context)
        rows = (await session.execute(select(private_asr_artifacts.c.artifact_id).where(
            private_asr_artifacts.c.workspace_id == workspace_id,
            private_asr_artifacts.c.environment_id == environment_id,
        ))).scalars().all()
    assert rows == [artifact_id]
