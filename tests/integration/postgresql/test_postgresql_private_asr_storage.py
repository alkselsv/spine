from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import event, func, insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from spine.application.persistence.context import (
    EnvironmentScope, PersistenceOperation, PersistencePurpose, WorkspaceScope,
    TrustedPersistenceContext,
)
from spine.application.diagnostics.audit import AuditEventRegistry
from spine.application.retention import (
    ApprovedRetentionTerms, RetentionStartingEvent,
)
from spine.application.private_asr_corpus import (
    AdmittedSourceRevision, PrivateReportSourceBinding,
)
from spine.domain.sources.retention import (
    RetentionApplicability, RetentionArtifactClass, RetentionAuthorityKind,
    RetentionTargetKind,
)
from spine.domain.evaluations.private_asr_corpus import CorpusSplit
from spine.domain.evaluations.private_asr_qualification import (
    PrivateCaseDisposition, PrivateCaseOutcome, PrivateEvaluationDenominators,
    PrivateRealASREvaluationReport,
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
from spine.infrastructure.persistence.postgresql_mappings import (
    audit_events, current_retention_applicabilities, environments, outbox_intents,
    retention_applicabilities, workspaces,
)
from spine.infrastructure.persistence.postgresql import (
    PostgreSQLPersistence, PostgreSQLTransactionStage,
)
from spine.infrastructure.persistence.postgresql_retention import PostgreSQLRetentionApplicabilityRepository
from spine.infrastructure.persistence.postgresql_source_locks import lock_source_lifecycle_exclusive
from spine.infrastructure.private_asr.postgresql_storage import (
    PostgreSQLPrivateASRStorage, PrivateASRStorageDenied, private_asr_artifacts,
)
from spine.infrastructure.private_asr.audit import PostgreSQLPrivateASRAuditReceipts
from spine.infrastructure.private_asr.retention import PostgreSQLPrivateASRRetentionResolver
from tests.application.test_private_asr_corpus import service, source_revision
from tests.contracts.persistence.ids import synthetic_uuid
from tests.contracts.persistence.outbox_events import create_outbox_event_registry
from tests.contracts.persistence.test_canonical_acceptance_contract import prepare_case
from tests.contracts.persistence.test_source_observation_contract import NOW as CANONICAL_NOW


pytestmark = [pytest.mark.postgresql, pytest.mark.asyncio(loop_scope="session")]

MIGRATION_ROLE = "spine_migration"
RUNTIME_ROLE = "spine_runtime"
DISPATCHER_ROLE = "spine_dispatcher"
MIGRATION_PASSWORD = "migration-asr-storage-secret"
RUNTIME_PASSWORD = "runtime-asr-storage-secret"
DISPATCHER_PASSWORD = "dispatcher-asr-storage-secret"
NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)


class _SyntheticRetentionApproval:
    async def verify_current(self, binding, context, transaction) -> bool:
        if (
            binding.workspace_id != context.scope.workspace_id
            or binding.environment_id != context.scope.environment_id
            or binding.approving_authority_reference.int < 1
        ):
            return False
        statement = select(audit_events.c.audit_event_id).where(
            audit_events.c.workspace_id == binding.workspace_id,
            audit_events.c.environment_id == binding.environment_id,
            audit_events.c.audit_event_id == binding.audit_event_id,
        )
        execute = getattr(transaction, "execute_statement", None)
        result = await (execute(statement) if execute else transaction.execute(statement))
        return result.scalar_one_or_none() == binding.audit_event_id


class _SyntheticRetentionTerms:
    """Explicit test-only terms; never registered by production composition."""

    def __init__(self, policy_id, version_id, policy_digest):
        self.policy_id = policy_id
        self.version_id = version_id
        self.policy_digest = policy_digest

    async def resolve(self, binding, artifact_class, context, transaction):
        del context, transaction
        if (
            binding.retention_policy_id != self.policy_id
            or binding.retention_policy_version_id != self.version_id
            or binding.retention_policy_digest != self.policy_digest
        ):
            return None
        return ApprovedRetentionTerms(
            retention_policy_id=self.policy_id,
            retention_policy_version_id=self.version_id,
            retention_policy_digest=self.policy_digest,
            artifact_class=artifact_class,
            terms_id=synthetic_uuid(796001),
            terms_version="test-synthetic-retention:v1",
            terms_digest="d" * 64,
            starting_event=(
                RetentionStartingEvent.REPORT_GENERATED
                if artifact_class is RetentionArtifactClass.PRIVATE_ASR_DETAILED_REPORT
                else RetentionStartingEvent.ARTIFACT_CREATED
            ),
            duration_seconds=1800,
            approval_evidence_reference=synthetic_uuid(796002),
            deletion_obligation_reference=synthetic_uuid(796003),
            backup_obligation_reference=synthetic_uuid(796004),
            approved=True,
            revoked=False,
        )


class _SyntheticManifestLineage:
    """Test-only approved manifest authority scoped to one synthetic fixture."""

    def __init__(self, *, dataset_id, manifest_version, manifest_sha256,
                 workspace_id, environment_id, retention_profile_ref, bindings):
        self.dataset_id = dataset_id
        self.manifest_version = manifest_version
        self.manifest_sha256 = manifest_sha256
        self.workspace_id = workspace_id
        self.environment_id = environment_id
        self.retention_profile_ref = retention_profile_ref
        self.bindings = bindings

    async def verify_current(
        self, *, transaction, context, dataset_id, manifest_version,
        manifest_sha256, retention_profile_ref, source_bindings,
    ) -> bool:
        del transaction
        return bool(
            context.scope.workspace_id == self.workspace_id
            and context.scope.environment_id == self.environment_id
            and dataset_id == self.dataset_id
            and manifest_version == self.manifest_version
            and manifest_sha256 == self.manifest_sha256
            and retention_profile_ref == self.retention_profile_ref
            and source_bindings == self.bindings
            and all(item.dataset_id == dataset_id for item in source_bindings)
            and len({item.record_id for item in source_bindings}) == len(source_bindings)
        )


class _SyntheticRetentionWriterAuthority:
    async def verify(self, applicability, context, transaction) -> None:
        assert transaction is not None
        assert applicability.actor_id == context.service_principal_id

    async def is_current(self, applicability, context) -> bool:
        return applicability.actor_id == context.service_principal_id


class _SyntheticRetentionEffects:
    async def persist(self, applicability, context, transaction):
        await transaction.execute_statement(insert(audit_events).values(
            audit_event_id=applicability.audit_event_id,
            workspace_id=applicability.workspace_id,
            environment_id=applicability.environment_id,
            event_type="source.retention.applicability", schema_version=1,
            origin=context.origin.value, acting_subject_id=context.acting_subject_id,
            service_principal_id=context.service_principal_id, trace_id=context.trace_id,
            correlation_id=None, causation_id=None, occurred_at=applicability.approved_at,
            appended_at=applicability.approved_at, target_type="source_retention",
            target_id=applicability.target_reference, target_schema_version=1,
            outcome="committed", reason="synthetic_retention_applicability_approved",
            producer_deduplication_id=f"synthetic-retention:{applicability.applicability_id}",
            payload={"generation": applicability.generation},
        ))
        await transaction.execute_statement(insert(outbox_intents).values(
            event_id=applicability.outbox_event_id,
            workspace_id=applicability.workspace_id,
            environment_id=applicability.environment_id,
            event_type="source.retention.applicability", event_schema_version=1,
            aggregate_type="source_retention", aggregate_id=applicability.target_reference,
            aggregate_schema_version=1,
            producer_deduplication_id=f"synthetic-retention:{applicability.applicability_id}",
            payload={"generation": applicability.generation}, trace_id=context.trace_id,
            correlation_id=None, causation_id=None, created_at=applicability.approved_at,
        ))
        return applicability.audit_event_id, applicability.outbox_event_id


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
    with pytest.raises(PrivateASRStorageDenied, match="retention authorization denied"):
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


@pytest.mark.asyncio
async def test_postgresql_private_asr_authorized_synthetic_report_commit(
    private_asr_database: _Database,
) -> None:
    runtime_sessions = async_sessionmaker(
        private_asr_database.runtime, expire_on_commit=False, autoflush=False,
    )
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(795100),
        secret=b"synthetic-private-asr-positive-path-context-secret",
    )
    outbox_events = create_outbox_event_registry()
    persistence = PostgreSQLPersistence(
        session_factory=runtime_sessions,
        context_verifier=boundary,
        outbox_events=outbox_events,
    )

    def workspace_context(workspace_id):
        return boundary.interactive(
            scope=WorkspaceScope(workspace_id=workspace_id),
            acting_subject_id=synthetic_uuid(795101),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation("workspace_repository"),
            trace_id=synthetic_uuid(795102),
        )

    def worker_workspace_context(workspace_id):
        return boundary.worker(
            scope=WorkspaceScope(workspace_id=workspace_id),
            service_principal_id=synthetic_uuid(795103),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation("workspace_repository"),
            trace_id=synthetic_uuid(795104),
        )

    def source_context(workspace_id, environment_id):
        return boundary.worker(
            scope=EnvironmentScope(workspace_id=workspace_id, environment_id=environment_id),
            service_principal_id=synthetic_uuid(795105),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation("source_observation"),
            trace_id=synthetic_uuid(795106),
        )

    def ingestion_context(workspace_id, environment_id, operation):
        return boundary.worker(
            scope=EnvironmentScope(workspace_id=workspace_id, environment_id=environment_id),
            service_principal_id=synthetic_uuid(795107),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation(operation),
            trace_id=synthetic_uuid(795108),
        )

    adapter = SimpleNamespace(
        uow_factory=persistence.uow_factory,
        outbox_events=outbox_events,
        workspace_context=workspace_context,
        worker_workspace_context=worker_workspace_context,
        source_context=source_context,
        ingestion_context=ingestion_context,
    )
    from tests.contracts.persistence.test_canonical_acceptance_contract import prepare_case

    raw = b"synthetic ASR source bytes for restricted storage integration"
    case = await prepare_case(adapter, 795110, original_content=raw)
    async with persistence.uow_factory(case.context) as uow:
        await uow.canonical.accept(case.command)
        await uow.commit()

    governance_context = ingestion_context(
        case.source.workspace_id, case.source.environment_id, "retention_applicability"
    )
    policy_id, policy_version_id = synthetic_uuid(795120), synthetic_uuid(795121)
    policy_digest = "a" * 64
    applicability = RetentionApplicability(
        applicability_id=synthetic_uuid(795122),
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        target_kind=RetentionTargetKind.EXACT_SOURCE,
        source_object_id=case.source.source_object_id,
        artifact_classes=(
            RetentionArtifactClass.PRIVATE_ASR_STAGING,
            RetentionArtifactClass.PRIVATE_ASR_SOURCE_ORIGINAL,
            RetentionArtifactClass.PRIVATE_ASR_ANNOTATION,
            RetentionArtifactClass.PRIVATE_ASR_DETAILED_REPORT,
        ),
        retention_policy_id=policy_id,
        retention_policy_version_id=policy_version_id,
        retention_policy_digest=policy_digest,
        applicability_evidence_reference=synthetic_uuid(795123),
        applicability_evidence_digest="b" * 64,
        approving_authority_reference=synthetic_uuid(795124),
        approving_authority_version_id=synthetic_uuid(795125),
        approving_authority_digest="c" * 64,
        approving_authority_kind=RetentionAuthorityKind.DATA_PORTAL,
        effective_from=NOW - timedelta(minutes=1),
        effective_until=NOW + timedelta(days=1),
        exception_references=(),
        legal_hold_references=(),
        expected_current_applicability_id=None,
        expected_current_generation=0,
        generation=1,
        actor_id=governance_context.service_principal_id,
        audit_event_id=synthetic_uuid(795126),
        outbox_event_id=synthetic_uuid(795127),
        approved_at=NOW,
    )
    retention_repository = PostgreSQLRetentionApplicabilityRepository(
        uow_factory=persistence.uow_factory,
        authority=_SyntheticRetentionWriterAuthority(),
        effects=_SyntheticRetentionEffects(),
    )
    await retention_repository.record_applicability(applicability, governance_context)

    context = boundary.interactive(
        scope=EnvironmentScope(
            workspace_id=case.source.workspace_id,
            environment_id=case.source.environment_id,
        ),
        acting_subject_id=UUID(int=11),
        purpose=PersistencePurpose("asr_model_evaluation"),
        operation=PersistenceOperation("run_private_asr_evaluation"),
        trace_id=synthetic_uuid(795128),
    )
    request, admission, authority, policies, _, _, _, _ = service(
        now=NOW, expires=NOW + timedelta(hours=2), policy_id=case.policy_id,
    )
    applicability_ref = PrivateReference(
        reference_id=applicability.applicability_id,
        reference_kind="retention_applicability",
    )
    request = request.model_copy(update={
        "source_object": case.source,
        "source_revision": case.revision,
        "workspace_id": case.source.workspace_id,
        "environment_id": case.source.environment_id,
        "allowed_environment_id": case.source.environment_id,
        "retention_profile_ref": applicability_ref,
        "access_policy_version": case.policy_id,
    })
    admitted: AdmittedSourceRevision = await admission.admit(request, context=context)
    audit_receipts = PostgreSQLPrivateASRAuditReceipts(
        uow_factory=persistence.uow_factory,
        events=AuditEventRegistry.with_default_families(),
        clock=lambda: NOW,
    )
    source_audit_ref = await audit_receipts.receipt(
        operation="synthetic_source_admitted",
        source_revision_id=case.revision.revision_id,
        outcome="admitted",
        context=context,
    )
    admitted = admitted.model_copy(update={"audit_receipt_ref": source_audit_ref})
    dataset_id, record_id = synthetic_uuid(795132), synthetic_uuid(795131)
    manifest_version = "synthetic-manifest:v1"
    manifest_sha256 = sha256(b"synthetic private manifest").hexdigest()
    bindings = (PrivateReportSourceBinding(
        record_id=record_id, dataset_id=dataset_id,
        source_object_id=case.source.source_object_id,
        source_revision_id=case.revision.revision_id,
        source_revision_digest=case.revision.revision_digest,
        split=CorpusSplit.DEVELOPMENT,
    ),)
    manifest_lineage = _SyntheticManifestLineage(
        dataset_id=dataset_id, manifest_version=manifest_version,
        manifest_sha256=manifest_sha256, workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        retention_profile_ref=applicability_ref, bindings=bindings,
    )
    resolver = PostgreSQLPrivateASRRetentionResolver(
        approvals=_SyntheticRetentionApproval(),
        terms=_SyntheticRetentionTerms(policy_id, policy_version_id, policy_digest),
    )
    store = PostgreSQLPrivateASRStorage(
        sessions=runtime_sessions,
        contexts=boundary,
        authority=authority,
        access_policies=policies,
        audit_receipts=audit_receipts,
        retention_resolver=resolver,
        manifest_lineage=manifest_lineage,
        uow_factory=persistence.uow_factory,
        clock=lambda: NOW,
    )
    staging = await store.begin(admitted, context=context)
    await store.append(staging, raw, context=context)
    await store.finalize(
        staging, sha256=sha256(raw).hexdigest(), byte_length=len(raw), context=context,
    )
    await store.bind_immutable_original(
        admitted, sha256=sha256(raw).hexdigest(), byte_length=len(raw), context=context,
    )

    report_id = synthetic_uuid(795130)
    report_receipt_ref = await audit_receipts.create_report_receipt(
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        report_id=report_id,
        retention_profile_ref=applicability_ref,
        context=context,
    )
    report = PrivateRealASREvaluationReport(
        report_id=report_id,
        dataset_id=dataset_id,
        manifest_version=manifest_version,
        manifest_sha256=manifest_sha256,
        evaluator_id="synthetic-evaluator:v1",
        evaluator_version="synthetic-evaluator-version:v1",
        normalizer_version="synthetic-normalizer:v1",
        candidate_id="synthetic-candidate:v1",
        candidate_config_digest="e" * 64,
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        generated_at=NOW,
        outcomes=(PrivateCaseOutcome(
            record_ref=PrivateReference(reference_id=record_id, reference_kind="private_asr_record"),
            split=CorpusSplit.DEVELOPMENT,
            disposition=PrivateCaseDisposition.UNANNOTATED,
        ),),
        denominators=PrivateEvaluationDenominators(
            eligible=1, admitted=1, verified=1, annotated=0, scorable=0,
            measured=0, independent_lineages=1, denied=0, revoked=0, unavailable=0,
        ),
        measurement_limitations=(),
        coverage_disposition="synthetic_test_only",
        audit_receipt_ref=report_receipt_ref,
        retention_profile_ref=applicability_ref,
    )
    callback_count = 0

    async def authorize_commit() -> None:
        nonlocal callback_count
        callback_count += 1

    async def deny_commit() -> None:
        raise RuntimeError("synthetic callback denial")

    with pytest.raises(PrivateASRStorageDenied, match="report authorization denied"):
        await store.store_private_report(
            report.model_dump_json().encode(), report_id=report_id,
            idempotency_key="synthetic-authorized-report-v1", source_bindings=bindings,
            retention_profile_ref=applicability_ref,
            audit_receipt_ref=report_receipt_ref,
            context=context, authorize_commit=deny_commit,
        )
    async with runtime_sessions() as session, session.begin():
        await store._set_scope(session, context)
        assert (await session.execute(select(private_asr_artifacts.c.artifact_id).where(
            private_asr_artifacts.c.report_id == report_id,
            private_asr_artifacts.c.kind == "report",
        ))).scalar_one_or_none() is None
        assert (await session.execute(select(audit_events.c.audit_event_id).where(
            audit_events.c.audit_event_id == report_receipt_ref.reference_id,
        ))).scalar_one_or_none() is None

    lifecycle_lock_requested = asyncio.Event()

    def observe_lifecycle_wait(conn, cursor, statement, parameters, execution_context, executemany):
        if "pg_advisory_xact_lock_shared" in statement.lower():
            lifecycle_lock_requested.set()

    event.listen(
        private_asr_database.runtime.sync_engine,
        "before_cursor_execute", observe_lifecycle_wait,
    )
    try:
        async with runtime_sessions() as lifecycle_session:
            async with lifecycle_session.begin():
                await lock_source_lifecycle_exclusive(
                    lifecycle_session.execute,
                    workspace_id=case.source.workspace_id,
                    environment_id=case.source.environment_id,
                    source_object_id=case.source.source_object_id,
                )
                pending_commit = asyncio.create_task(store.store_private_report(
                    report.model_dump_json().encode(), report_id=report_id,
                    idempotency_key="synthetic-authorized-report-v1", source_bindings=bindings,
                    retention_profile_ref=applicability_ref,
                    audit_receipt_ref=report_receipt_ref,
                    context=context, authorize_commit=authorize_commit,
                ))
                await asyncio.wait_for(lifecycle_lock_requested.wait(), timeout=10)
                assert callback_count == 0
        stored = await pending_commit
    finally:
        event.remove(
            private_asr_database.runtime.sync_engine,
            "before_cursor_execute", observe_lifecycle_wait,
        )
    assert callback_count == 1
    assert stored.audit_receipt_ref == report_receipt_ref
    assert await store.read_private_reference(stored.report_ref, context=context) == report.model_dump_json().encode()
    replay = await store.store_private_report(
        report.model_dump_json().encode(), report_id=report_id,
        idempotency_key="synthetic-authorized-report-v1", source_bindings=bindings,
        retention_profile_ref=applicability_ref,
        audit_receipt_ref=report_receipt_ref,
        context=context, authorize_commit=authorize_commit,
    )
    assert callback_count == 2
    assert replay.report_ref == stored.report_ref
    assert replay.audit_receipt_ref == stored.audit_receipt_ref
    changed_payload = report.model_copy(
        update={"coverage_disposition": "changed_synthetic_payload"}
    ).model_dump_json().encode()
    with pytest.raises(PrivateASRStorageDenied, match="idempotency conflict"):
        await store.store_private_report(
            changed_payload, report_id=report_id,
            idempotency_key="synthetic-authorized-report-v1", source_bindings=bindings,
            retention_profile_ref=applicability_ref,
            audit_receipt_ref=report_receipt_ref,
            context=context, authorize_commit=authorize_commit,
        )
    concurrent = await asyncio.gather(*(
        store.store_private_report(
            report.model_dump_json().encode(), report_id=report_id,
            idempotency_key="synthetic-authorized-report-v1", source_bindings=bindings,
            retention_profile_ref=applicability_ref,
            audit_receipt_ref=report_receipt_ref,
            context=context, authorize_commit=authorize_commit,
        ) for _ in range(2)
    ))
    assert callback_count == 4
    assert concurrent[0].report_ref == concurrent[1].report_ref == stored.report_ref

    async with runtime_sessions() as session, session.begin():
        await store._set_scope(session, context)
        report_row = (await session.execute(select(
            private_asr_artifacts.c.status,
            private_asr_artifacts.c.retention_profile_ref,
            private_asr_artifacts.c.expires_at,
        ).where(
            private_asr_artifacts.c.artifact_id == stored.report_ref.reference_id,
            private_asr_artifacts.c.workspace_id == case.source.workspace_id,
            private_asr_artifacts.c.environment_id == case.source.environment_id,
        ))).one()
        report_audit = (await session.execute(select(audit_events.c.audit_event_id).where(
            audit_events.c.audit_event_id == report_receipt_ref.reference_id,
            audit_events.c.workspace_id == case.source.workspace_id,
            audit_events.c.environment_id == case.source.environment_id,
        ))).scalar_one()
        binding_row = (await session.execute(select(
            current_retention_applicabilities.c.current_applicability_id,
            current_retention_applicabilities.c.generation,
        ).where(
            current_retention_applicabilities.c.workspace_id == case.source.workspace_id,
            current_retention_applicabilities.c.environment_id == case.source.environment_id,
            current_retention_applicabilities.c.target_kind == "exact_source",
            current_retention_applicabilities.c.target_reference == case.source.source_object_id,
        ))).one()
        aggregate_count = (await session.execute(text(
            "SELECT count(*) FROM spine.private_asr_aggregate_exports "
            "WHERE workspace_id = :workspace_id AND environment_id = :environment_id"
        ), {"workspace_id": case.source.workspace_id, "environment_id": case.source.environment_id})).scalar_one()
        report_count = (await session.execute(select(func.count()).select_from(
            private_asr_artifacts
        ).where(
            private_asr_artifacts.c.workspace_id == case.source.workspace_id,
            private_asr_artifacts.c.environment_id == case.source.environment_id,
            private_asr_artifacts.c.idempotency_key == "synthetic-authorized-report-v1",
        ))).scalar_one()
        receipt_count = (await session.execute(select(func.count()).select_from(audit_events).where(
            audit_events.c.workspace_id == case.source.workspace_id,
            audit_events.c.environment_id == case.source.environment_id,
            audit_events.c.audit_event_id == report_receipt_ref.reference_id,
        ))).scalar_one()
    assert report_row == ("committed", applicability.applicability_id, NOW + timedelta(minutes=30))
    assert report_audit == report_receipt_ref.reference_id
    assert binding_row == (applicability.applicability_id, 1)
    assert aggregate_count == 0
    assert report_count == receipt_count == 1

    failed_report_id = synthetic_uuid(795145)
    failed_receipt_ref = await audit_receipts.create_report_receipt(
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        report_id=failed_report_id,
        retention_profile_ref=applicability_ref,
        context=context,
    )
    failed_report = report.model_copy(update={
        "report_id": failed_report_id,
        "audit_receipt_ref": failed_receipt_ref,
    })

    async def fail_after_audit_flush(stage):
        if stage is PostgreSQLTransactionStage.AFTER_AUDIT_FLUSH:
            raise RuntimeError("synthetic canonical audit persistence failure")

    failing_persistence = PostgreSQLPersistence(
        session_factory=runtime_sessions,
        context_verifier=boundary,
        outbox_events=create_outbox_event_registry(),
        transaction_probe=fail_after_audit_flush,
    )
    failing_store = PostgreSQLPrivateASRStorage(
        sessions=runtime_sessions, contexts=boundary, authority=authority,
        access_policies=policies, audit_receipts=audit_receipts,
        retention_resolver=resolver, manifest_lineage=manifest_lineage,
        uow_factory=failing_persistence.uow_factory, clock=lambda: NOW,
    )
    with pytest.raises(PrivateASRStorageDenied, match="audit authorization unavailable"):
        await failing_store.store_private_report(
            failed_report.model_dump_json().encode(), report_id=failed_report_id,
            idempotency_key="synthetic-audit-failure-v1", source_bindings=bindings,
            retention_profile_ref=applicability_ref,
            audit_receipt_ref=failed_receipt_ref,
            context=context, authorize_commit=authorize_commit,
        )
    async with runtime_sessions() as session, session.begin():
        await store._set_scope(session, context)
        assert (await session.execute(select(private_asr_artifacts.c.artifact_id).where(
            private_asr_artifacts.c.report_id == failed_report_id,
        ))).scalar_one_or_none() is None
        assert (await session.execute(select(audit_events.c.audit_event_id).where(
            audit_events.c.audit_event_id == failed_receipt_ref.reference_id,
        ))).scalar_one_or_none() is None

    # A canonical retention CAS started while the commit callback is waiting
    # must serialize after the report's locked current-pointer check.
    second_report_id = synthetic_uuid(795140)
    second_receipt_ref = await audit_receipts.create_report_receipt(
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        report_id=second_report_id,
        retention_profile_ref=applicability_ref,
        context=context,
    )
    second_report = report.model_copy(update={
        "report_id": second_report_id,
        "audit_receipt_ref": second_receipt_ref,
    })
    callback_entered, release_callback = asyncio.Event(), asyncio.Event()

    async def held_authorization() -> None:
        callback_entered.set()
        await release_callback.wait()

    concurrent_publication = asyncio.create_task(store.store_private_report(
        second_report.model_dump_json().encode(), report_id=second_report_id,
        idempotency_key="synthetic-retention-race-report-v1", source_bindings=bindings,
        retention_profile_ref=applicability_ref,
        audit_receipt_ref=second_receipt_ref,
        context=context, authorize_commit=held_authorization,
    ))
    await asyncio.wait_for(callback_entered.wait(), timeout=10)
    updated_applicability_id = synthetic_uuid(795141)
    replacement = applicability.model_copy(update={
        "applicability_id": updated_applicability_id,
        "expected_current_applicability_id": applicability.applicability_id,
        "expected_current_generation": 1,
        "generation": 2,
        "audit_event_id": synthetic_uuid(795142),
        "outbox_event_id": synthetic_uuid(795143),
        "approved_at": NOW + timedelta(seconds=1),
    })
    replacement_lock_requested = asyncio.Event()

    def observe_retention_cas(conn, cursor, statement, parameters, execution_context, executemany):
        if "current_retention_applicabilities" in statement.lower():
            replacement_lock_requested.set()

    event.listen(
        private_asr_database.runtime.sync_engine,
        "before_cursor_execute", observe_retention_cas,
    )
    try:
        concurrent_replacement = asyncio.create_task(
            retention_repository.record_applicability(replacement, governance_context)
        )
        await asyncio.wait_for(replacement_lock_requested.wait(), timeout=10)
        assert not concurrent_replacement.done()
        release_callback.set()
        await concurrent_publication
        assert await concurrent_replacement == replacement
    finally:
        release_callback.set()
        event.remove(
            private_asr_database.runtime.sync_engine,
            "before_cursor_execute", observe_retention_cas,
        )
    with pytest.raises(PrivateASRStorageDenied):
        await store.read_private_reference(
            PrivateReference(
                reference_id=uuid5(NAMESPACE_URL,
                    f"spine-private-asr-report:v1:{case.source.workspace_id}:"
                    f"{case.source.environment_id}:synthetic-retention-race-report-v1"),
                reference_kind="private_asr_report",
            ),
            context=context,
        )
