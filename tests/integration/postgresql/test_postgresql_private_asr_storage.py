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
    AdmittedSourceRevision, PrivateArtifactDeletionAuthorization,
    PrivateArtifactDeletionRequest, PrivateReportSourceBinding,
)
from spine.domain.sources.retention import (
    RetentionApplicability, RetentionArtifactClass, RetentionAuthorityKind,
    RetentionTargetKind,
)
from spine.domain.evaluations.private_asr_corpus import AnnotationStatus, CorpusSplit, GroundTruthAnnotation
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
    private_asr_source_fences,
)
from spine.infrastructure.private_asr.audit import PostgreSQLPrivateASRAuditReceipts
from spine.infrastructure.private_asr.retention import PostgreSQLPrivateASRRetentionResolver
from spine.infrastructure.private_asr.retention import PrivateASRRetentionDisposition
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
        self.terms_digest = "d" * 64
        self.duration_seconds = 1800

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
            terms_digest=self.terms_digest,
            starting_event=(
                RetentionStartingEvent.REPORT_GENERATED
                if artifact_class is RetentionArtifactClass.PRIVATE_ASR_DETAILED_REPORT
                else RetentionStartingEvent.ARTIFACT_CREATED
            ),
            duration_seconds=self.duration_seconds,
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


class _SyntheticArtifactDeletionAuthority:
    """Fixture-scoped deletion grant; never registered by production wiring."""

    async def authorize_deletion(self, request, *, context, transaction):
        del transaction
        allowed = (
            (
                request.action == "retention_expiration"
                and request.artifact_kind in {"original", "annotation", "report"}
                and request.retention_deadline <= request.requested_at
            )
            or (request.action == "staging_cleanup" and request.artifact_kind == "staging")
        ) and (
            context.scope.workspace_id == request.workspace_id
            and context.scope.environment_id == request.environment_id
        )
        return PrivateArtifactDeletionAuthorization(
            request_digest=request.calculate_digest(),
            authorization_ref=PrivateReference(
                reference_id=synthetic_uuid(798001),
                reference_kind="private_asr_deletion_authorization",
            ),
            authority_version="synthetic-deletion-authority:v1",
            revocation_epoch=1,
            valid_until=NOW + timedelta(days=1),
            authorized=allowed,
        )


@pytest.mark.asyncio
async def test_postgresql_private_asr_expired_original_is_tombstoned_with_audit(
    private_asr_database: _Database,
) -> None:
    clock = _ControlledClock(NOW)
    prepared = await _prepare_synthetic_report_case(
        private_asr_database,
        seed=798000,
        deletion_authority=_SyntheticArtifactDeletionAuthority(),
        clock=clock,
    )
    deletion_clock = NOW + timedelta(minutes=31)
    clock.current = deletion_clock
    cleanup_context = prepared.boundary.worker(
        scope=EnvironmentScope(
            workspace_id=prepared.case.source.workspace_id,
            environment_id=prepared.case.source.environment_id,
        ),
        service_principal_id=synthetic_uuid(798020),
        purpose=PersistencePurpose("asr_model_evaluation"),
        operation=PersistenceOperation("private_asr_retention_expiration"),
        trace_id=synthetic_uuid(798021),
    )

    await prepared.store.expire(
        prepared.original_ref,
        policy_ref=prepared.applicability_ref,
        context=cleanup_context,
    )
    async with prepared.runtime_sessions() as session, session.begin():
        await prepared.store._set_scope(session, cleanup_context)
        row = (await session.execute(select(
            private_asr_artifacts.c.status,
            private_asr_artifacts.c.payload,
            private_asr_artifacts.c.tombstoned_at,
        ).where(
            private_asr_artifacts.c.artifact_id == prepared.original_ref.reference_id,
            private_asr_artifacts.c.workspace_id == prepared.case.source.workspace_id,
            private_asr_artifacts.c.environment_id == prepared.case.source.environment_id,
        ))).one()
        deletion_events = (await session.execute(select(func.count()).select_from(
            audit_events
        ).where(
            audit_events.c.workspace_id == prepared.case.source.workspace_id,
            audit_events.c.environment_id == prepared.case.source.environment_id,
            audit_events.c.target_id == prepared.original_ref.reference_id,
            audit_events.c.reason == "private_asr_artifact_expired",
        ))).scalar_one()
    assert row.status == "tombstoned"
    assert row.payload == b""
    assert row.tombstoned_at == deletion_clock
    assert deletion_events == 1

    await prepared.store.expire(
        prepared.original_ref,
        policy_ref=prepared.applicability_ref,
        context=cleanup_context,
    )


@pytest.mark.asyncio
async def test_postgresql_private_asr_report_expires_after_original_tombstone(
    private_asr_database: _Database,
) -> None:
    clock = _ControlledClock(NOW)
    prepared = await _prepare_synthetic_report_case(
        private_asr_database,
        seed=798050,
        deletion_authority=_SyntheticArtifactDeletionAuthority(),
        clock=clock,
    )
    report = await _commit_synthetic_report(prepared, idempotency_key="report-expiry-order")
    clock.current = NOW + timedelta(minutes=31)
    cleanup_context = prepared.boundary.worker(
        scope=EnvironmentScope(
            workspace_id=prepared.case.source.workspace_id,
            environment_id=prepared.case.source.environment_id,
        ),
        service_principal_id=synthetic_uuid(798070),
        purpose=PersistencePurpose("asr_model_evaluation"),
        operation=PersistenceOperation("private_asr_retention_expiration"),
        trace_id=synthetic_uuid(798071),
    )
    await prepared.store.expire(
        prepared.original_ref,
        policy_ref=prepared.applicability_ref,
        context=cleanup_context,
    )
    await prepared.store.expire(
        report.report_ref,
        policy_ref=prepared.applicability_ref,
        context=cleanup_context,
    )
    async with prepared.runtime_sessions() as session, session.begin():
        await prepared.store._set_scope(session, cleanup_context)
        rows = (await session.execute(select(
            private_asr_artifacts.c.kind,
            private_asr_artifacts.c.status,
            private_asr_artifacts.c.payload,
        ).where(
            private_asr_artifacts.c.artifact_id.in_(
                (prepared.original_ref.reference_id, report.report_ref.reference_id)
            ),
        ))).all()
    assert len(rows) == 2
    assert all(row.status == "tombstoned" and row.payload == b"" for row in rows)


@pytest.mark.asyncio
async def test_postgresql_private_asr_expiration_denies_before_deadline(
    private_asr_database: _Database,
) -> None:
    prepared = await _prepare_synthetic_report_case(
        private_asr_database,
        seed=798100,
        deletion_authority=_SyntheticArtifactDeletionAuthority(),
    )
    cleanup_context = prepared.boundary.worker(
        scope=EnvironmentScope(
            workspace_id=prepared.case.source.workspace_id,
            environment_id=prepared.case.source.environment_id,
        ),
        service_principal_id=synthetic_uuid(798120),
        purpose=PersistencePurpose("asr_model_evaluation"),
        operation=PersistenceOperation("private_asr_retention_expiration"),
        trace_id=synthetic_uuid(798121),
    )
    with pytest.raises(PrivateASRStorageDenied, match="deletion authorization denied"):
        await prepared.store.expire(
            prepared.original_ref,
            policy_ref=prepared.applicability_ref,
            context=cleanup_context,
        )
    async with prepared.runtime_sessions() as session, session.begin():
        await prepared.store._set_scope(session, cleanup_context)
        row = (await session.execute(select(
            private_asr_artifacts.c.status, private_asr_artifacts.c.payload,
        ).where(private_asr_artifacts.c.artifact_id == prepared.original_ref.reference_id))).one()
        assert row.status == "committed"
        assert row.payload != b""


@pytest.mark.asyncio
async def test_postgresql_private_asr_expiration_denies_without_deletion_authority(
    private_asr_database: _Database,
) -> None:
    clock = _ControlledClock(NOW)
    prepared = await _prepare_synthetic_report_case(
        private_asr_database, seed=798200, clock=clock,
    )
    clock.current = NOW + timedelta(minutes=31)
    cleanup_context = prepared.boundary.worker(
        scope=EnvironmentScope(
            workspace_id=prepared.case.source.workspace_id,
            environment_id=prepared.case.source.environment_id,
        ),
        service_principal_id=synthetic_uuid(798220),
        purpose=PersistencePurpose("asr_model_evaluation"),
        operation=PersistenceOperation("private_asr_retention_expiration"),
        trace_id=synthetic_uuid(798221),
    )
    with pytest.raises(PrivateASRStorageDenied, match="deletion authorization denied"):
        await prepared.store.expire(
            prepared.original_ref,
            policy_ref=prepared.applicability_ref,
            context=cleanup_context,
        )
    async with prepared.runtime_sessions() as session, session.begin():
        await prepared.store._set_scope(session, cleanup_context)
        row = (await session.execute(select(
            private_asr_artifacts.c.status, private_asr_artifacts.c.payload,
        ).where(private_asr_artifacts.c.artifact_id == prepared.original_ref.reference_id))).one()
        assert row.status == "committed"
        assert row.payload != b""


@pytest.mark.asyncio
async def test_postgresql_private_asr_staging_cleanup_requires_exact_authority(
    private_asr_database: _Database,
) -> None:
    prepared = await _prepare_synthetic_report_case(
        private_asr_database,
        seed=798300,
        deletion_authority=_SyntheticArtifactDeletionAuthority(),
        leave_staging=True,
    )
    cleanup_context = prepared.boundary.worker(
        scope=EnvironmentScope(
            workspace_id=prepared.case.source.workspace_id,
            environment_id=prepared.case.source.environment_id,
        ),
        service_principal_id=synthetic_uuid(798320),
        purpose=PersistencePurpose("asr_model_evaluation"),
        operation=PersistenceOperation("private_asr_cleanup"),
        trace_id=synthetic_uuid(798321),
    )
    await prepared.store.discard(prepared.staging, context=cleanup_context)
    async with prepared.runtime_sessions() as session, session.begin():
        await prepared.store._set_scope(session, cleanup_context)
        row = (await session.execute(select(
            private_asr_artifacts.c.status, private_asr_artifacts.c.payload,
        ).where(private_asr_artifacts.c.artifact_id == prepared.staging.reference_id))).one()
        audit_count = (await session.execute(select(func.count()).select_from(audit_events).where(
            audit_events.c.target_id == prepared.staging.reference_id,
            audit_events.c.reason == "private_asr_staging_discarded",
        ))).scalar_one()
    assert row.status == "tombstoned"
    assert row.payload == b""
    assert audit_count == 1


@pytest.mark.asyncio
async def test_postgresql_private_asr_annotation_is_persisted_and_read_under_authority(
    private_asr_database: _Database,
) -> None:
    prepared = await _prepare_synthetic_report_case(private_asr_database, seed=798350)
    annotation = GroundTruthAnnotation(
        annotation_id=synthetic_uuid(798351),
        version=1,
        source_record_id=prepared.record_id,
        status=AnnotationStatus.DRAFT,
        authored_by=prepared.admitted.actor_id,
        author_authorization_ref=PrivateReference(
            reference_id=synthetic_uuid(798352),
            reference_kind="synthetic_annotation_authorization",
        ),
        authored_at=NOW,
        segments=(),
    )
    payload = annotation.model_dump_json().encode()
    reference = await prepared.store.store_immutable_annotation(
        prepared.admitted,
        prepared.record_id,
        annotation.version,
        payload,
        retention_profile_ref=prepared.applicability_ref,
        audit_receipt_ref=prepared.admitted.audit_receipt_ref,
        context=prepared.context,
    )
    assert await prepared.store.read_private_reference(
        reference, context=prepared.context,
    ) == payload


@pytest.mark.asyncio
async def test_postgresql_private_asr_source_revocation_tombstones_derivatives(
    private_asr_database: _Database,
) -> None:
    prepared = await _prepare_synthetic_report_case(private_asr_database, seed=798360)
    report = await _commit_synthetic_report(prepared, idempotency_key="revoked-report")
    revocation_context = prepared.boundary.worker(
        scope=EnvironmentScope(
            workspace_id=prepared.case.source.workspace_id,
            environment_id=prepared.case.source.environment_id,
        ),
        service_principal_id=synthetic_uuid(798361),
        purpose=PersistencePurpose("asr_model_evaluation"),
        operation=PersistenceOperation("private_asr_revocation"),
        trace_id=synthetic_uuid(798362),
    )
    await prepared.store.fence(
        prepared.case.revision.revision_id,
        reason="synthetic_source_withdrawal",
        context=revocation_context,
    )
    async with prepared.runtime_sessions() as session, session.begin():
        await prepared.store._set_scope(session, revocation_context)
        states = (await session.execute(select(
            private_asr_artifacts.c.status, private_asr_artifacts.c.payload,
        ).where(
            private_asr_artifacts.c.workspace_id == prepared.case.source.workspace_id,
            private_asr_artifacts.c.environment_id == prepared.case.source.environment_id,
            private_asr_artifacts.c.source_revision_id == prepared.case.revision.revision_id,
        ))).all()
        fence = (await session.execute(select(private_asr_source_fences.c.revoked).where(
            private_asr_source_fences.c.workspace_id == prepared.case.source.workspace_id,
            private_asr_source_fences.c.environment_id == prepared.case.source.environment_id,
            private_asr_source_fences.c.source_revision_id == prepared.case.revision.revision_id,
        ))).scalar_one()
    assert fence is True
    assert states
    assert all(status == "tombstoned" and payload == b"" for status, payload in states)
    with pytest.raises(PrivateASRStorageDenied):
        await prepared.store.read_private_reference(report.report_ref, context=prepared.context)


@pytest.mark.asyncio
async def test_postgresql_private_asr_cleanup_rejects_report_and_forged_reference_generically(
    private_asr_database: _Database,
) -> None:
    prepared = await _prepare_synthetic_report_case(
        private_asr_database,
        seed=798400,
        deletion_authority=_SyntheticArtifactDeletionAuthority(),
    )
    stored = await _commit_synthetic_report(prepared, idempotency_key="cleanup-denial")
    cleanup_context = prepared.boundary.worker(
        scope=EnvironmentScope(
            workspace_id=prepared.case.source.workspace_id,
            environment_id=prepared.case.source.environment_id,
        ),
        service_principal_id=synthetic_uuid(798420),
        purpose=PersistencePurpose("asr_model_evaluation"),
        operation=PersistenceOperation("private_asr_cleanup"),
        trace_id=synthetic_uuid(798421),
    )
    messages = []
    for reference in (
        stored.report_ref,
        PrivateReference(
            reference_id=synthetic_uuid(798422),
            reference_kind="private_asr_staging",
        ),
    ):
        with pytest.raises(PrivateASRStorageDenied) as denied:
            await prepared.store.delete_private_reference(reference, context=cleanup_context)
        messages.append(str(denied.value))
    assert messages[0] == messages[1]
    assert prepared.case.source.source_object_id.hex not in messages[0]


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


class _ControlledClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


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
async def test_postgresql_private_asr_missing_retention_terms_deny_report_publication(
    private_asr_database: _Database,
) -> None:
    prepared = await _prepare_synthetic_report_case(private_asr_database, seed=798500)
    store_without_retention = PostgreSQLPrivateASRStorage(
        sessions=prepared.runtime_sessions,
        contexts=prepared.boundary,
        authority=prepared.authority,
        access_policies=prepared.policies,
        audit_receipts=prepared.audit_receipts,
        retention_resolver=None,
        manifest_lineage=prepared.manifest_lineage,
        uow_factory=prepared.persistence.uow_factory,
        clock=prepared.clock,
    )
    callback_called = False

    async def authorize_commit() -> None:
        nonlocal callback_called
        callback_called = True

    with pytest.raises(PrivateASRStorageDenied, match="storage authorization denied"):
        await store_without_retention.store_private_report(
            prepared.report.model_dump_json().encode(),
            report_id=prepared.report_id,
            idempotency_key="missing-retention-terms",
            source_bindings=prepared.bindings,
            retention_profile_ref=prepared.applicability_ref,
            audit_receipt_ref=prepared.report_receipt_ref,
            context=prepared.context,
            authorize_commit=authorize_commit,
        )
    assert callback_called is False
    async with prepared.runtime_sessions() as session, session.begin():
        await prepared.store._set_scope(session, prepared.context)
        assert (await session.execute(select(private_asr_artifacts.c.artifact_id).where(
            private_asr_artifacts.c.idempotency_key == "missing-retention-terms",
        ))).scalar_one_or_none() is None
        assert (await session.execute(select(audit_events.c.audit_event_id).where(
            audit_events.c.audit_event_id == prepared.report_receipt_ref.reference_id,
        ))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_postgresql_private_asr_runtime_role_enforces_forced_rls(
    private_asr_database: _Database,
) -> None:
    prepared = await _prepare_synthetic_report_case(private_asr_database, seed=798600)
    async with prepared.runtime_sessions() as session, session.begin():
        await prepared.store._set_scope(session, prepared.context)
        rls = (await session.execute(text(
            """SELECT role.rolsuper, role.rolbypassrls, relation.relrowsecurity,
                      relation.relforcerowsecurity
                 FROM pg_roles AS role
                 CROSS JOIN pg_class AS relation
                 JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                WHERE role.rolname = current_user
                  AND namespace.nspname = 'spine'
                  AND relation.relname = 'private_asr_artifacts'"""
        ))).one()
        own_rows = (await session.execute(select(private_asr_artifacts.c.artifact_id).where(
            private_asr_artifacts.c.artifact_id == prepared.original_ref.reference_id,
        ))).scalars().all()
    assert tuple(rls) == (False, False, True, True)
    assert own_rows == [prepared.original_ref.reference_id]


@pytest.mark.asyncio
async def test_postgresql_private_asr_read_denial_has_no_existence_oracle_or_payload_select(
    private_asr_database: _Database,
) -> None:
    prepared = await _prepare_synthetic_report_case(private_asr_database, seed=798700)
    statements: list[str] = []

    def capture_statement(conn, cursor, statement, parameters, execution_context, executemany):
        statements.append(statement.lower())

    event.listen(private_asr_database.runtime.sync_engine, "before_cursor_execute", capture_statement)
    messages = []
    try:
        for reference_id in (prepared.original_ref.reference_id, synthetic_uuid(798701)):
            reference = PrivateReference(
                reference_id=reference_id, reference_kind="private_asr_report",
            )
            with pytest.raises(PrivateASRStorageDenied) as denial:
                await prepared.store.read_private_reference(reference, context=prepared.context)
            messages.append(str(denial.value))
    finally:
        event.remove(
            private_asr_database.runtime.sync_engine, "before_cursor_execute", capture_statement,
        )
    assert messages[0] == messages[1]
    assert not any("select spine.private_asr_artifacts.payload" in statement for statement in statements)


async def _prepare_synthetic_report_case(
    private_asr_database: _Database, *, seed: int = 795000,
    deletion_authority=None, clock=None, leave_staging: bool = False,
) -> SimpleNamespace:
    clock = clock or (lambda: NOW)
    runtime_sessions = async_sessionmaker(
        private_asr_database.runtime, expire_on_commit=False, autoflush=False,
    )
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(seed + 100),
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
            acting_subject_id=synthetic_uuid(seed + 101),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation("workspace_repository"),
            trace_id=synthetic_uuid(seed + 102),
        )

    def worker_workspace_context(workspace_id):
        return boundary.worker(
            scope=WorkspaceScope(workspace_id=workspace_id),
            service_principal_id=synthetic_uuid(seed + 103),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation("workspace_repository"),
            trace_id=synthetic_uuid(seed + 104),
        )

    def source_context(workspace_id, environment_id):
        return boundary.worker(
            scope=EnvironmentScope(workspace_id=workspace_id, environment_id=environment_id),
            service_principal_id=synthetic_uuid(seed + 105),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation("source_observation"),
            trace_id=synthetic_uuid(seed + 106),
        )

    def ingestion_context(workspace_id, environment_id, operation):
        return boundary.worker(
            scope=EnvironmentScope(workspace_id=workspace_id, environment_id=environment_id),
            service_principal_id=synthetic_uuid(seed + 107),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation(operation),
            trace_id=synthetic_uuid(seed + 108),
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
    case = await prepare_case(adapter, seed + 110, original_content=raw)
    async with persistence.uow_factory(case.context) as uow:
        await uow.canonical.accept(case.command)
        await uow.commit()

    governance_context = ingestion_context(
        case.source.workspace_id, case.source.environment_id, "retention_applicability"
    )
    policy_id, policy_version_id = synthetic_uuid(seed + 120), synthetic_uuid(seed + 121)
    policy_digest = "a" * 64
    applicability = RetentionApplicability(
        applicability_id=synthetic_uuid(seed + 122),
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
        applicability_evidence_reference=synthetic_uuid(seed + 123),
        applicability_evidence_digest="b" * 64,
        approving_authority_reference=synthetic_uuid(seed + 124),
        approving_authority_version_id=synthetic_uuid(seed + 125),
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
        audit_event_id=synthetic_uuid(seed + 126),
        outbox_event_id=synthetic_uuid(seed + 127),
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
        trace_id=synthetic_uuid(seed + 128),
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
    dataset_id, record_id = synthetic_uuid(seed + 132), synthetic_uuid(seed + 131)
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
    terms_authority = _SyntheticRetentionTerms(policy_id, policy_version_id, policy_digest)
    resolver = PostgreSQLPrivateASRRetentionResolver(
        approvals=_SyntheticRetentionApproval(),
        terms=terms_authority,
    )
    store = PostgreSQLPrivateASRStorage(
        sessions=runtime_sessions,
        contexts=boundary,
        authority=authority,
        access_policies=policies,
        audit_receipts=audit_receipts,
        retention_resolver=resolver,
        manifest_lineage=manifest_lineage,
        deletion_authority=deletion_authority,
        uow_factory=persistence.uow_factory,
        clock=clock,
    )
    staging = await store.begin(admitted, context=context)
    if leave_staging:
        original_ref = None
    else:
        await store.append(staging, raw, context=context)
        await store.finalize(
            staging, sha256=sha256(raw).hexdigest(), byte_length=len(raw), context=context,
        )
        original_ref = await store.bind_immutable_original(
            admitted, sha256=sha256(raw).hexdigest(), byte_length=len(raw), context=context,
        )

    report_id = synthetic_uuid(seed + 130)
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
    return SimpleNamespace(**locals())


async def _commit_synthetic_report(prepared, *, idempotency_key, callback=None):
    callback = callback or _allow_commit
    return await prepared.store.store_private_report(
        prepared.report.model_dump_json().encode(),
        report_id=prepared.report_id,
        idempotency_key=idempotency_key,
        source_bindings=prepared.bindings,
        retention_profile_ref=prepared.applicability_ref,
        audit_receipt_ref=prepared.report_receipt_ref,
        context=prepared.context,
        authorize_commit=callback,
    )


async def _allow_commit() -> None:
    return None


def _storage_for_transaction_probe(prepared, *, uow_factory):
    return PostgreSQLPrivateASRStorage(
        sessions=prepared.runtime_sessions,
        contexts=prepared.boundary,
        authority=prepared.authority,
        access_policies=prepared.policies,
        audit_receipts=prepared.audit_receipts,
        retention_resolver=prepared.resolver,
        manifest_lineage=prepared.manifest_lineage,
        uow_factory=uow_factory,
        clock=prepared.clock,
    )


@pytest.mark.asyncio
async def test_postgresql_private_asr_callback_denial_rolls_back_report_and_receipt(
    private_asr_database: _Database,
) -> None:
    prepared = await _prepare_synthetic_report_case(private_asr_database, seed=799000)

    async def deny_commit() -> None:
        raise RuntimeError("synthetic callback denial")

    with pytest.raises(PrivateASRStorageDenied, match="report authorization denied"):
        await _commit_synthetic_report(
            prepared, idempotency_key="callback-denial", callback=deny_commit,
        )
    async with prepared.runtime_sessions() as session, session.begin():
        await prepared.store._set_scope(session, prepared.context)
        assert (await session.execute(select(private_asr_artifacts.c.artifact_id).where(
            private_asr_artifacts.c.report_id == prepared.report_id,
            private_asr_artifacts.c.kind == "report",
        ))).scalar_one_or_none() is None
        assert (await session.execute(select(audit_events.c.audit_event_id).where(
            audit_events.c.audit_event_id == prepared.report_receipt_ref.reference_id,
        ))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_postgresql_private_asr_cancellation_during_authorization_rolls_back(
    private_asr_database: _Database,
) -> None:
    prepared = await _prepare_synthetic_report_case(private_asr_database, seed=799050)
    entered, never_release = asyncio.Event(), asyncio.Event()

    async def suspended_authorization() -> None:
        entered.set()
        await never_release.wait()

    task = asyncio.create_task(_commit_synthetic_report(
        prepared, idempotency_key="cancelled-authorization", callback=suspended_authorization,
    ))
    await asyncio.wait_for(entered.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with prepared.runtime_sessions() as session, session.begin():
        await prepared.store._set_scope(session, prepared.context)
        report = (await session.execute(select(private_asr_artifacts.c.artifact_id).where(
            private_asr_artifacts.c.report_id == prepared.report_id,
        ))).scalar_one_or_none()
        receipt = (await session.execute(select(audit_events.c.audit_event_id).where(
            audit_events.c.audit_event_id == prepared.report_receipt_ref.reference_id,
        ))).scalar_one_or_none()
    assert report is None
    assert receipt is None


@pytest.mark.asyncio
async def test_postgresql_private_asr_cancellation_after_audit_flush_rolls_back(
    private_asr_database: _Database,
) -> None:
    prepared = await _prepare_synthetic_report_case(private_asr_database, seed=799060)

    async def cancel_after_audit_flush(stage):
        if stage is PostgreSQLTransactionStage.AFTER_AUDIT_FLUSH:
            raise asyncio.CancelledError

    persistence = PostgreSQLPersistence(
        session_factory=prepared.runtime_sessions,
        context_verifier=prepared.boundary,
        outbox_events=create_outbox_event_registry(),
        transaction_probe=cancel_after_audit_flush,
    )
    probed_store = _storage_for_transaction_probe(
        prepared, uow_factory=persistence.uow_factory,
    )
    with pytest.raises(asyncio.CancelledError):
        await probed_store.store_private_report(
            prepared.report.model_dump_json().encode(),
            report_id=prepared.report_id,
            idempotency_key="cancel-after-audit",
            source_bindings=prepared.bindings,
            retention_profile_ref=prepared.applicability_ref,
            audit_receipt_ref=prepared.report_receipt_ref,
            context=prepared.context,
            authorize_commit=_allow_commit,
        )
    async with prepared.runtime_sessions() as session, session.begin():
        await prepared.store._set_scope(session, prepared.context)
        report = (await session.execute(select(private_asr_artifacts.c.artifact_id).where(
            private_asr_artifacts.c.report_id == prepared.report_id,
        ))).scalar_one_or_none()
        receipt = (await session.execute(select(audit_events.c.audit_event_id).where(
            audit_events.c.audit_event_id == prepared.report_receipt_ref.reference_id,
        ))).scalar_one_or_none()
    assert report is None
    assert receipt is None


@pytest.mark.asyncio
async def test_postgresql_private_asr_lifecycle_lock_fences_report_commit(
    private_asr_database: _Database,
) -> None:
    prepared = await _prepare_synthetic_report_case(private_asr_database, seed=799100)
    callback_called = False

    async def authorize_commit() -> None:
        nonlocal callback_called
        callback_called = True

    lifecycle_lock_requested = asyncio.Event()

    def observe_lifecycle_wait(conn, cursor, statement, parameters, execution_context, executemany):
        if "pg_advisory_xact_lock_shared" in statement.lower():
            lifecycle_lock_requested.set()

    event.listen(
        private_asr_database.runtime.sync_engine,
        "before_cursor_execute", observe_lifecycle_wait,
    )
    try:
        async with prepared.runtime_sessions() as lifecycle_session:
            async with lifecycle_session.begin():
                await lock_source_lifecycle_exclusive(
                    lifecycle_session.execute,
                    workspace_id=prepared.case.source.workspace_id,
                    environment_id=prepared.case.source.environment_id,
                    source_object_id=prepared.case.source.source_object_id,
                )
                pending_commit = asyncio.create_task(_commit_synthetic_report(
                    prepared, idempotency_key="lifecycle-race", callback=authorize_commit,
                ))
                await asyncio.wait_for(lifecycle_lock_requested.wait(), timeout=10)
                assert callback_called is False
        stored = await pending_commit
    finally:
        event.remove(
            private_asr_database.runtime.sync_engine,
            "before_cursor_execute", observe_lifecycle_wait,
        )
    assert callback_called is True
    assert stored.report_ref.reference_kind == "private_asr_report"


@pytest.mark.asyncio
async def test_postgresql_private_asr_authorized_synthetic_report_commit(
    private_asr_database: _Database,
) -> None:
    prepared = await _prepare_synthetic_report_case(private_asr_database, seed=799200)
    stored = await _commit_synthetic_report(prepared, idempotency_key="authorized-report")
    assert stored.audit_receipt_ref == prepared.report_receipt_ref
    assert await prepared.store.read_private_reference(
        stored.report_ref, context=prepared.context,
    ) == prepared.report.model_dump_json().encode()
    async with prepared.runtime_sessions() as session, session.begin():
        await prepared.store._set_scope(session, prepared.context)
        report_row = (await session.execute(select(
            private_asr_artifacts.c.status,
            private_asr_artifacts.c.retention_profile_ref,
            private_asr_artifacts.c.expires_at,
            private_asr_artifacts.c.retention_disposition,
        ).where(
            private_asr_artifacts.c.artifact_id == stored.report_ref.reference_id,
        ))).one()
        receipt_count = (await session.execute(select(func.count()).select_from(audit_events).where(
            audit_events.c.audit_event_id == prepared.report_receipt_ref.reference_id,
        ))).scalar_one()
        report_count = (await session.execute(select(func.count()).select_from(
            private_asr_artifacts
        ).where(private_asr_artifacts.c.idempotency_key == "authorized-report"))).scalar_one()
    assert report_row.status == "committed"
    assert report_row.retention_profile_ref == prepared.applicability.applicability_id
    assert report_row.expires_at == NOW + timedelta(minutes=30)
    disposition = PrivateASRRetentionDisposition.model_validate(
        report_row.retention_disposition[0]["disposition"]
    )
    assert disposition.applicability_id == prepared.applicability.applicability_id
    assert disposition.applicability_generation == 1
    assert disposition.policy_id == prepared.policy_id
    assert disposition.policy_version_id == prepared.policy_version_id
    assert disposition.policy_digest == "a" * 64
    assert disposition.terms_id == synthetic_uuid(796001)
    assert disposition.terms_digest == "d" * 64
    assert disposition.expires_at == NOW + timedelta(minutes=30)
    assert report_count == receipt_count == 1


@pytest.mark.asyncio
async def test_postgresql_private_asr_same_key_retry_returns_stable_identity(
    private_asr_database: _Database,
) -> None:
    prepared = await _prepare_synthetic_report_case(private_asr_database, seed=799300)
    first = await _commit_synthetic_report(prepared, idempotency_key="idempotent-report")
    replay = await _commit_synthetic_report(prepared, idempotency_key="idempotent-report")
    assert replay.report_ref == first.report_ref
    assert replay.audit_receipt_ref == first.audit_receipt_ref


@pytest.mark.asyncio
async def test_postgresql_private_asr_concurrent_same_key_commits_have_one_identity(
    private_asr_database: _Database,
) -> None:
    prepared = await _prepare_synthetic_report_case(private_asr_database, seed=799310)
    concurrent = await asyncio.gather(*(
        _commit_synthetic_report(prepared, idempotency_key="concurrent-report")
        for _ in range(2)
    ))
    assert concurrent[0].report_ref == concurrent[1].report_ref
    async with prepared.runtime_sessions() as session, session.begin():
        await prepared.store._set_scope(session, prepared.context)
        reports = (await session.execute(select(func.count()).select_from(
            private_asr_artifacts
        ).where(private_asr_artifacts.c.idempotency_key == "concurrent-report"))).scalar_one()
        receipts = (await session.execute(select(func.count()).select_from(audit_events).where(
            audit_events.c.audit_event_id == prepared.report_receipt_ref.reference_id,
        ))).scalar_one()
    assert reports == receipts == 1
@pytest.mark.asyncio
async def test_postgresql_private_asr_same_key_different_payload_is_rejected(
    private_asr_database: _Database,
) -> None:
    prepared = await _prepare_synthetic_report_case(private_asr_database, seed=799320)
    await _commit_synthetic_report(prepared, idempotency_key="conflicting-report")
    changed = prepared.report.model_copy(update={
        "candidate_id": "synthetic-candidate:v2",
    })
    with pytest.raises(PrivateASRStorageDenied, match="idempotency conflict"):
        await prepared.store.store_private_report(
            changed.model_dump_json().encode(),
            report_id=prepared.report_id,
            idempotency_key="conflicting-report",
            source_bindings=prepared.bindings,
            retention_profile_ref=prepared.applicability_ref,
            audit_receipt_ref=prepared.report_receipt_ref,
            context=prepared.context,
            authorize_commit=_allow_commit,
        )


@pytest.mark.asyncio
async def test_postgresql_private_asr_stale_retention_terms_deny_before_payload_read(
    private_asr_database: _Database,
) -> None:
    prepared = await _prepare_synthetic_report_case(private_asr_database, seed=799400)
    stored = await _commit_synthetic_report(prepared, idempotency_key="stale-terms")
    prepared.terms_authority.terms_digest = "f" * 64
    payload_selects: list[str] = []

    def observe_payload_select(conn, cursor, statement, parameters, execution_context, executemany):
        if "select spine.private_asr_artifacts.payload" in statement.lower():
            payload_selects.append(statement)

    event.listen(
        private_asr_database.runtime.sync_engine, "before_cursor_execute", observe_payload_select,
    )
    try:
        with pytest.raises(PrivateASRStorageDenied):
            await prepared.store.read_private_reference(stored.report_ref, context=prepared.context)
    finally:
        event.remove(
            private_asr_database.runtime.sync_engine, "before_cursor_execute", observe_payload_select,
        )
    assert payload_selects == []


@pytest.mark.asyncio
async def test_postgresql_private_asr_audit_failure_rolls_back_report_and_receipt(
    private_asr_database: _Database,
) -> None:
    prepared = await _prepare_synthetic_report_case(private_asr_database, seed=799500)

    async def fail_after_audit_flush(stage):
        if stage is PostgreSQLTransactionStage.AFTER_AUDIT_FLUSH:
            raise RuntimeError("synthetic audit persistence failure")

    failing_persistence = PostgreSQLPersistence(
        session_factory=prepared.runtime_sessions,
        context_verifier=prepared.boundary,
        outbox_events=create_outbox_event_registry(),
        transaction_probe=fail_after_audit_flush,
    )
    failing_store = _storage_for_transaction_probe(
        prepared, uow_factory=failing_persistence.uow_factory,
    )
    with pytest.raises(PrivateASRStorageDenied, match="audit authorization unavailable"):
        await failing_store.store_private_report(
            prepared.report.model_dump_json().encode(),
            report_id=prepared.report_id,
            idempotency_key="audit-failure",
            source_bindings=prepared.bindings,
            retention_profile_ref=prepared.applicability_ref,
            audit_receipt_ref=prepared.report_receipt_ref,
            context=prepared.context,
            authorize_commit=_allow_commit,
        )
    async with prepared.runtime_sessions() as session, session.begin():
        await prepared.store._set_scope(session, prepared.context)
        assert (await session.execute(select(private_asr_artifacts.c.artifact_id).where(
            private_asr_artifacts.c.report_id == prepared.report_id,
        ))).scalar_one_or_none() is None
        assert (await session.execute(select(audit_events.c.audit_event_id).where(
            audit_events.c.audit_event_id == prepared.report_receipt_ref.reference_id,
        ))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_postgresql_private_asr_retention_cas_waits_for_report_commit(
    private_asr_database: _Database,
) -> None:
    prepared = await _prepare_synthetic_report_case(private_asr_database, seed=799600)
    callback_entered, release_callback = asyncio.Event(), asyncio.Event()

    async def held_authorization() -> None:
        callback_entered.set()
        await release_callback.wait()

    publication = asyncio.create_task(_commit_synthetic_report(
        prepared, idempotency_key="retention-cas-race", callback=held_authorization,
    ))
    await asyncio.wait_for(callback_entered.wait(), timeout=10)
    replacement = prepared.applicability.model_copy(update={
        "applicability_id": synthetic_uuid(799641),
        "expected_current_applicability_id": prepared.applicability.applicability_id,
        "expected_current_generation": 1,
        "generation": 2,
        "audit_event_id": synthetic_uuid(799642),
        "outbox_event_id": synthetic_uuid(799643),
        "approved_at": NOW + timedelta(seconds=1),
    })
    lock_requested = asyncio.Event()

    def observe_retention_cas(conn, cursor, statement, parameters, execution_context, executemany):
        if "current_retention_applicabilities" in statement.lower():
            lock_requested.set()

    event.listen(
        private_asr_database.runtime.sync_engine, "before_cursor_execute", observe_retention_cas,
    )
    try:
        replacement_task = asyncio.create_task(
            prepared.retention_repository.record_applicability(
                replacement, prepared.governance_context,
            )
        )
        await asyncio.wait_for(lock_requested.wait(), timeout=10)
        assert replacement_task.done() is False
        release_callback.set()
        await publication
        assert await replacement_task == replacement
    finally:
        release_callback.set()
        event.remove(
            private_asr_database.runtime.sync_engine, "before_cursor_execute", observe_retention_cas,
        )
    with pytest.raises(PrivateASRStorageDenied):
        await prepared.store.read_private_reference(
            PrivateReference(
                reference_id=uuid5(NAMESPACE_URL,
                    f"spine-private-asr-report:v1:{prepared.case.source.workspace_id}:"
                    f"{prepared.case.source.environment_id}:retention-cas-race"),
                reference_kind="private_asr_report",
            ),
            context=prepared.context,
        )
