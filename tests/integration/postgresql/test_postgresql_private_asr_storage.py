from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
import json
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import insert, select, text
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from spine.application.persistence.context import (
    EnvironmentScope, PersistenceOperation, PersistencePurpose,
    TrustedPersistenceContext,
)
from spine.application.private_asr_corpus import (
    CurrentPrivateASRRetentionProfile, PrivateReportSourceBinding,
)
from spine.domain.evaluations.private_asr_corpus import PrivateReference
from spine.domain.evaluations.private_asr_corpus import CorpusSplit
from spine.domain.evaluations.private_asr_qualification import (
    PrivateCaseDisposition, PrivateCaseOutcome, PrivateEvaluationDenominators,
    PrivateRealASREvaluationReport,
)
from spine.infrastructure.db.migrations import upgrade_database
from spine.infrastructure.db.operator import bootstrap_database_roles
from spine.infrastructure.db.settings import (
    POSTGRESQL_SEARCH_PATH_OPTIONS, MigrationDatabaseSettings,
    OperatorDatabaseSettings,
)
from spine.infrastructure.db.test_harness import TestDatabaseProvision
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.persistence.postgresql_mappings import (
    audit_events, environments, source_objects, source_revisions, workspaces,
)
from spine.infrastructure.private_asr.postgresql_storage import (
    PostgreSQLPrivateASRStorage, PrivateASRStorageDenied,
    private_asr_aggregate_exports, private_asr_artifacts,
    private_asr_report_sources, private_asr_source_fences,
)
from tests.application.test_private_asr_corpus import (
    NOW, admission_request, service, source_revision,
)


pytestmark = [pytest.mark.postgresql, pytest.mark.asyncio(loop_scope="session")]

MIGRATION_ROLE = "spine_migration"
RUNTIME_ROLE = "spine_runtime"
DISPATCHER_ROLE = "spine_dispatcher"
MIGRATION_PASSWORD = "migration-asr-storage-secret"
RUNTIME_PASSWORD = "runtime-asr-storage-secret"
DISPATCHER_PASSWORD = "dispatcher-asr-storage-secret"


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


class _RetentionReader:
    def __init__(self, expiry: datetime) -> None:
        self.expiry = expiry

    async def resolve_current(
        self, profile_ref, *, workspace_id, environment_id, context, evaluated_at,
    ):
        return CurrentPrivateASRRetentionProfile(
            profile_ref=profile_ref, workspace_id=workspace_id,
            environment_id=environment_id, purpose="asr_model_evaluation",
            approved=True, current=True, revoked=False, expires_at=self.expiry,
        )


async def _seed_scope_and_source(
    database: _Database, *, workspace_id: UUID, environment_id: UUID,
    source, revision,
) -> None:
    revision_data = revision.model_dump(mode="python")
    revision_data["revision_metadata"] = revision.revision_metadata.model_dump(mode="json")
    revision_data["original_reference"] = revision.original_reference.model_dump(mode="json")
    async with database.migration.begin() as connection:
        await connection.execute(insert(workspaces).values(
            id=workspace_id, slug=f"asr-{workspace_id.hex[:12]}", display_name="Synthetic ASR test",
        ))
        await connection.execute(insert(environments).values(
            id=environment_id, workspace_id=workspace_id, kind="development",
            display_name="Synthetic ASR test environment",
        ))
        await connection.execute(insert(source_objects).values(
            source_object_id=source.source_object_id, workspace_id=workspace_id,
            environment_id=environment_id, source_kind=source.source_kind,
            identity_mode=source.identity_mode, connection_id=None,
            external_namespace=None, external_generation=None,
            external_object_id=None, upload_identity=source.upload_identity,
            created_at=source.created_at,
        ))
        await connection.execute(insert(source_revisions).values(**revision_data))


async def _seed_audit(
    database: _Database, *, event_id: UUID, workspace_id: UUID,
    environment_id: UUID, trace_id: UUID,
) -> None:
    async with database.migration.begin() as connection:
        await connection.execute(insert(audit_events).values(
            audit_event_id=event_id, workspace_id=workspace_id,
            environment_id=environment_id, event_type="command.outcome",
            schema_version=1, origin="interactive", acting_subject_id=UUID(int=11),
            service_principal_id=None, trace_id=trace_id, correlation_id=None,
            causation_id=None, occurred_at=NOW, appended_at=NOW, target_type=None,
            target_id=None, target_schema_version=None, outcome="recorded",
            reason="private_asr_storage_receipt", producer_deduplication_id=None,
            payload={"command_type": "private_asr_storage"},
        ))


def _report(
    *, workspace_id: UUID, environment_id: UUID, report_id: UUID,
    retention_ref: PrivateReference, audit_ref: PrivateReference,
) -> PrivateRealASREvaluationReport:
    return PrivateRealASREvaluationReport(
        report_id=report_id, dataset_id=UUID(int=201), manifest_version="fixture:v1",
        manifest_sha256="a" * 64, evaluator_id="fixture_evaluator",
        evaluator_version="fixture_evaluator:v1", normalizer_version="fixture_normalizer:v1",
        candidate_id="fixture_candidate", candidate_config_digest="b" * 64,
        workspace_id=workspace_id, environment_id=environment_id, generated_at=NOW,
        outcomes=(PrivateCaseOutcome(
            record_ref=PrivateReference(reference_id=UUID(int=202), reference_kind="private_asr_record"),
            split=CorpusSplit.DEVELOPMENT, disposition=PrivateCaseDisposition.UNANNOTATED,
        ),), denominators=PrivateEvaluationDenominators(
            eligible=0, admitted=0, verified=0, annotated=0, scorable=0,
            measured=0, independent_lineages=0, denied=0, revoked=0, unavailable=0,
        ), measurement_limitations=("synthetic_fixture",),
        coverage_disposition="insufficient_coverage", audit_receipt_ref=audit_ref,
        retention_profile_ref=retention_ref,
    )


async def _new_store(
    database: _Database, *, boundary: TrustedContextBoundary,
    authority, policies, retention_expiry: datetime,
) -> PostgreSQLPrivateASRStorage:
    return PostgreSQLPrivateASRStorage(
        sessions=async_sessionmaker(database.runtime, expire_on_commit=False, autoflush=False),
        contexts=boundary, authority=authority, access_policies=policies,
        retention_profiles=_RetentionReader(retention_expiry),
        audit_receipts=object(), clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_postgresql_report_callback_denial_is_invisible_and_retry_is_stable(
    private_asr_database: _Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id, environment_id = uuid4(), uuid4()
    request, _, authority, policies, _, _, _, _ = service()
    source, revision = source_revision()
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
    await _seed_scope_and_source(
        private_asr_database, workspace_id=workspace_id,
        environment_id=environment_id, source=source, revision=revision,
    )
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
    audit_id, report_audit_id = uuid4(), uuid4()
    await _seed_audit(
        private_asr_database, event_id=audit_id, workspace_id=workspace_id,
        environment_id=environment_id, trace_id=context.trace_id,
    )
    await _seed_audit(
        private_asr_database, event_id=report_audit_id, workspace_id=workspace_id,
        environment_id=environment_id, trace_id=context.trace_id,
    )
    _, admission, authority, policies, _, _, _, _ = service()
    admitted = await admission.admit(request, context=context)
    admitted = admitted.model_copy(update={
        "audit_receipt_ref": PrivateReference(
            reference_id=audit_id, reference_kind="private_asr_audit",
        ),
    })
    store = await _new_store(
        private_asr_database, boundary=boundary, authority=authority,
        policies=policies, retention_expiry=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )

    # The test isolates the report transaction from canonical source-row
    # construction. The #795 integration contract around those rows remains a
    # separate operator qualification until it is wired through the UoW adapter.
    async def synthetic_source_lock(session, admitted_source):
        return None

    monkeypatch.setattr(store, "_locked_source", synthetic_source_lock)
    staging_ref = await store.begin(admitted, context=context)
    raw = b"synthetic fixture audio"
    import hashlib
    digest = hashlib.sha256(raw).hexdigest()
    await store.append(staging_ref, b"synthetic interrupted prefix", context=context)
    assert await store.begin(admitted, context=context) == staging_ref
    await store.append(staging_ref, raw, context=context)
    with pytest.raises(PrivateASRStorageDenied):
        await store.finalize(
            staging_ref, sha256="c" * 64, byte_length=len(raw), context=context,
        )
    await store.finalize(staging_ref, sha256=digest, byte_length=len(raw), context=context)
    original_ref = await store.bind_immutable_original(
        admitted, sha256=digest, byte_length=len(raw), context=context,
    )
    report_id = uuid4()
    retention_ref = request.retention_profile_ref
    report = _report(
        workspace_id=workspace_id, environment_id=environment_id,
        report_id=report_id, retention_ref=retention_ref,
        audit_ref=PrivateReference(reference_id=report_audit_id, reference_kind="private_asr_audit"),
    )
    payload = report.model_dump_json().encode()
    arguments = dict(
        report_id=report_id, idempotency_key=f"private-asr-report:{report_id}",
        source_bindings=(PrivateReportSourceBinding(
            record_id=UUID(int=202), source_revision_id=revision.revision_id,
        ),), retention_profile_ref=retention_ref,
        audit_receipt_ref=report.audit_receipt_ref, context=context,
    )

    async def denied() -> None:
        raise RuntimeError("synthetic-private-payload-marker")

    with pytest.raises(PrivateASRStorageDenied) as denied_error:
        await store.store_private_report(payload, authorize_commit=denied, **arguments)
    assert "synthetic-private-payload-marker" not in str(denied_error.value)
    sessions = async_sessionmaker(private_asr_database.runtime, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await store._set_scope(session, context)
        visible = (await session.execute(select(private_asr_artifacts.c.artifact_id).where(
            private_asr_artifacts.c.workspace_id == workspace_id,
            private_asr_artifacts.c.environment_id == environment_id,
            private_asr_artifacts.c.report_id == report_id,
        ))).scalars().all()
    assert visible == []

    async def allowed() -> None:
        return None

    async def cancelled() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await store.store_private_report(payload, authorize_commit=cancelled, **arguments)

    async def concurrent_commit():
        return await store.store_private_report(payload, authorize_commit=allowed, **arguments)

    first, second = await asyncio.gather(concurrent_commit(), concurrent_commit())
    assert first == second
    receipt = first
    # Simulate a lost caller response: replay returns the same receipt.
    replay = await store.store_private_report(payload, authorize_commit=allowed, **arguments)
    assert replay == receipt
    assert await store.read_private_reference(receipt.report_ref, context=context) == payload
    assert await store.read_private_reference(original_ref, context=context) == raw
    async with sessions() as session, session.begin():
        await store._set_scope(session, context)
        reports = (await session.execute(select(private_asr_artifacts.c.artifact_id).where(
            private_asr_artifacts.c.workspace_id == workspace_id,
            private_asr_artifacts.c.environment_id == environment_id,
            private_asr_artifacts.c.kind == "report",
            private_asr_artifacts.c.status == "committed",
        ))).scalars().all()
        bindings = (await session.execute(select(private_asr_report_sources.c.source_revision_id).where(
            private_asr_report_sources.c.workspace_id == workspace_id,
            private_asr_report_sources.c.environment_id == environment_id,
            private_asr_report_sources.c.report_id == report_id,
        ))).scalars().all()
        source_record = await session.scalar(select(
            private_asr_report_sources.c.source_record_id
        ).where(
            private_asr_report_sources.c.workspace_id == workspace_id,
            private_asr_report_sources.c.environment_id == environment_id,
            private_asr_report_sources.c.report_id == report_id,
        ))
    assert reports == [receipt.report_ref.reference_id]
    assert bindings == [revision.revision_id]
    assert source_record == UUID(int=202)

    export_authorization_called = False

    async def authorize_aggregate_export() -> None:
        nonlocal export_authorization_called
        export_authorization_called = True

    with pytest.raises(PrivateASRStorageDenied, match="not enabled"):
        await store.export_sanitized(
            b"synthetic aggregate payload",
            context=context,
            authorize_export=authorize_aggregate_export,
        )
    assert not export_authorization_called
    async with sessions() as session, session.begin():
        await store._set_scope(session, context)
        aggregate_rows = (await session.execute(select(
            private_asr_aggregate_exports.c.export_id
        ).where(
            private_asr_aggregate_exports.c.workspace_id == workspace_id,
            private_asr_aggregate_exports.c.environment_id == environment_id,
        ))).scalars().all()
    assert aggregate_rows == []

    async with private_asr_database.migration.begin() as connection:
        await connection.execute(insert(private_asr_source_fences).values(
            workspace_id=workspace_id, environment_id=environment_id,
            purpose="asr_model_evaluation", source_revision_id=uuid4(),
            revoked=False, epoch=0, reason="synthetic_fixture", updated_at=NOW,
        ))
        await connection.execute(insert(private_asr_aggregate_exports).values(
            export_id=uuid4(), workspace_id=workspace_id, environment_id=environment_id,
            purpose="asr_model_evaluation", privacy_policy_version="fixture:v1",
            audit_receipt_id=report_audit_id, payload={"status": "NOT_VERIFIED"},
            created_at=NOW,
        ))
    async with sessions() as session, session.begin():
        await session.execute(text("select set_config('spine.workspace_id', :value, true)"),
                              {"value": str(workspace_id)})
        await session.execute(text("select set_config('spine.environment_id', :value, true)"),
                              {"value": str(environment_id)})
        await session.execute(text("select set_config('spine.purpose', :value, true)"),
                              {"value": "unrelated_operation"})
        assert (await session.execute(select(private_asr_artifacts.c.artifact_id))).scalars().all() == []
        assert (await session.execute(select(private_asr_source_fences.c.source_revision_id))).scalars().all() == []
        assert (await session.execute(select(private_asr_report_sources.c.report_id))).scalars().all() == []
        assert (await session.execute(select(private_asr_aggregate_exports.c.export_id))).scalars().all() == []

    denied_queries: list[str] = []

    def capture_query(conn, cursor, statement, parameters, context, executemany):
        denied_queries.append(statement)

    sqlalchemy_event.listen(private_asr_database.runtime.sync_engine,
                            "before_cursor_execute", capture_query)
    try:
        wrong_environment = boundary.interactive(
            scope=EnvironmentScope(workspace_id=workspace_id, environment_id=uuid4()),
            acting_subject_id=UUID(int=11),
            purpose=PersistencePurpose("asr_model_evaluation"),
            operation=PersistenceOperation("run_private_asr_evaluation"),
            trace_id=uuid4(),
        )
        with pytest.raises(PrivateASRStorageDenied) as cross_scope_error:
            await store.read_private_reference(receipt.report_ref, context=wrong_environment)
        assert str(workspace_id) not in str(cross_scope_error.value)
        assert str(revision.revision_id) not in str(cross_scope_error.value)
        assert not any("private_asr_artifacts.payload" in query for query in denied_queries)
    finally:
        sqlalchemy_event.remove(private_asr_database.runtime.sync_engine,
                                "before_cursor_execute", capture_query)

    revocation_context = boundary.worker(
        scope=EnvironmentScope(workspace_id=workspace_id, environment_id=environment_id),
        service_principal_id=UUID(int=12),
        purpose=PersistencePurpose("asr_model_evaluation"),
        operation=PersistenceOperation("private_asr_revocation"),
        trace_id=uuid4(),
    )
    await store.fence(revision.revision_id, reason="synthetic_revocation", context=revocation_context)
    with pytest.raises(PrivateASRStorageDenied):
        await store.read_private_reference(receipt.report_ref, context=context)
