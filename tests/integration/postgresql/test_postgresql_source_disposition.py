from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from spine.application.disposition_authority import binding_from_tombstone_authorization
from spine.application.disposition_authority import DispositionAdmissionUnknownError
from spine.application.disposition_coordinator import DispositionConflictError
from spine.application.diagnostics.audit import AuditEventRegistry
from spine.application.tombstone_authorization import (
    IssueTombstoneAuthorizationCommand,
    TombstoneAuthorizationOutcome,
    TombstoneAuthorizationService,
)
from spine.domain.sources.disposition import (
    DispositionOperation,
    DispositionStatus,
    DispositionStoreState,
)
from spine.domain.sources.canonical_tombstone_selector import CanonicalTombstoneWriterMode
from spine.domain.sources.lifecycle import (
    LifecycleDecisionEffect,
    LifecycleEligibilityStatus,
    LifecycleObservationKind,
    LifecycleReasonCode,
)
from spine.domain.sources.retention import RetentionArtifactClass
from spine.infrastructure.persistence.postgresql_disposition import (
    PostgreSQLDispositionRepository,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    source_disposition_outbox,
)
from spine.infrastructure.persistence.postgresql_canonical_tombstone_selectors import (
    PostgreSQLCanonicalTombstoneSelectorRepository,
)
from spine.infrastructure.persistence.postgresql_lifecycle import (
    PostgreSQLLifecycleAuthorityRepository,
)
from spine.infrastructure.persistence.postgresql_retention import (
    PostgreSQLRetentionApplicabilityRepository,
)
from tests.application.test_tombstone_authorization import NOW
from tests.integration.postgresql.test_postgresql_tombstone_authorization import (
    _repository_case,
)
from tests.integration.postgresql.test_postgresql_canonical_tombstone_v3 import (
    _selector_audit,
    _selector_context_factory,
    _selector_record,
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
from tests.integration.postgresql.test_postgresql_unit_of_work import (
    PersistenceAdapter,
    PostgreSQLContractDatabase,
    postgresql_adapter,
    postgresql_contract_database,
    synthetic_uuid,
)


pytestmark = [pytest.mark.postgresql, pytest.mark.asyncio(loop_scope="session")]


def _operation(binding) -> DispositionOperation:
    return DispositionOperation(
        workspace_id=binding.workspace_id,
        environment_id=binding.environment_id,
        operation_id=binding.operation_id,
        request_digest="d" * 64,
        declared_store_ids=("canonical",),
        authority_binding=binding,
        stores=(DispositionStoreState(store_id="canonical"),),
    )


def _repository(adapter: PersistenceAdapter) -> PostgreSQLDispositionRepository:
    return PostgreSQLDispositionRepository(
        uow_factory=adapter.uow_factory,
        context_factory=adapter.environment_context,
        clock=lambda: NOW,
    )


async def _issued_authorization(
    adapter: PersistenceAdapter,
    database: PostgreSQLContractDatabase,
    number: int,
):
    (
        case,
        context,
        evidence_context,
        authority_repository,
        evidence,
        approval,
        issuer,
        authorization,
    ) = await _repository_case(adapter, database, number)
    evidence = evidence.model_copy(
        update={
            "fresh_until": NOW + timedelta(days=365),
            "object_namespace": case.source.external_namespace,
            "object_identity": case.source.external_object_id,
        }
    )
    lifecycle_context = adapter.ingestion_context(
        case.source.workspace_id, case.source.environment_id, "lifecycle_decision"
    )
    lifecycle_repository = PostgreSQLLifecycleAuthorityRepository(
        uow_factory=adapter.uow_factory,
        observation_authority=_AllowObservationAuthority(),
        decision_authority=_AllowAuthority(),
        decision_effects=_DecisionEffects(),
    )
    lifecycle_observation = _observation(case, number + 70).model_copy(
        update={
            "connection_version_id": authorization.connection_version_id,
            "kind": LifecycleObservationKind.MOVE_WITHIN_SCOPE,
            "reason_code": LifecycleReasonCode.MOVE_WITHIN_SCOPE,
        }
    )
    lifecycle_decision = _decision(
        case, lifecycle_observation, lifecycle_context, number + 80
    ).model_copy(
        update={
            "decision_id": authorization.legal_hold_check_reference,
            "effect": LifecycleDecisionEffect.ELIGIBLE,
            "status": LifecycleEligibilityStatus.ELIGIBLE,
            "legal_hold_active": False,
        }
    )
    await lifecycle_repository.record_observation(
        lifecycle_observation, lifecycle_context
    )
    await lifecycle_repository.record_decision(lifecycle_decision, lifecycle_context)
    retention_context = adapter.ingestion_context(
        case.source.workspace_id, case.source.environment_id, "retention_applicability"
    )
    retention = _applicability(
        case, retention_context, identifier=number + 90,
        artifact_classes=(RetentionArtifactClass.ORIGINAL,),
    ).model_copy(
        update={
            "applicability_id": authorization.retention_eligibility_reference,
            "legal_hold_references": (),
        }
    )
    await retention_repository(adapter).record_applicability(retention, retention_context)
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
    provider_connection_engine = create_async_engine(
        database.migration_url.get_secret_value(), hide_parameters=True
    )
    try:
        async with provider_connection_engine.begin() as connection:
            approval_id = synthetic_uuid(number + 100)
            principal_id = synthetic_uuid(number + 101)
            credential_reference = synthetic_uuid(number + 102)
            grant_reference = synthetic_uuid(number + 103)
            parameters = {
                "approval_id": approval_id,
                "workspace_id": authorization.workspace_id,
                "environment_id": authorization.environment_id,
                "principal_id": principal_id,
                "provider_kind": authorization.provider_kind,
                "credential_reference": credential_reference,
                "grant_reference": grant_reference,
                "scopes": [authorization.resource_scope_reference],
                "authorization_generation": authorization.authorization_generation,
                "configuration_generation": authorization.configuration_generation,
                "now": NOW,
            }
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
                parameters,
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
                    "(:version_id, :connection_id, :approval_id, 1, NULL, "
                    ":workspace_id, :environment_id, :principal_id, :provider_kind, "
                    ":credential_reference, :grant_reference, :scopes, "
                    ":authorization_generation, :configuration_generation, 'active', "
                    "'[]'::jsonb, 'healthy', ARRAY[]::varchar[], :now, NULL, NULL, "
                    ":receipt_id, :idempotency_key, :digest, :now)"
                ),
                {
                    **parameters,
                    "version_id": authorization.connection_version_id,
                    "connection_id": authorization.connection_id,
                    "receipt_id": synthetic_uuid(number + 104),
                    "idempotency_key": f"source-disposition-connection:{number}",
                    "digest": "e" * 64,
                },
            )
    finally:
        await provider_connection_engine.dispose()
    await authority_repository.record_evidence(evidence, evidence_context)
    approval_context = adapter.governance_context(
        approval.workspace_id, approval.environment_id, approval.approver_human_id,
        "approve_tombstone_policy",
    )
    issuer_context = adapter.governance_context(
        issuer.workspace_id, issuer.environment_id, issuer.authorized_by_human_id,
        "activate_tombstone_issuer",
    )
    await authority_repository.record_policy_approval(approval, approval_context)
    await authority_repository.record_issuer(issuer, issuer_context)
    service = TombstoneAuthorizationService(
        authority_repository, authority_repository, clock=lambda: NOW
    )
    issued = await service.issue(IssueTombstoneAuthorizationCommand(authorization), context)
    assert issued.outcome is TombstoneAuthorizationOutcome.ISSUED
    selector_context_factory = _selector_context_factory(adapter, approval)
    selector_context = selector_context_factory(
        "canonical_tombstone_selector.publish",
        case.source.workspace_id,
        case.source.environment_id,
    )
    selector_repository = PostgreSQLCanonicalTombstoneSelectorRepository(
        uow_factory=adapter.uow_factory,
        context_factory=selector_context_factory,
    )
    selector = _selector_record(
        case=case,
        approval=approval,
        authorization=authorization,
        selector_id=synthetic_uuid(number + 11_000),
        selector_series_id=synthetic_uuid(number + 11_001),
        version=1,
        predecessor_id=None,
        audit_id=synthetic_uuid(number + 11_002),
        key=f"source-disposition:{number}:v3",
        mode=CanonicalTombstoneWriterMode.V3_REQUIRED,
    )
    await selector_repository.publish(
        selector,
        expected_current_selector_id=None,
        expected_cas_version=None,
        recorded_at=NOW,
        audit_event=_selector_audit(
            AuditEventRegistry.with_default_families(),
            selector_context,
            selector.audit_reference,
        ),
    )
    return case, authorization


async def _outbox_count(adapter: PersistenceAdapter, operation: DispositionOperation) -> int:
    context = adapter.environment_context(operation.workspace_id, operation.environment_id)
    async with adapter.uow_factory(context) as uow:
        result = await uow.execute_statement(
            select(source_disposition_outbox.c.event_id).where(
                source_disposition_outbox.c.workspace_id == operation.workspace_id,
                source_disposition_outbox.c.environment_id == operation.environment_id,
                source_disposition_outbox.c.operation_id == operation.operation_id,
            )
        )
        rows = result.all()
        await uow.rollback()
        return len(rows)


async def test_postgresql_disposition_admission_persists_one_scoped_outbox(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    _, authorization = await _issued_authorization(
        postgresql_adapter, postgresql_contract_database, 753000
    )
    operation_id = synthetic_uuid(753100)
    binding = binding_from_tombstone_authorization(
        authorization, operation_id=operation_id
    )
    operation = _operation(binding)
    repository = _repository(postgresql_adapter)

    admitted = await repository.admit_if_current(operation, binding)
    replay = await repository.admit_if_current(operation, binding)

    assert admitted == operation
    assert replay == operation
    assert await repository.get(
        workspace_id=operation.workspace_id,
        environment_id=operation.environment_id,
        operation_id=operation.operation_id,
    ) == operation
    assert await _outbox_count(postgresql_adapter, operation) == 1


async def test_postgresql_canonical_source_delete_remains_guarded(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    case, *_ = await _repository_case(
        postgresql_adapter, postgresql_contract_database, 753050
    )
    engine = create_async_engine(
        postgresql_contract_database.migration_url.get_secret_value(),
        hide_parameters=True,
    )
    runtime_engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        hide_parameters=True,
    )
    try:
        with pytest.raises(DBAPIError) as error:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "DELETE FROM spine.source_objects "
                        "WHERE source_object_id = :source_object_id"
                    ),
                    {"source_object_id": case.source.source_object_id},
                )
        assert error.value.orig.diag.constraint_name == "ck_governed_source_erasure_only"
        with pytest.raises(DBAPIError) as runtime_error:
            async with runtime_engine.begin() as connection:
                await connection.execute(
                    text("SELECT set_config('spine.governed_erasure', 'true', true)")
                )
                await connection.execute(
                    text(
                        "DELETE FROM spine.source_objects "
                        "WHERE source_object_id = :source_object_id"
                    ),
                    {"source_object_id": case.source.source_object_id},
                )
        assert runtime_error.value.orig.sqlstate == "42501"
    finally:
        await engine.dispose()
        await runtime_engine.dispose()


async def test_postgresql_erasure_role_is_nonlogin_and_unavailable_to_runtime(
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    migration_engine = create_async_engine(
        postgresql_contract_database.migration_url.get_secret_value(),
        hide_parameters=True,
    )
    runtime_engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        hide_parameters=True,
    )
    try:
        async with migration_engine.connect() as connection:
            role = (
                await connection.execute(
                    text(
                        "SELECT rolcanlogin, rolsuper, rolbypassrls "
                        "FROM pg_roles WHERE rolname = 'spine_erasure_executor'"
                    )
                )
            ).one()
            assert role == (False, False, False)
            migration_membership = (
                await connection.execute(
                    text(
                        "SELECT m.inherit_option, m.set_option, m.admin_option "
                        "FROM pg_auth_members m "
                        "JOIN pg_roles r ON r.oid = m.roleid "
                        "JOIN pg_roles u ON u.oid = m.member "
                        "WHERE r.rolname = 'spine_erasure_executor' "
                        "AND u.rolname = 'spine_migration'"
                    )
                )
            ).one_or_none()
            runtime_membership = (
                await connection.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_auth_members m "
                        "JOIN pg_roles r ON r.oid = m.roleid "
                        "JOIN pg_roles u ON u.oid = m.member "
                        "WHERE r.rolname = 'spine_erasure_executor' "
                        "AND u.rolname = 'spine_runtime')"
                    )
                )
            ).scalar_one()
            assert migration_membership is None
            assert runtime_membership is False
        with pytest.raises(DBAPIError):
            async with runtime_engine.begin() as connection:
                await connection.execute(text("SET ROLE spine_erasure_executor"))
        with pytest.raises(DBAPIError):
            async with migration_engine.begin() as connection:
                await connection.execute(text("SET ROLE spine_erasure_executor"))
        async with runtime_engine.connect() as connection:
            can_delete = (
                await connection.execute(
                    text(
                        "SELECT has_table_privilege(current_user, "
                        "'spine.source_objects', 'DELETE')"
                    )
                )
            ).scalar_one()
            assert can_delete is False
    finally:
        await migration_engine.dispose()
        await runtime_engine.dispose()


async def test_authorized_erasure_executor_physically_removes_supported_source_graph(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    case, authorization = await _issued_authorization(
        postgresql_adapter, postgresql_contract_database, 753600
    )
    unrelated_case, _ = await _issued_authorization(
        postgresql_adapter, postgresql_contract_database, 753602
    )
    operation_id = synthetic_uuid(753601)
    binding = binding_from_tombstone_authorization(
        authorization, operation_id=operation_id
    )
    operation = _operation(binding)
    assert await _repository(postgresql_adapter).admit_if_current(operation, binding)

    engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        hide_parameters=True,
    )
    try:
        async with engine.begin() as connection:
            outcome = await connection.execute(
                text(
                    "SELECT spine.execute_source_disposition_erasure("
                    ":workspace_id, :environment_id, :operation_id, :state_version)"
                ),
                {
                    "workspace_id": binding.workspace_id,
                    "environment_id": binding.environment_id,
                    "operation_id": operation_id,
                    "state_version": operation.state_version,
                },
            )
            assert outcome.scalar_one() == "ERASED"

        async with postgresql_adapter.uow_factory(
            postgresql_adapter.environment_context(
                binding.workspace_id, binding.environment_id
            )
        ) as uow:
            rows = await uow.execute_statement(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM spine.source_objects WHERE source_object_id = :source_id) AS objects, "
                    "(SELECT count(*) FROM spine.source_revisions WHERE source_object_id = :source_id) AS revisions, "
                    "(SELECT count(*) FROM spine.source_revision_provenance WHERE source_object_id = :source_id) AS provenance, "
                    "(SELECT count(*) FROM spine.source_revision_receipt_associations WHERE source_object_id = :source_id) AS receipts, "
                    "(SELECT count(*) FROM spine.canonical_acceptances WHERE source_object_id = :source_id) AS acceptances, "
                    "(SELECT count(*) FROM spine.current_source_heads WHERE source_object_id = :source_id) AS heads, "
                    "(SELECT count(*) FROM spine.qualified_deletion_evidence WHERE source_object_id = :source_id) AS deletion_evidence, "
                    "(SELECT count(*) FROM spine.trusted_canonical_tombstone_selector_versions WHERE source_object_id = :source_id) AS selectors"
                ),
                {"source_id": case.source.source_object_id},
            )
            result = rows.one()._mapping
            assert dict(result) == {
                "objects": 0,
                "revisions": 0,
                "provenance": 0,
                "receipts": 0,
                "acceptances": 0,
                "heads": 0,
                "deletion_evidence": 0,
                "selectors": 0,
            }
            await uow.rollback()

        async with postgresql_adapter.uow_factory(
            postgresql_adapter.environment_context(
                unrelated_case.source.workspace_id,
                unrelated_case.source.environment_id,
            )
        ) as unrelated_uow:
            unrelated_source = await unrelated_uow.execute_statement(
                text(
                    "SELECT count(*) FROM spine.source_objects "
                    "WHERE source_object_id = :source_id"
                ),
                {"source_id": unrelated_case.source.source_object_id},
            )
            assert unrelated_source.scalar_one() == 1
            await unrelated_uow.rollback()

        async with engine.begin() as connection:
            replay = await connection.execute(
                text(
                    "SELECT spine.execute_source_disposition_erasure("
                    ":workspace_id, :environment_id, :operation_id, :state_version)"
                ),
                {
                    "workspace_id": binding.workspace_id,
                    "environment_id": binding.environment_id,
                    "operation_id": operation_id,
                    "state_version": operation.state_version,
                },
            )
            assert replay.scalar_one() == "DENIED"
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("failed_table", "failure_stage"),
    (
        ("source_disposition_operations", "after_write"),
        ("source_disposition_outbox", "after_write"),
    ),
)
async def test_postgresql_disposition_admission_rolls_back_state_and_outbox_on_failure(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
    failed_table: str,
    failure_stage: str,
) -> None:
    _, authorization = await _issued_authorization(
        postgresql_adapter,
        postgresql_contract_database,
        754000 + (1 if failed_table == "source_disposition_outbox" else 0),
    )
    operation_id = synthetic_uuid(
        754100 + (1 if failed_table == "source_disposition_outbox" else 0)
    )
    binding = binding_from_tombstone_authorization(
        authorization, operation_id=operation_id
    )
    operation = _operation(binding)

    class FailAfterInsert:
        def __init__(self, wrapped) -> None:
            self._wrapped = wrapped

        def __call__(self, context):
            @asynccontextmanager
            async def transaction():
                async with self._wrapped(context) as uow:
                    class UnitOfWorkProxy:
                        async def execute_statement(self, statement, parameters=None):
                            result = await uow.execute_statement(statement, parameters)
                            table = getattr(statement, "table", None)
                            if (
                                failure_stage == "after_write"
                                and getattr(table, "name", None) == failed_table
                            ):
                                raise RuntimeError("synthetic transaction-stage failure")
                            return result

                        async def commit(self) -> None:
                            await uow.commit()

                        async def rollback(self) -> None:
                            await uow.rollback()

                    yield UnitOfWorkProxy()

            return transaction()

    repository = PostgreSQLDispositionRepository(
        uow_factory=FailAfterInsert(postgresql_adapter.uow_factory),
        context_factory=postgresql_adapter.environment_context,
        clock=lambda: NOW,
    )

    with pytest.raises(DispositionAdmissionUnknownError):
        await repository.admit_if_current(operation, binding)

    assert await _repository(postgresql_adapter).get(
        workspace_id=operation.workspace_id,
        environment_id=operation.environment_id,
        operation_id=operation.operation_id,
    ) is None
    assert await _outbox_count(postgresql_adapter, operation) == 0


async def test_postgresql_disposition_cas_is_tenant_scoped_and_replay_safe(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    _, authorization = await _issued_authorization(
        postgresql_adapter, postgresql_contract_database, 753200
    )
    binding = binding_from_tombstone_authorization(
        authorization, operation_id=synthetic_uuid(753300)
    )
    operation = _operation(binding)
    repository = _repository(postgresql_adapter)
    assert await repository.admit_if_current(operation, binding) == operation

    incomplete = operation.model_copy(
        update={
            "state_version": 2,
            "status": DispositionStatus.INCOMPLETE,
            "safe_code": "STORE_FAILED",
        }
    )
    await repository.save(incomplete, expected_state_version=1)
    with pytest.raises(DispositionConflictError):
        await repository.save(
            incomplete.model_copy(update={"state_version": 3}),
            expected_state_version=1,
        )
    assert await repository.get(
        workspace_id=operation.workspace_id,
        environment_id=operation.environment_id,
        operation_id=operation.operation_id,
    ) == incomplete
    (
        foreign_case,
        _,
        _,
        _,
        _,
        _,
        _,
        _,
    ) = await _repository_case(
        postgresql_adapter, postgresql_contract_database, 753900
    )
    assert await repository.get(
        workspace_id=foreign_case.source.workspace_id,
        environment_id=foreign_case.source.environment_id,
        operation_id=operation.operation_id,
    ) is None


async def test_postgresql_disposition_recovers_commit_with_lost_response(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    _, authorization = await _issued_authorization(
        postgresql_adapter, postgresql_contract_database, 753400
    )
    binding = binding_from_tombstone_authorization(
        authorization, operation_id=synthetic_uuid(753500)
    )
    operation = _operation(binding)

    class LoseFirstCommittedResponse:
        def __init__(self, wrapped) -> None:
            self.wrapped = wrapped
            self.lose_response = True

        def __call__(self, context):
            @asynccontextmanager
            async def transaction():
                async with self.wrapped(context) as uow:
                    yield uow
                if self.lose_response:
                    self.lose_response = False
                    raise TimeoutError("committed response was lost")

            return transaction()

    wrapped_factory = LoseFirstCommittedResponse(postgresql_adapter.uow_factory)
    repository = PostgreSQLDispositionRepository(
        uow_factory=wrapped_factory,
        context_factory=postgresql_adapter.environment_context,
        clock=lambda: NOW,
    )

    admitted = await repository.admit_if_current(operation, binding)

    assert admitted == operation
    assert not wrapped_factory.lose_response
    assert await repository.get(
        workspace_id=operation.workspace_id,
        environment_id=operation.environment_id,
        operation_id=operation.operation_id,
    ) == operation
    assert await _outbox_count(postgresql_adapter, operation) == 1
