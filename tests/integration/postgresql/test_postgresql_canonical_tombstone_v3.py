from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import SecretStr
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from spine.application.canonical_tombstone_selectors import (
    CanonicalTombstoneSelectorError,
)
from spine.application.persistence.canonical import CanonicalAcceptanceCommand
from spine.application.tombstone_authorization import (
    IssueTombstoneAuthorizationCommand,
    TombstoneAuthorizationOutcome,
    TombstoneAuthorizationService,
)
from spine.application.diagnostics.audit import AuditEventRegistry
from spine.application.persistence.errors import (
    CanonicalEligibilityError,
    TransactionDeadlockError,
)
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    WorkspaceScope,
)
from spine.application.persistence.repositories import SourceObservationCommand
from spine.application.persistence.retry import (
    RetryEligibility,
    TransactionRetryPolicy,
    run_with_transaction_retry,
)
from spine.domain.audit import AuditOutcome, CommandAuditPayload
from spine.domain.sources.canonical_tombstone_selector import (
    CanonicalTombstoneWriterMode,
    TrustedCanonicalTombstoneVersionSelector,
)
from spine.domain.sources.lifecycle import (
    LifecycleDecisionEffect,
    LifecycleEligibilityStatus,
    LifecycleObservationKind,
    LifecycleReasonCode,
)
from spine.infrastructure.persistence.postgresql_canonical_tombstone_selectors import (
    PostgreSQLCanonicalTombstoneSelectorRepository,
)
from spine.infrastructure.db.migrations import locate_migration_assets
from spine.infrastructure.db.operator import bootstrap_database_roles
from spine.infrastructure.db.settings import (
    MigrationDatabaseSettings,
    OperatorDatabaseSettings,
)
from spine.infrastructure.db.test_harness import (
    DISPOSABLE_MARKER_PREFIX,
    TestDatabaseProvision,
    TestTarget,
    validate_target_database_name,
)
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.persistence.postgresql import PostgreSQLPersistence
from tests.contracts.persistence.outbox_events import create_outbox_event_registry
from spine.infrastructure.persistence.postgresql_canonical_tombstone import (
    PostgreSQLCurrentTombstoneAuthorizationAuthority,
)
from spine.infrastructure.persistence.postgresql_errors import (
    translate_persistence_error,
)
from tests.contracts.persistence.test_canonical_acceptance_contract import (
    _audit as canonical_audit,
    _canonical_outbox,
)
from tests.contracts.persistence.test_source_observation_contract import (
    provenance as source_provenance,
    tombstone,
)
from tests.application.test_tombstone_authorization import NOW
from tests.integration.postgresql.test_postgresql_tombstone_authorization import (
    _admitted_case,
    _repository_case,
)
from tests.integration.postgresql.test_postgresql_lifecycle_authority import (
    _AllowAuthority,
    _AllowObservationAuthority,
    _DecisionEffects,
    _decision,
    _observation,
)
from tests.integration.postgresql.test_postgresql_retention_applicability import (
    _applicability,
    _repository as retention_repository,
)
from spine.infrastructure.persistence.postgresql_lifecycle import (
    PostgreSQLLifecycleAuthorityRepository,
)
from tests.integration.postgresql.test_postgresql_unit_of_work import (
    DISPATCHER_ROLE,
    MIGRATION_ROLE,
    PersistenceAdapter,
    PostgreSQLContractDatabase,
    POSTGRESQL_SEARCH_PATH_OPTIONS,
    RUNTIME_ROLE,
    postgresql_adapter,
    postgresql_contract_database,
    synthetic_uuid,
)
from tests.integration.postgresql.conftest import postgresql_provision

pytestmark = [pytest.mark.postgresql, pytest.mark.asyncio(loop_scope="session")]


def _selector_context_factory(adapter: PersistenceAdapter, approval):
    assert adapter.canonical_tombstone_selector_context is not None

    def context_factory(_operation: str, workspace_id: UUID, environment_id: UUID):
        return adapter.canonical_tombstone_selector_context(
            workspace_id, environment_id, approval.approver_human_id
        )

    return context_factory


def _selector_record(
    *,
    case,
    approval,
    authorization,
    selector_id: UUID,
    selector_series_id: UUID,
    version: int,
    predecessor_id: UUID | None,
    audit_id: UUID,
    key: str,
    mode: CanonicalTombstoneWriterMode,
) -> TrustedCanonicalTombstoneVersionSelector:
    assert case.source.connection_id is not None
    return TrustedCanonicalTombstoneVersionSelector(
        selector_id=selector_id,
        selector_series_id=selector_series_id,
        selector_version=version,
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        source_object_id=case.source.source_object_id,
        source_contract_id=approval.source_contract_id,
        source_contract_version=approval.source_contract_version,
        provider_kind=authorization.provider_kind,
        connection_id=case.source.connection_id,
        resource_scope_reference=approval.resource_scope_reference,
        mode=mode,
        approval_reference=approval.approval_id,
        predecessor_selector_id=predecessor_id,
        audit_reference=audit_id,
        idempotency_identity=key,
        valid_from=NOW,
        valid_until=NOW + timedelta(days=365),
        created_at=NOW,
    )


def _selector_audit(registry, context, audit_id: UUID):
    return registry.build_event(
        audit_event_id=audit_id,
        workspace_id=context.scope.workspace_id,
        environment_id=context.scope.environment_id,
        event_type="command.outcome",
        schema_version=1,
        payload=CommandAuditPayload(
            command_type="canonical_tombstone_selector.publish"
        ),
        origin=context.origin,
        acting_subject_id=context.acting_subject_id,
        service_principal_id=context.service_principal_id,
        trace_id=context.trace_id,
        occurred_at=NOW,
        outcome=AuditOutcome.ACCEPTED,
        reason="tombstone_selector_published",
    )


async def _build_v3_rollout_state(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
):
    (
        case,
        ingestion_context,
        evidence_context,
        authorization_repository,
        evidence,
        approval,
        issuer,
        authorization,
    ) = await _repository_case(postgresql_adapter, postgresql_contract_database, 738000)
    evidence = evidence.model_copy(
        update={
            "fresh_until": NOW + timedelta(days=365),
            "object_namespace": case.source.external_namespace,
            "object_identity": case.source.external_object_id,
        }
    )
    approval = approval.model_copy(update={"valid_until": NOW + timedelta(days=365)})
    issuer = issuer.model_copy(update={"valid_until": NOW + timedelta(days=365)})
    lifecycle_context = postgresql_adapter.ingestion_context(
        case.source.workspace_id,
        case.source.environment_id,
        "lifecycle_decision",
    )
    lifecycle_repository = PostgreSQLLifecycleAuthorityRepository(
        uow_factory=postgresql_adapter.uow_factory,
        observation_authority=_AllowObservationAuthority(),
        decision_authority=_AllowAuthority(),
        decision_effects=_DecisionEffects(),
    )
    lifecycle_observation = _observation(case, 738020).model_copy(
        update={
            "kind": LifecycleObservationKind.MOVE_WITHIN_SCOPE,
            "reason_code": LifecycleReasonCode.MOVE_WITHIN_SCOPE,
        }
    )
    lifecycle_decision = _decision(
        case, lifecycle_observation, lifecycle_context, 738030
    ).model_copy(
        update={
            "effect": LifecycleDecisionEffect.ELIGIBLE,
            "status": LifecycleEligibilityStatus.ELIGIBLE,
            "legal_hold_active": False,
        }
    )
    await lifecycle_repository.record_observation(
        lifecycle_observation, lifecycle_context
    )
    await lifecycle_repository.record_decision(lifecycle_decision, lifecycle_context)
    retention_context = postgresql_adapter.ingestion_context(
        case.source.workspace_id,
        case.source.environment_id,
        "retention_applicability",
    )
    retention = _applicability(
        case,
        retention_context,
        identifier=738040,
    ).model_copy(update={"legal_hold_references": ()})
    await retention_repository(postgresql_adapter).record_applicability(
        retention, retention_context
    )
    authorization = authorization.model_copy(
        update={
            "expires_at": NOW + timedelta(days=365),
            "legal_hold_check_reference": lifecycle_decision.decision_id,
            "legal_hold_generation": lifecycle_decision.generation,
            "retention_eligibility_reference": retention.applicability_id,
            "retention_eligibility_generation": retention.generation,
            "object_namespace": case.source.external_namespace,
            "object_identity": case.source.external_object_id,
        }
    )
    connection_engine = create_async_engine(
        postgresql_contract_database.migration_url.get_secret_value(),
        hide_parameters=True,
    )
    try:
        async with connection_engine.begin() as connection:
            connection_approval_id = synthetic_uuid(738050)
            service_principal_id = synthetic_uuid(738051)
            credential_reference = synthetic_uuid(738052)
            grant_reference = synthetic_uuid(738053)
            await connection.execute(
                text(
                    "INSERT INTO spine.provider_connection_approvals "
                    "(approval_id, workspace_id, environment_id, service_principal_id, "
                    "provider_kind, credential_reference, grant_reference, "
                    "approved_resource_scope_references, authorization_generation, "
                    "configuration_generation, status, recorded_at) VALUES "
                    "(:approval_id, :workspace_id, :environment_id, :principal_id, "
                    ":provider_kind, :credential_reference, :grant_reference, :scopes, "
                    ":authorization_generation, :configuration_generation, 'active', :now)"
                ),
                {
                    "approval_id": connection_approval_id,
                    "workspace_id": authorization.workspace_id,
                    "environment_id": authorization.environment_id,
                    "principal_id": service_principal_id,
                    "provider_kind": authorization.provider_kind,
                    "credential_reference": credential_reference,
                    "grant_reference": grant_reference,
                    "scopes": [authorization.resource_scope_reference],
                    "authorization_generation": authorization.authorization_generation,
                    "configuration_generation": authorization.configuration_generation,
                    "now": NOW,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO spine.provider_connection_versions "
                    "(connection_version_id, connection_id, approval_id, version, "
                    "predecessor_version_id, workspace_id, environment_id, "
                    "service_principal_id, provider_kind, credential_reference, "
                    "grant_reference, approved_resource_scope_references, "
                    "authorization_generation, configuration_generation, status, "
                    "capabilities, health, diagnostic_codes, observed_at, "
                    "cursor_lag_seconds, last_successful_reconciliation_at, receipt_id, "
                    "idempotency_key, command_digest, recorded_at) VALUES "
                    "(:version_id, :connection_id, :approval_id, 1, NULL, :workspace_id, "
                    ":environment_id, :principal_id, :provider_kind, :credential_reference, "
                    ":grant_reference, :scopes, :authorization_generation, "
                    ":configuration_generation, 'active', '[]'::jsonb, 'healthy', "
                    "ARRAY[]::varchar[], :now, NULL, NULL, :receipt_id, "
                    "'issue-738-connection', :digest, :now)"
                ),
                {
                    "version_id": authorization.connection_version_id,
                    "connection_id": authorization.connection_id,
                    "approval_id": connection_approval_id,
                    "workspace_id": authorization.workspace_id,
                    "environment_id": authorization.environment_id,
                    "principal_id": service_principal_id,
                    "provider_kind": authorization.provider_kind,
                    "credential_reference": credential_reference,
                    "grant_reference": grant_reference,
                    "scopes": [authorization.resource_scope_reference],
                    "authorization_generation": authorization.authorization_generation,
                    "configuration_generation": authorization.configuration_generation,
                    "receipt_id": synthetic_uuid(738054),
                    "digest": "e" * 64,
                    "now": NOW,
                },
            )
    finally:
        await connection_engine.dispose()
    await authorization_repository.record_evidence(evidence, evidence_context)
    approval_context = postgresql_adapter.governance_context(
        approval.workspace_id,
        approval.environment_id,
        approval.approver_human_id,
        "approve_tombstone_policy",
    )
    issuer_context = postgresql_adapter.governance_context(
        issuer.workspace_id,
        issuer.environment_id,
        issuer.authorized_by_human_id,
        "activate_tombstone_issuer",
    )
    await authorization_repository.record_policy_approval(approval, approval_context)
    await authorization_repository.record_issuer(issuer, issuer_context)
    context_factory = _selector_context_factory(postgresql_adapter, approval)
    selector_repository = PostgreSQLCanonicalTombstoneSelectorRepository(
        uow_factory=postgresql_adapter.uow_factory,
        context_factory=context_factory,
    )
    selector_registry = AuditEventRegistry.with_default_families()
    selector_context = context_factory(
        "canonical_tombstone_selector.publish",
        case.source.workspace_id,
        case.source.environment_id,
    )
    selector_series_id = synthetic_uuid(738010)
    legacy_selector = _selector_record(
        case=case,
        approval=approval,
        authorization=authorization,
        selector_id=synthetic_uuid(738011),
        selector_series_id=selector_series_id,
        version=1,
        predecessor_id=None,
        audit_id=synthetic_uuid(738012),
        key="issue-738:legacy-reader",
        mode=CanonicalTombstoneWriterMode.LEGACY_V1_ALLOWED,
    )
    await selector_repository.publish(
        legacy_selector,
        expected_current_selector_id=None,
        expected_cas_version=None,
        recorded_at=NOW,
        audit_event=_selector_audit(
            selector_registry, selector_context, legacy_selector.audit_reference
        ),
    )
    async with postgresql_adapter.uow_factory(case.context) as reader_uow:
        legacy_read = await reader_uow.canonical.resolve_acceptance_read(
            case.command.canonical_acceptance_id
        )
        await reader_uow.rollback()
    issuance = await TombstoneAuthorizationService(
        authorization_repository,
        authorization_repository,
        clock=lambda: NOW,
    ).issue(IssueTombstoneAuthorizationCommand(authorization), ingestion_context)
    assert issuance.outcome is TombstoneAuthorizationOutcome.ISSUED
    v3_selector = _selector_record(
        case=case,
        approval=approval,
        authorization=authorization,
        selector_id=synthetic_uuid(738013),
        selector_series_id=selector_series_id,
        version=2,
        predecessor_id=legacy_selector.selector_id,
        audit_id=synthetic_uuid(738014),
        key="issue-738:v3-writer",
        mode=CanonicalTombstoneWriterMode.V3_REQUIRED,
    )
    await selector_repository.publish(
        v3_selector,
        expected_current_selector_id=legacy_selector.selector_id,
        expected_cas_version=1,
        recorded_at=NOW,
        audit_event=_selector_audit(
            selector_registry, selector_context, v3_selector.audit_reference
        ),
    )

    tombstone_revision = tombstone(case.source, authorization.tombstone_revision_id)
    tombstone_provenance = source_provenance(
        case.source, tombstone_revision.revision_id, "issue-738-v3-tombstone-9"
    )
    source_context = postgresql_adapter.source_context(
        case.source.workspace_id, case.source.environment_id
    )
    async with postgresql_adapter.uow_factory(source_context) as source_uow:
        await source_uow.sources.record_observation(
            SourceObservationCommand.create(
                source=case.source,
                revision=tombstone_revision,
                provenance=tombstone_provenance,
                idempotency_key=IdempotencyKey("issue-738:v3-observation"),
            )
        )
        await source_uow.commit()
    acceptance_id = synthetic_uuid(738015)
    audit_id = synthetic_uuid(738016)
    outbox_id = synthetic_uuid(738017)
    v3_command = CanonicalAcceptanceCommand.create(
        source=case.source,
        revision=tombstone_revision,
        provenance=tombstone_provenance,
        expected_current_revision_id=authorization.expected_current_revision_id,
        expected_current_acceptance_id=authorization.expected_current_acceptance_id,
        expected_current_cas_version=authorization.expected_current_cas_version,
        idempotency_key=IdempotencyKey(authorization.idempotency_key),
        canonical_acceptance_id=acceptance_id,
        audit_event_id=audit_id,
        outbox_event_id=outbox_id,
        accepted_at=NOW,
        audit_event=canonical_audit(
            context=case.context,
            source_revision_id=tombstone_revision.revision_id,
            event_id=audit_id,
            reason="source_tombstoned",
            transition="source.tombstoned",
        ),
        outbox_intent=_canonical_outbox(
            postgresql_adapter,
            context=case.context,
            source_object_id=case.source.source_object_id,
            source_revision_id=tombstone_revision.revision_id,
            acceptance_id=acceptance_id,
            generation=2,
            audit_event_id=audit_id,
            event_id=outbox_id,
            transition="source.tombstoned",
        ),
        tombstone_authorization=authorization,
        operation=case.context.operation,
    )
    async with postgresql_adapter.uow_factory(case.context) as writer_uow:
        v3_result = await writer_uow.canonical.accept(v3_command)
        await writer_uow.commit()
    return SimpleNamespace(
        case=case,
        legacy_read=legacy_read,
        authorization=authorization,
        v3_selector=v3_selector,
        acceptance_id=acceptance_id,
        v3_result=v3_result,
    )


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def historical_v3_database(
    postgresql_provision: TestDatabaseProvision,
):
    """A separate disposable database stopped before the irreversible revision."""
    database_name = validate_target_database_name(
        f"spine_test_v3hist_{uuid4().hex[:12]}"
    )
    marker = f"{DISPOSABLE_MARKER_PREFIX}v3-history-{uuid4().hex}"
    base_url = make_url(postgresql_provision.url.get_secret_value())
    admin_url = base_url.set(database="postgres")
    admin_engine = create_async_engine(
        admin_url.render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )
    try:
        async with admin_engine.connect() as connection:
            quoted_name = connection.dialect.identifier_preparer.quote(database_name)
            await connection.execute(text(f"CREATE DATABASE {quoted_name}"))
            await connection.execute(
                text("SELECT set_config('spine.test_marker', :marker, false)"),
                {"marker": marker},
            )
            quoted_marker = marker.replace("'", "''")
            await connection.execute(
                text(f"COMMENT ON DATABASE {quoted_name} IS '{quoted_marker}'")
            )
    finally:
        await admin_engine.dispose()

    target_url = base_url.set(database=database_name)
    target_provision = TestDatabaseProvision(
        url=SecretStr(target_url.render_as_string(hide_password=False)),
        target=TestTarget(
            database_name=database_name,
            disposable_marker=SecretStr(marker),
        ),
    )
    operator = OperatorDatabaseSettings(
        url=target_provision.url,
        database_name=database_name,
        migration_role=MIGRATION_ROLE,
        migration_password=SecretStr("migration-test-secret"),
        runtime_role=RUNTIME_ROLE,
        runtime_password=SecretStr("runtime-test-secret"),
        dispatcher_role=DISPATCHER_ROLE,
        dispatcher_password=SecretStr("dispatcher-test-secret"),
    )
    await bootstrap_database_roles(operator)
    migration_url = target_url.set(
        username=MIGRATION_ROLE, password="migration-test-secret"
    )
    runtime_url = target_url.set(
        username=RUNTIME_ROLE, password="runtime-test-secret"
    )
    database = PostgreSQLContractDatabase(
        migration_url=SecretStr(migration_url.render_as_string(hide_password=False)),
        runtime_url=SecretStr(runtime_url.render_as_string(hide_password=False)),
    )
    migration_settings = MigrationDatabaseSettings(
        url=database.migration_url,
        migration_role=MIGRATION_ROLE,
        runtime_role=RUNTIME_ROLE,
        dispatcher_role=DISPATCHER_ROLE,
    )
    config_path, script_location = locate_migration_assets()
    config = Config(str(config_path))
    config.set_main_option("script_location", str(script_location))
    config.attributes["migration_settings"] = migration_settings
    await asyncio.to_thread(command.upgrade, config, "20270112_111")

    engine = create_async_engine(
        database.runtime_url.get_secret_value(),
        pool_size=2,
        max_overflow=0,
        pool_reset_on_return="rollback",
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(738900),
        secret=b"issue-738-historical-migration-boundary-secret",
    )
    outbox_events = create_outbox_event_registry()
    persistence = PostgreSQLPersistence(
        session_factory=session_factory,
        context_verifier=boundary,
        outbox_events=outbox_events,
    )

    def workspace_context(workspace_id: UUID):
        return boundary.interactive(
            scope=WorkspaceScope(workspace_id=workspace_id),
            acting_subject_id=synthetic_uuid(738901),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation("workspace_repository"),
            trace_id=synthetic_uuid(738902),
        )

    def environment_context(workspace_id: UUID, environment_id: UUID):
        return boundary.worker(
            scope=EnvironmentScope(workspace_id=workspace_id, environment_id=environment_id),
            service_principal_id=synthetic_uuid(738903),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation("environment_repository"),
            trace_id=synthetic_uuid(738904),
        )

    def source_context(workspace_id: UUID, environment_id: UUID):
        return boundary.worker(
            scope=EnvironmentScope(workspace_id=workspace_id, environment_id=environment_id),
            service_principal_id=synthetic_uuid(738905),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation("source_observation"),
            trace_id=synthetic_uuid(738906),
        )

    def ingestion_context(workspace_id: UUID, environment_id: UUID, operation: str):
        return boundary.worker(
            scope=EnvironmentScope(workspace_id=workspace_id, environment_id=environment_id),
            service_principal_id=synthetic_uuid(738907),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation(operation),
            trace_id=synthetic_uuid(738908),
        )

    def governance_context(workspace_id: UUID, environment_id: UUID, actor_id: UUID, operation: str):
        return boundary.interactive(
            scope=EnvironmentScope(workspace_id=workspace_id, environment_id=environment_id),
            acting_subject_id=actor_id,
            purpose=PersistencePurpose("source_governance"),
            operation=PersistenceOperation(operation),
            trace_id=synthetic_uuid(738909),
        )

    def canonical_selector_context(workspace_id: UUID, environment_id: UUID, actor_id: UUID):
        return boundary.interactive(
            scope=EnvironmentScope(workspace_id=workspace_id, environment_id=environment_id),
            acting_subject_id=actor_id,
            purpose=PersistencePurpose("canonical_tombstone_selector"),
            operation=PersistenceOperation("canonical_tombstone_selector.publish"),
            trace_id=synthetic_uuid(738910),
        )

    @asynccontextmanager
    async def hold_transactions():
        yield

    adapter = PersistenceAdapter(
        uow_factory=persistence.uow_factory,
        workspace_context=workspace_context,
        worker_workspace_context=lambda workspace_id: boundary.worker(
            scope=WorkspaceScope(workspace_id=workspace_id),
            service_principal_id=synthetic_uuid(738911),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation("workspace_repository"),
            trace_id=synthetic_uuid(738912),
        ),
        environment_context=environment_context,
        hold_transactions=hold_transactions,
        outbox_events=outbox_events,
        source_context=source_context,
        ingestion_context=ingestion_context,
        governance_context=governance_context,
        canonical_tombstone_selector_context=canonical_selector_context,
    )
    try:
        yield SimpleNamespace(database=database, adapter=adapter, engine=engine)
    finally:
        await engine.dispose()
        cleanup_engine = create_async_engine(
            admin_url.render_as_string(hide_password=False),
            isolation_level="AUTOCOMMIT",
            hide_parameters=True,
        )
        try:
            async with cleanup_engine.connect() as connection:
                identity = (
                    await connection.execute(
                        text(
                            "SELECT datname, shobj_description(oid, 'pg_database') AS marker "
                            "FROM pg_database WHERE datname = :database_name"
                        ),
                        {"database_name": database_name},
                    )
                ).mappings().one_or_none()
                if identity is not None:
                    if identity["marker"] != marker:
                        raise RuntimeError("historical test database ownership marker changed")
                    quoted_name = connection.dialect.identifier_preparer.quote(database_name)
                    await connection.execute(text(f"DROP DATABASE {quoted_name} WITH (FORCE)"))
        finally:
            await cleanup_engine.dispose()


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def v3_rollout_state(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
):
    return await _build_v3_rollout_state(postgresql_adapter, postgresql_contract_database)


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def v3_historical_rollout_state(historical_v3_database):
    return await _build_v3_rollout_state(
        historical_v3_database.adapter, historical_v3_database.database
    )


async def test_v3_reader_first_rollout_reads_legacy_before_writer_activation(
    v3_rollout_state,
) -> None:
    assert v3_rollout_state.legacy_read is not None
    assert v3_rollout_state.legacy_read.acceptance.tombstone_authorization_id is None
    assert v3_rollout_state.v3_result.acceptance is not None


async def test_v3_migration_rollback_preserves_history_and_restores_constraints(
    v3_historical_rollout_state,
    historical_v3_database,
) -> None:
    authorization = v3_historical_rollout_state.authorization
    v3_selector = v3_historical_rollout_state.v3_selector
    acceptance_id = v3_historical_rollout_state.acceptance_id
    postgresql_contract_database = historical_v3_database.database

    migration_settings = MigrationDatabaseSettings(
        url=postgresql_contract_database.migration_url,
        migration_role=MIGRATION_ROLE,
        runtime_role=RUNTIME_ROLE,
        dispatcher_role=DISPATCHER_ROLE,
    )
    config_path, script_location = locate_migration_assets()
    config = Config(str(config_path))
    config.set_main_option("script_location", str(script_location))
    config.attributes["migration_settings"] = migration_settings
    engine = create_async_engine(
        postgresql_contract_database.migration_url.get_secret_value(),
        hide_parameters=True,
    )
    migration_failure: BaseException | None = None
    retained_acceptance: dict[str, object] | None = None
    retained_selector: dict[str, object] | None = None
    try:
        await asyncio.to_thread(command.downgrade, config, "20270111_110")
        async with engine.connect() as connection:
            columns = set(
                (
                    await connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = 'spine' "
                            "AND table_name = 'canonical_acceptances'"
                        )
                    )
                ).scalars()
            )
            selector_table = await connection.scalar(
                text(
                    "SELECT to_regclass("
                    "'spine.trusted_canonical_tombstone_selector_versions')"
                )
            )
            disabled_trigger = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_trigger AS trigger "
                    "JOIN pg_class AS relation ON relation.oid = trigger.tgrelid "
                    "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
                    "WHERE namespace.nspname = 'spine' "
                    "AND relation.relname = 'canonical_acceptances' "
                    "AND trigger.tgname = 'trg_canonical_acceptance_v3_disabled' "
                    "AND NOT trigger.tgisinternal"
                )
            )
            retained_acceptance = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM spine.canonical_acceptances "
                            "WHERE canonical_acceptance_id = :acceptance_id"
                        ),
                        {"acceptance_id": acceptance_id},
                    )
                )
                .mappings()
                .one()
            )
            retained_selector = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM spine.trusted_canonical_tombstone_selector_versions "
                            "WHERE selector_id = :selector_id"
                        ),
                        {"selector_id": v3_selector.selector_id},
                    )
                )
                .mappings()
                .one()
            )
        assert {
            "revision_kind",
            "tombstone_authorization_id",
            "tombstone_authorization_digest",
            "transition_id",
        } <= columns
        assert selector_table == "spine.trusted_canonical_tombstone_selector_versions"
        assert disabled_trigger == 1
        assert retained_acceptance["canonical_acceptance_schema_version"] == 3
        assert (
            retained_acceptance["tombstone_authorization_id"]
            == authorization.authorization_id
        )
        assert (
            retained_selector["mode"] == CanonicalTombstoneWriterMode.V3_REQUIRED.value
        )

    except BaseException as error:
        migration_failure = error
    finally:
        await engine.dispose()

    try:
        await asyncio.to_thread(command.upgrade, config, "20270112_111")
    except BaseException as cleanup_error:
        if migration_failure is not None:
            raise cleanup_error from migration_failure
        raise
    if migration_failure is not None:
        raise migration_failure

    engine = create_async_engine(
        postgresql_contract_database.migration_url.get_secret_value(),
        hide_parameters=True,
    )
    try:
        async with engine.connect() as connection:
            restored_fk = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_constraint "
                    "WHERE conname = 'fk_canonical_acceptances_tombstone_authorization'"
                )
            )
            disabled_trigger = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_trigger "
                    "WHERE tgname = 'trg_canonical_acceptance_v3_disabled'"
                )
            )
            restored_acceptance = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM spine.canonical_acceptances "
                            "WHERE canonical_acceptance_id = :acceptance_id"
                        ),
                        {"acceptance_id": acceptance_id},
                    )
                )
                .mappings()
                .one()
            )
            restored_selector = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM spine.trusted_canonical_tombstone_selector_versions "
                            "WHERE selector_id = :selector_id"
                        ),
                        {"selector_id": v3_selector.selector_id},
                    )
                )
                .mappings()
                .one()
            )
        assert restored_fk == 1
        assert disabled_trigger == 0
        assert restored_acceptance == retained_acceptance
        assert restored_selector == retained_selector
    finally:
        await engine.dispose()


async def test_v3_migration_rollback_disables_new_writer(
    historical_v3_database,
) -> None:
    postgresql_contract_database = historical_v3_database.database
    migration_settings = MigrationDatabaseSettings(
        url=postgresql_contract_database.migration_url,
        migration_role=MIGRATION_ROLE,
        runtime_role=RUNTIME_ROLE,
        dispatcher_role=DISPATCHER_ROLE,
    )
    config_path, script_location = locate_migration_assets()
    config = Config(str(config_path))
    config.set_main_option("script_location", str(script_location))
    config.attributes["migration_settings"] = migration_settings
    engine = create_async_engine(
        postgresql_contract_database.migration_url.get_secret_value(),
        hide_parameters=True,
    )
    rollback_failure: BaseException | None = None
    try:
        await asyncio.to_thread(command.downgrade, config, "20270111_110")
        with pytest.raises(
            DBAPIError,
            match="Canonical acceptance v3 writes are disabled by rollback",
        ):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO spine.canonical_acceptances "
                        "(canonical_acceptance_id, canonical_acceptance_schema_version, "
                        "source_object_id, source_revision_id, revision_kind, workspace_id, "
                        "environment_id, accepted_generation, ordering, provenance_id, "
                        "idempotency_receipt_id, audit_event_id, outbox_event_id, accepted_at) "
                        "VALUES (:id, 3, :id, :id, 'tombstone', :id, :id, 1, "
                        "'expected_current', :id, :id, :id, :id, :accepted_at)"
                    ),
                    {"id": synthetic_uuid(738001), "accepted_at": NOW},
                )
    except BaseException as error:
        rollback_failure = error
    finally:
        await engine.dispose()
    try:
        await asyncio.to_thread(command.upgrade, config, "20270112_111")
    except BaseException as cleanup_error:
        if rollback_failure is not None:
            raise cleanup_error from rollback_failure
        raise
    if rollback_failure is not None:
        raise rollback_failure


async def test_governed_erasure_migration_downgrade_fails_without_schema_change(
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    migration_settings = MigrationDatabaseSettings(
        url=postgresql_contract_database.migration_url,
        migration_role=MIGRATION_ROLE,
        runtime_role=RUNTIME_ROLE,
        dispatcher_role=DISPATCHER_ROLE,
    )
    config_path, script_location = locate_migration_assets()
    config = Config(str(config_path))
    config.set_main_option("script_location", str(script_location))
    config.attributes["migration_settings"] = migration_settings
    engine = create_async_engine(
        postgresql_contract_database.migration_url.get_secret_value(),
        hide_parameters=True,
    )
    try:
        with pytest.raises(RuntimeError, match="governed erasure migration is irreversible"):
            await asyncio.to_thread(command.downgrade, config, "20270113_112")
        async with engine.connect() as connection:
            assert await connection.scalar(
                text("SELECT version_num FROM spine.alembic_version")
            ) == "20270114_113"
            assert await connection.scalar(
                text("SELECT to_regprocedure('spine.execute_source_disposition_erasure(uuid,uuid,uuid,integer)')")
            ) is not None
    finally:
        await engine.dispose()


async def test_postgresql_deadlock_is_translated_and_retried_within_bound(
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    engine = create_async_engine(
        postgresql_contract_database.migration_url.get_secret_value(),
        hide_parameters=True,
    )
    attempts: list[int] = []
    translated_errors: list[BaseException] = []

    async def deadlocking_transaction(
        first_lock: int,
        second_lock: int,
        ready: asyncio.Event,
        proceed: asyncio.Event,
    ) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": first_lock},
            )
            ready.set()
            await proceed.wait()
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": second_lock},
            )

    async def operation(attempt: int) -> str:
        attempts.append(attempt)
        if attempt > 1:
            return "committed"
        left_ready = asyncio.Event()
        right_ready = asyncio.Event()
        proceed = asyncio.Event()
        left = asyncio.create_task(
            deadlocking_transaction(738201, 738202, left_ready, proceed)
        )
        right = asyncio.create_task(
            deadlocking_transaction(738202, 738201, right_ready, proceed)
        )
        await asyncio.gather(left_ready.wait(), right_ready.wait())
        proceed.set()
        outcomes = await asyncio.gather(left, right, return_exceptions=True)
        database_error = next(
            outcome for outcome in outcomes if isinstance(outcome, DBAPIError)
        )
        translated = translate_persistence_error(database_error)
        translated_errors.append(translated)
        raise translated

    try:
        result = await run_with_transaction_retry(
            operation,
            eligibility=RetryEligibility(
                idempotent=True,
                has_idempotency_key=True,
                reproducible=True,
            ),
            policy=TransactionRetryPolicy(max_attempts=2),
        )
    finally:
        await engine.dispose()

    assert result == "committed"
    assert attempts == [1, 2]
    assert len(translated_errors) == 1
    assert isinstance(translated_errors[0], TransactionDeadlockError)


async def test_tombstone_selector_publication_is_immutable_and_cas_serialized(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    (
        case,
        ingestion_context,
        _,
        authorization_repository,
        _,
        approval,
        _,
        authorization,
        _,
        _,
    ) = await _admitted_case(postgresql_adapter, postgresql_contract_database, 736200)
    context_factory = _selector_context_factory(postgresql_adapter, approval)

    context = context_factory(
        "canonical_tombstone_selector.publish",
        case.source.workspace_id,
        case.source.environment_id,
    )
    repository = PostgreSQLCanonicalTombstoneSelectorRepository(
        uow_factory=postgresql_adapter.uow_factory,
        context_factory=context_factory,
    )
    registry = AuditEventRegistry.with_default_families()
    assert case.source.connection_id is not None

    def selector(
        selector_id: UUID,
        version: int,
        predecessor_id: UUID | None,
        audit_id: UUID,
        key: str,
        mode: CanonicalTombstoneWriterMode = CanonicalTombstoneWriterMode.V3_REQUIRED,
    ) -> TrustedCanonicalTombstoneVersionSelector:
        return TrustedCanonicalTombstoneVersionSelector(
            selector_id=selector_id,
            selector_series_id=synthetic_uuid(736202),
            selector_version=version,
            workspace_id=case.source.workspace_id,
            environment_id=case.source.environment_id,
            source_object_id=case.source.source_object_id,
            source_contract_id=approval.source_contract_id,
            source_contract_version=approval.source_contract_version,
            provider_kind=authorization.provider_kind,
            connection_id=case.source.connection_id,
            resource_scope_reference=approval.resource_scope_reference,
            mode=mode,
            approval_reference=approval.approval_id,
            predecessor_selector_id=predecessor_id,
            audit_reference=audit_id,
            idempotency_identity=key,
            valid_from=NOW,
            valid_until=NOW + timedelta(days=365),
            created_at=NOW,
        )

    def audit(audit_id: UUID):
        return registry.build_event(
            audit_event_id=audit_id,
            workspace_id=case.source.workspace_id,
            environment_id=case.source.environment_id,
            event_type="command.outcome",
            schema_version=1,
            payload=CommandAuditPayload(
                command_type="canonical_tombstone_selector.publish"
            ),
            origin=context.origin,
            acting_subject_id=context.acting_subject_id,
            service_principal_id=context.service_principal_id,
            trace_id=context.trace_id,
            occurred_at=NOW,
            outcome=AuditOutcome.ACCEPTED,
            reason="tombstone_selector_published",
        )

    first = selector(
        synthetic_uuid(736203),
        1,
        None,
        synthetic_uuid(736204),
        "tombstone:first",
        CanonicalTombstoneWriterMode.LEGACY_V1_ALLOWED,
    )
    stored = await repository.publish(
        first,
        expected_current_selector_id=None,
        expected_cas_version=None,
        recorded_at=NOW,
        audit_event=audit(first.audit_reference),
    )
    replay = await repository.publish(
        first,
        expected_current_selector_id=None,
        expected_cas_version=None,
        recorded_at=NOW,
        audit_event=audit(first.audit_reference),
    )
    assert stored.replayed is False
    assert replay.replayed is True
    legacy_command = SimpleNamespace(
        source=case.source,
        tombstone_authorization=None,
    )
    async with postgresql_adapter.uow_factory(ingestion_context) as uow:
        await PostgreSQLCurrentTombstoneAuthorizationAuthority(
            clock=lambda: NOW
        ).revalidate(
            legacy_command, uow
        )  # type: ignore[arg-type]
        await uow.rollback()
    unrelated_command = SimpleNamespace(
        source=case.source.model_copy(
            update={"source_object_id": synthetic_uuid(736299)}
        ),
        tombstone_authorization=None,
    )
    async with postgresql_adapter.uow_factory(ingestion_context) as uow:
        with pytest.raises(CanonicalEligibilityError):
            await PostgreSQLCurrentTombstoneAuthorizationAuthority(
                clock=lambda: NOW
            ).revalidate(
                unrelated_command, uow
            )  # type: ignore[arg-type]
        await uow.rollback()
    issuance = await TombstoneAuthorizationService(
        authorization_repository,
        authorization_repository,
        clock=lambda: NOW,
    ).issue(IssueTombstoneAuthorizationCommand(authorization), ingestion_context)
    assert issuance.outcome is TombstoneAuthorizationOutcome.ISSUED
    second = selector(
        synthetic_uuid(736205),
        2,
        first.selector_id,
        synthetic_uuid(736206),
        "tombstone:second",
    )
    await repository.publish(
        second,
        expected_current_selector_id=first.selector_id,
        expected_cas_version=1,
        recorded_at=NOW,
        audit_event=audit(second.audit_reference),
    )
    async with postgresql_adapter.uow_factory(ingestion_context) as uow:
        with pytest.raises(CanonicalEligibilityError):
            await PostgreSQLCurrentTombstoneAuthorizationAuthority(
                clock=lambda: NOW
            ).revalidate(
                legacy_command, uow
            )  # type: ignore[arg-type]
        await uow.rollback()
    left = selector(
        synthetic_uuid(736207),
        3,
        second.selector_id,
        synthetic_uuid(736208),
        "tombstone:left",
    )
    right = selector(
        synthetic_uuid(736209),
        3,
        second.selector_id,
        synthetic_uuid(736210),
        "tombstone:right",
    )
    outcomes = await asyncio.gather(
        repository.publish(
            left,
            expected_current_selector_id=second.selector_id,
            expected_cas_version=2,
            recorded_at=NOW,
            audit_event=audit(left.audit_reference),
        ),
        repository.publish(
            right,
            expected_current_selector_id=second.selector_id,
            expected_cas_version=2,
            recorded_at=NOW,
            audit_event=audit(right.audit_reference),
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(value, Exception) for value in outcomes) == 1
    assert (
        sum(isinstance(value, CanonicalTombstoneSelectorError) for value in outcomes)
        == 1
    )
