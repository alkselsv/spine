from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
import hashlib
import json
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from spine.application.disposition_authority import binding_from_tombstone_authorization
from spine.application.email_sync import (
    EMAIL_SYNC_PAGE_DIGEST_V2,
    EmailSyncAdmissionState,
    EmailSyncCoordinator,
    EmailSyncEffectReceipt,
    EmailSyncPage,
    EmailSyncPageState,
    EmailSyncStatus,
)
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
    StoreDispositionStatus,
)
from spine.domain.sources.canonical_tombstone_selector import CanonicalTombstoneWriterMode
from spine.domain.sources.lifecycle import (
    LifecycleDecisionEffect,
    LifecycleEligibilityStatus,
    LifecycleObservationKind,
    LifecycleReasonCode,
)
from spine.domain.sources.retention import RetentionArtifactClass
from spine.domain.sources.deletion_authorization import (
    TombstoneAuthorizationDecision,
    TombstoneAuthorizationDisposition,
)
from spine.infrastructure.persistence.postgresql_disposition import (
    PostgreSQLDispositionRepository,
)
from spine.infrastructure.persistence.postgresql_email_sync import (
    PostgreSQLEmailSyncRepository,
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
from spine.infrastructure.persistence.postgresql_tombstone_authorization import (
    PostgreSQLTombstoneAuthorizationRepository,
)
from spine.infrastructure.persistence.postgresql_retention import (
    PostgreSQLRetentionApplicabilityRepository,
)
from tests.application.test_tombstone_authorization import NOW
from tests.integration.postgresql.test_postgresql_tombstone_authorization import (
    _CommitVerifier,
    _Effects,
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
    _prepare_lifecycle_case,
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
    *,
    gmail_source: bool = False,
    scope: tuple[UUID, UUID] | None = None,
    create_scope: bool = True,
    gmail_connection_id: UUID | None = None,
    gmail_mailbox_binding_id: UUID | None = None,
    gmail_message_id: str | None = None,
    accepted_generation: int = 1,
    reuse_qualification_binding: bool = False,
    prepared_case: tuple[object, ...] | None = None,
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
    ) = prepared_case or await _repository_case(
            adapter,
            database,
            number,
            gmail_source=gmail_source,
            scope=scope,
            create_scope=create_scope,
            gmail_connection_id=gmail_connection_id,
            gmail_mailbox_binding_id=gmail_mailbox_binding_id,
            gmail_message_id=gmail_message_id,
            accepted_generation=accepted_generation,
            reuse_qualification_binding=reuse_qualification_binding,
        )
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


async def test_tombstone_selector_immutability_is_replaced_by_governed_guard(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    case, _ = await _issued_authorization(
        postgresql_adapter, postgresql_contract_database, 753075
    )
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
            trigger_rows = (
                await connection.execute(
                    text(
                        "SELECT trigger.tgname, pg_get_triggerdef(trigger.oid) AS definition "
                        "FROM pg_trigger AS trigger "
                        "JOIN pg_class AS relation ON relation.oid = trigger.tgrelid "
                        "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
                        "WHERE namespace.nspname = 'spine' "
                        "AND relation.relname = 'trusted_canonical_tombstone_selector_versions' "
                        "AND NOT trigger.tgisinternal ORDER BY trigger.tgname"
                    )
                )
            ).mappings().all()
        trigger_names = {row.tgname for row in trigger_rows}
        assert "trg_canonical_tombstone_selectors_immutable" not in trigger_names
        assert "trg_trusted_canonical_tombstone_selector_versions_immutable" in trigger_names
        replacement = next(
            row for row in trigger_rows
            if row.tgname == "trg_trusted_canonical_tombstone_selector_versions_immutable"
        )
        assert "BEFORE DELETE OR UPDATE" in replacement.definition
        async with migration_engine.connect() as connection:
            governed_trigger_tables = (
                await connection.execute(
                    text(
                        "SELECT relation.relname FROM pg_trigger AS trigger "
                        "JOIN pg_class AS relation ON relation.oid = trigger.tgrelid "
                        "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
                        "WHERE namespace.nspname = 'spine' "
                        "AND trigger.tgfoid = 'spine.allow_governed_source_delete()'::regprocedure "
                        "ORDER BY relation.relname"
                    )
                )
            ).scalars().all()
        assert set(governed_trigger_tables) == {
            "gmail_message_label_observations",
            "gmail_message_relationship_evidence",
            "access_policy_grants",
            "access_policy_versions",
            "admission_decisions",
            "canonical_acceptances",
            "lifecycle_decisions",
            "lifecycle_observations",
            "policy_activations",
            "policy_approvals",
            "policy_proposals",
            "qualified_deletion_evidence",
            "retention_applicabilities",
            "source_objects",
            "source_revision_provenance",
            "source_revision_receipt_associations",
            "source_revisions",
            "tombstone_authorization_decisions",
            "tombstone_authorizations",
            "trusted_canonical_tombstone_selector_versions",
        }

        async with postgresql_adapter.uow_factory(
            postgresql_adapter.environment_context(
                case.source.workspace_id, case.source.environment_id
            )
        ) as uow:
            selector_id = await uow.execute_statement(
                text(
                    "SELECT selector_id FROM spine.trusted_canonical_tombstone_selector_versions "
                    "WHERE source_object_id = :source_id"
                ),
                {"source_id": case.source.source_object_id},
            )
            selector = selector_id.scalar_one()
            await uow.rollback()

        for sql in (
            "UPDATE spine.trusted_canonical_tombstone_selector_versions "
            "SET record = record WHERE selector_id = :selector_id",
            "DELETE FROM spine.trusted_canonical_tombstone_selector_versions "
            "WHERE selector_id = :selector_id",
        ):
            with pytest.raises(DBAPIError) as migration_error:
                async with migration_engine.begin() as connection:
                    await connection.execute(text(sql), {"selector_id": selector})
            assert migration_error.value.orig.sqlstate == "23514"
            assert migration_error.value.orig.diag.constraint_name == "ck_governed_source_erasure_only"

            with pytest.raises(DBAPIError) as runtime_error:
                async with runtime_engine.begin() as connection:
                    await connection.execute(text(sql), {"selector_id": selector})
            assert runtime_error.value.orig.sqlstate == "42501"

        async with postgresql_adapter.uow_factory(
            postgresql_adapter.environment_context(
                case.source.workspace_id, case.source.environment_id
            )
        ) as uow:
            selector_count = await uow.execute_statement(
                text(
                    "SELECT count(*) FROM spine.trusted_canonical_tombstone_selector_versions "
                    "WHERE selector_id = :selector_id"
                ),
                {"selector_id": selector},
            )
            assert selector_count.scalar_one() == 1
            await uow.rollback()
    finally:
        await migration_engine.dispose()
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
        postgresql_adapter,
        postgresql_contract_database,
        753600,
        gmail_source=True,
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

    observation_id = synthetic_uuid(753610)
    evidence_id = synthetic_uuid(753611)
    lineage_engine = create_async_engine(
        postgresql_contract_database.migration_url.get_secret_value(),
        hide_parameters=True,
    )
    try:
        async with lineage_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO spine.gmail_message_label_observations "
                    "(observation_id, workspace_id, environment_id, source_object_id, "
                    "source_revision_id, history_id, event_identity, label_id, state, "
                    "record, received_at) VALUES (:observation_id, :workspace_id, "
                    ":environment_id, :source_id, :revision_id, 11, :event_identity, "
                    ":label_id, 'present', CAST(:record AS jsonb), :received_at)"
                ),
                {
                    "observation_id": observation_id,
                    "workspace_id": binding.workspace_id,
                    "environment_id": binding.environment_id,
                    "source_id": case.source.source_object_id,
                    "revision_id": case.revision.revision_id,
                    "event_identity": "synthetic-provider-event-753610",
                    "label_id": "synthetic-label-753610",
                    "record": '{"message_id":"synthetic-message-753610"}',
                    "received_at": NOW,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO spine.gmail_message_label_heads "
                    "(workspace_id, environment_id, source_object_id, label_id, "
                    "observation_id, history_id, event_identity, state) VALUES "
                    "(:workspace_id, :environment_id, :source_id, :label_id, "
                    ":observation_id, 11, :event_identity, 'present')"
                ),
                {
                    "workspace_id": binding.workspace_id,
                    "environment_id": binding.environment_id,
                    "source_id": case.source.source_object_id,
                    "label_id": "synthetic-label-753610",
                    "observation_id": observation_id,
                    "event_identity": "synthetic-provider-event-753610",
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO spine.gmail_message_relationship_evidence "
                    "(evidence_id, workspace_id, environment_id, source_object_id, "
                    "source_revision_id, kind, authority, record, received_at) VALUES "
                    "(:evidence_id, :workspace_id, :environment_id, :source_id, "
                    ":revision_id, 'provider_thread', 'provider_reported_thread_id', "
                    "CAST(:record AS jsonb), :received_at)"
                ),
                {
                    "evidence_id": evidence_id,
                    "workspace_id": binding.workspace_id,
                    "environment_id": binding.environment_id,
                    "source_id": case.source.source_object_id,
                    "revision_id": case.revision.revision_id,
                    "record": '{"thread_id":"synthetic-thread-753610"}',
                    "received_at": NOW,
                },
            )
    finally:
        await lineage_engine.dispose()

    engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        hide_parameters=True,
    )
    try:
        async def commit_erasure_then_lose_response() -> None:
            async with engine.begin() as connection:
                await connection.execute(
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
            raise TimeoutError("synthetic lost executor response after PostgreSQL commit")

        with pytest.raises(TimeoutError, match="lost executor response"):
            await commit_erasure_then_lose_response()

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
                    "(SELECT count(*) FROM spine.trusted_canonical_tombstone_selector_versions WHERE source_object_id = :source_id) AS selectors, "
                    "(SELECT count(*) FROM spine.gmail_message_label_heads WHERE source_object_id = :source_id) AS label_heads, "
                    "(SELECT count(*) FROM spine.gmail_message_label_observations WHERE source_object_id = :source_id) AS label_observations, "
                    "(SELECT count(*) FROM spine.gmail_message_relationship_evidence WHERE source_object_id = :source_id) AS relationship_evidence"
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
                "label_heads": 0,
                "label_observations": 0,
                "relationship_evidence": 0,
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
            assert replay.scalar_one() == "CANONICAL_ERASED"
    finally:
        await engine.dispose()


async def test_erasure_executor_rolls_back_all_rows_when_final_source_delete_fails(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    case, authorization = await _issued_authorization(
        postgresql_adapter,
        postgresql_contract_database,
        753700,
        gmail_source=True,
    )
    operation_id = synthetic_uuid(753701)
    binding = binding_from_tombstone_authorization(
        authorization, operation_id=operation_id
    )
    operation = _operation(binding)
    assert await _repository(postgresql_adapter).admit_if_current(operation, binding)

    migration_engine = create_async_engine(
        postgresql_contract_database.migration_url.get_secret_value(),
        hide_parameters=True,
    )
    runtime_engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        hide_parameters=True,
    )
    try:
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE FUNCTION spine.test_fail_source_erasure() "
                    "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                    "RAISE EXCEPTION 'synthetic erasure failure' "
                    "USING ERRCODE = 'P0001'; END $$"
                )
            )
            await connection.execute(
                text(
                    "CREATE TRIGGER zz_test_fail_source_erasure "
                    "BEFORE DELETE ON spine.source_objects FOR EACH ROW "
                    "EXECUTE FUNCTION spine.test_fail_source_erasure()"
                )
            )

        with pytest.raises(DBAPIError) as error:
            async with runtime_engine.begin() as connection:
                await connection.execute(
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
        assert error.value.orig.sqlstate == "P0001"

        async with postgresql_adapter.uow_factory(
            postgresql_adapter.environment_context(
                binding.workspace_id, binding.environment_id
            )
        ) as uow:
            persisted = await uow.execute_statement(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM spine.source_objects WHERE source_object_id = :source_id) AS objects, "
                    "(SELECT count(*) FROM spine.source_revisions WHERE source_object_id = :source_id) AS revisions, "
                    "(SELECT count(*) FROM spine.canonical_acceptances WHERE source_object_id = :source_id) AS acceptances, "
                    "(SELECT count(*) FROM spine.current_source_heads WHERE source_object_id = :source_id) AS heads"
                ),
                {"source_id": case.source.source_object_id},
            )
            assert dict(persisted.one()._mapping) == {
                "objects": 1,
                "revisions": 1,
                "acceptances": 1,
                "heads": 1,
            }
            await uow.rollback()
    finally:
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "DROP TRIGGER IF EXISTS zz_test_fail_source_erasure "
                    "ON spine.source_objects"
                )
            )
            await connection.execute(
                text("DROP FUNCTION IF EXISTS spine.test_fail_source_erasure()")
            )
        await migration_engine.dispose()
        await runtime_engine.dispose()


@pytest.mark.parametrize(
    "invalid_call",
    ("workspace", "environment", "operation", "state_version", "missing_fence"),
)
async def test_erasure_executor_denies_wrong_scope_or_stale_operation(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
    invalid_call: str,
) -> None:
    seed = 758000 + ("workspace", "environment", "operation", "state_version", "missing_fence").index(invalid_call) * 100
    case, authorization = await _issued_authorization(
        postgresql_adapter, postgresql_contract_database, seed, gmail_source=True
    )
    operation_id = synthetic_uuid(seed + 1)
    binding = binding_from_tombstone_authorization(
        authorization, operation_id=operation_id
    )
    operation = _operation(binding)
    if invalid_call == "missing_fence":
        operation = operation.model_copy(
            update={"status": DispositionStatus.BLOCKED, "fence_active": False}
        )
    assert await _repository(postgresql_adapter).admit_if_current(operation, binding)

    arguments = {
        "workspace_id": binding.workspace_id,
        "environment_id": binding.environment_id,
        "operation_id": operation_id,
        "state_version": operation.state_version,
    }
    if invalid_call == "workspace":
        arguments["workspace_id"] = synthetic_uuid(seed + 2)
    elif invalid_call == "environment":
        arguments["environment_id"] = synthetic_uuid(seed + 3)
    elif invalid_call == "operation":
        arguments["operation_id"] = synthetic_uuid(seed + 4)
    elif invalid_call == "state_version":
        arguments["state_version"] += 1

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
                arguments,
            )
            assert outcome.scalar_one() == "DENIED"
        async with postgresql_adapter.uow_factory(
            postgresql_adapter.environment_context(
                binding.workspace_id, binding.environment_id
            )
        ) as uow:
            source = await uow.execute_statement(
                text(
                    "SELECT count(*) FROM spine.source_objects "
                    "WHERE source_object_id = :source_id"
                ),
                {"source_id": case.source.source_object_id},
            )
            assert source.scalar_one() == 1
            await uow.rollback()
    finally:
        await engine.dispose()


async def test_erasure_executor_denies_revoked_persisted_authorization(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    case, authorization = await _issued_authorization(
        postgresql_adapter,
        postgresql_contract_database,
        758900,
        gmail_source=True,
    )
    operation_id = synthetic_uuid(758901)
    binding = binding_from_tombstone_authorization(
        authorization, operation_id=operation_id
    )
    operation = _operation(binding)
    repository = _repository(postgresql_adapter)
    assert await repository.admit_if_current(operation, binding)

    authorization_context = postgresql_adapter.ingestion_context(
        case.source.workspace_id,
        case.source.environment_id,
        "tombstone_authorization",
    )
    authority_repository = PostgreSQLTombstoneAuthorizationRepository(
        uow_factory=postgresql_adapter.uow_factory,
        effects=_Effects(),
        commit_verifier=_CommitVerifier(),
        clock=lambda: NOW,
    )
    current = await authority_repository.current_decision(
        authorization.authorization_id, authorization_context
    )
    assert current is not None
    revocation = TombstoneAuthorizationDecision(
        decision_id=synthetic_uuid(758902),
        workspace_id=authorization.workspace_id,
        environment_id=authorization.environment_id,
        source_object_id=authorization.source_object_id,
        authorization_id=authorization.authorization_id,
        disposition=TombstoneAuthorizationDisposition.REVOKED,
        expected_current_decision_id=current.decision_id,
        expected_current_generation=current.generation,
        generation=current.generation + 1,
        authority_reference=authorization.issuer_authority_id,
        authority_generation=authorization.issuer_authority_generation,
        audit_event_id=synthetic_uuid(758903),
        decided_at=NOW + timedelta(seconds=1),
    )
    await authority_repository.record_decision(revocation, authorization_context)

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
            assert outcome.scalar_one() == "DENIED"
        async with postgresql_adapter.uow_factory(
            postgresql_adapter.environment_context(
                binding.workspace_id, binding.environment_id
            )
        ) as uow:
            source_count = await uow.execute_statement(
                text(
                    "SELECT count(*) FROM spine.source_objects "
                    "WHERE source_object_id = :source_id"
                ),
                {"source_id": case.source.source_object_id},
            )
            persisted_operation = await repository.get(
                workspace_id=binding.workspace_id,
                environment_id=binding.environment_id,
                operation_id=operation_id,
            )
            assert source_count.scalar_one() == 1
            assert persisted_operation is not None
            assert persisted_operation.fence_active is True
            await uow.rollback()
    finally:
        await engine.dispose()


async def test_erasure_executor_fails_closed_on_unknown_source_foreign_key(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    case, authorization = await _issued_authorization(
        postgresql_adapter,
        postgresql_contract_database,
        759000,
        gmail_source=True,
    )
    operation_id = synthetic_uuid(759001)
    binding = binding_from_tombstone_authorization(
        authorization, operation_id=operation_id
    )
    operation = _operation(binding)
    assert await _repository(postgresql_adapter).admit_if_current(operation, binding)

    migration_engine = create_async_engine(
        postgresql_contract_database.migration_url.get_secret_value(),
        hide_parameters=True,
    )
    runtime_engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        hide_parameters=True,
    )
    try:
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE spine.test_unknown_source_dependency ("
                    "workspace_id uuid NOT NULL, environment_id uuid NOT NULL, "
                    "source_object_id uuid NOT NULL, "
                    "CONSTRAINT fk_test_unknown_source_dependency FOREIGN KEY "
                    "(workspace_id, environment_id, source_object_id) REFERENCES "
                    "spine.source_objects(workspace_id, environment_id, source_object_id))"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO spine.test_unknown_source_dependency "
                    "(workspace_id, environment_id, source_object_id) "
                    "VALUES (:workspace_id, :environment_id, :source_id)"
                ),
                {
                    "workspace_id": binding.workspace_id,
                    "environment_id": binding.environment_id,
                    "source_id": case.source.source_object_id,
                },
            )

        with pytest.raises(DBAPIError) as error:
            async with runtime_engine.begin() as connection:
                await connection.execute(
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
        assert error.value.orig.sqlstate == "23503"
        assert (
            error.value.orig.diag.constraint_name
            == "fk_test_unknown_source_dependency"
        )

        async with postgresql_adapter.uow_factory(
            postgresql_adapter.environment_context(
                binding.workspace_id, binding.environment_id
            )
        ) as uow:
            source_count = await uow.execute_statement(
                text(
                    "SELECT count(*) FROM spine.source_objects "
                    "WHERE source_object_id = :source_id"
                ),
                {"source_id": case.source.source_object_id},
            )
            assert source_count.scalar_one() == 1
            await uow.rollback()
    finally:
        async with migration_engine.begin() as connection:
            await connection.execute(
                text("DROP TABLE IF EXISTS spine.test_unknown_source_dependency")
            )
        await migration_engine.dispose()
        await runtime_engine.dispose()


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


async def test_postgresql_expiry_purges_only_expired_verified_operation_identity(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    _, first_authority = await _issued_authorization(
        postgresql_adapter, postgresql_contract_database, 754900
    )
    _, other_authority = await _issued_authorization(
        postgresql_adapter, postgresql_contract_database, 754910
    )
    repository = _repository(postgresql_adapter)

    async def persist_operation(authorization, number: int, *, verified: bool):
        operation_id = synthetic_uuid(number)
        binding = binding_from_tombstone_authorization(
            authorization, operation_id=operation_id
        )
        operation = _operation(binding)
        assert await repository.admit_if_current(operation, binding)
        store_status = (
            StoreDispositionStatus.VERIFIED
            if verified
            else StoreDispositionStatus.UNKNOWN
        )
        store_state = DispositionStoreState(
            store_id="canonical",
            status=store_status,
            attempts=1,
            result_code="CANONICAL_ERASED" if verified else "COMMIT_UNKNOWN",
        )
        persisted = DispositionOperation.model_validate(
            {
                **operation.model_dump(mode="python"),
                "state_version": operation.state_version + 1,
                "status": (
                    DispositionStatus.VERIFIED
                    if verified
                    else DispositionStatus.UNKNOWN
                ),
                "fence_active": True,
                "safe_code": "DISPOSITION_VERIFIED" if verified else "COMMIT_UNKNOWN",
                "completion_ack_expires_at": NOW - timedelta(seconds=1),
                "stores": (store_state,),
            }
        )
        await repository.save(
            persisted, expected_state_version=operation.state_version
        )
        return persisted

    expired = await persist_operation(first_authority, 754901, verified=True)
    other_tenant_expired = await persist_operation(
        other_authority, 754911, verified=True
    )
    fenced_unknown = await persist_operation(first_authority, 754902, verified=False)

    assert await repository.expire_completed(
        workspace_id=expired.workspace_id,
        environment_id=expired.environment_id,
        evaluated_at=NOW,
    ) == 1
    assert await repository.get(
        workspace_id=expired.workspace_id,
        environment_id=expired.environment_id,
        operation_id=expired.operation_id,
    ) is None
    assert await _outbox_count(postgresql_adapter, expired) == 0
    assert await repository.get(
        workspace_id=fenced_unknown.workspace_id,
        environment_id=fenced_unknown.environment_id,
        operation_id=fenced_unknown.operation_id,
    ) is not None
    assert await _outbox_count(postgresql_adapter, fenced_unknown) == 1
    assert await repository.get(
        workspace_id=other_tenant_expired.workspace_id,
        environment_id=other_tenant_expired.environment_id,
        operation_id=other_tenant_expired.operation_id,
    ) is not None
    assert await _outbox_count(postgresql_adapter, other_tenant_expired) == 1


async def test_governed_erasure_preserves_canonical_message_b_on_shared_email_sync_page(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    prepared_a = await _repository_case(
        postgresql_adapter,
        postgresql_contract_database,
        753800,
        gmail_source=True,
        gmail_connection_id=synthetic_uuid(753802),
        gmail_mailbox_binding_id=synthetic_uuid(753803),
        gmail_message_id="synthetic-provider-message-A",
    )
    case = prepared_a[0]
    mailbox_binding_id = synthetic_uuid(753803)
    message_b = await _prepare_lifecycle_case(
        postgresql_adapter,
        753820,
        gmail_source=True,
        scope=(case.source.workspace_id, case.source.environment_id),
        create_scope=False,
        gmail_connection_id=case.source.connection_id,
        gmail_mailbox_binding_id=mailbox_binding_id,
        gmail_message_id="synthetic-provider-message-B",
        accepted_generation=2,
    )
    async with postgresql_adapter.uow_factory(message_b.context) as uow:
        await uow.canonical.accept(message_b.command)
        await uow.commit()
    case, authorization = await _issued_authorization(
        postgresql_adapter,
        postgresql_contract_database,
        753800,
        gmail_source=True,
        gmail_connection_id=synthetic_uuid(753802),
        gmail_mailbox_binding_id=mailbox_binding_id,
        gmail_message_id="synthetic-provider-message-A",
        prepared_case=prepared_a,
    )
    operation_id = synthetic_uuid(753801)
    binding = binding_from_tombstone_authorization(
        authorization, operation_id=operation_id
    )
    operation = _operation(binding)
    assert await _repository(postgresql_adapter).admit_if_current(operation, binding)
    acceptance_id = case.command.canonical_acceptance_id
    attempt_id = synthetic_uuid(753803)
    observation_id = synthetic_uuid(753804)
    outbox_event_id = synthetic_uuid(753805)
    message_b_acceptance_id = message_b.command.canonical_acceptance_id
    page_id = "gmail.history.page.synthetic-shared-753800"
    source_digest = hashlib.sha256(
        case.source.external_object_id.encode("utf-8")
    ).hexdigest()
    event_digest = hashlib.sha256(b"synthetic-history-event-753800").hexdigest()
    observation = {
        "schema_version": "email-sync-state-observation:v2",
        "observation_id": str(observation_id),
        "workspace_id": str(binding.workspace_id),
        "environment_id": str(binding.environment_id),
        "connection_id": str(case.source.connection_id),
        "mailbox_binding_id": str(mailbox_binding_id),
        "state_kind": "label_added",
        "provider_event_digest": event_digest,
        "subject_reference_digest": source_digest,
        "provider_generation": "753800",
        "outbox_event_id": str(outbox_event_id),
    }
    message_b_observation_id = synthetic_uuid(753821)
    message_b_outbox_event_id = synthetic_uuid(753822)
    message_b_event_digest = hashlib.sha256(b"synthetic-history-event-753821").hexdigest()
    message_b_digest = hashlib.sha256(
        message_b.source.external_object_id.encode("utf-8")
    ).hexdigest()
    message_b_observation = {
        **observation,
        "observation_id": str(message_b_observation_id),
        "provider_event_digest": message_b_event_digest,
        "subject_reference_digest": message_b_digest,
        "outbox_event_id": str(message_b_outbox_event_id),
    }
    effects = [
        {
            "workspace_id": str(binding.workspace_id),
            "environment_id": str(binding.environment_id),
            "effect_key": "gmail.message.disposed.753800",
            "effect_reference": str(acceptance_id),
            "kind": "content",
            "state_observation": None,
        },
        {
            "workspace_id": str(binding.workspace_id),
            "environment_id": str(binding.environment_id),
            "effect_key": "gmail.message.unrelated.753800",
            "effect_reference": str(message_b_acceptance_id),
            "kind": "content",
            "state_observation": None,
        },
        {
            "workspace_id": str(binding.workspace_id),
            "environment_id": str(binding.environment_id),
            "effect_key": "gmail.label.disposed.753800",
            "effect_reference": str(observation_id),
            "kind": "state",
            "state_observation": observation,
        },
        {
            "workspace_id": str(binding.workspace_id),
            "environment_id": str(binding.environment_id),
            "effect_key": "gmail.label.active.753821",
            "effect_reference": str(message_b_observation_id),
            "kind": "state",
            "state_observation": message_b_observation,
        },
    ]
    page_record = {
        "workspace_id": str(binding.workspace_id),
        "environment_id": str(binding.environment_id),
        "page_kind": "history_drain",
        "page_id": page_id,
        "page_digest": "a" * 64,
        "page_schema_version": "email-sync-page:v1",
        "requested_checkpoint": "gmail-history:v1:100",
        "covered_checkpoint": "gmail-history:v1:110",
        "next_checkpoint": "gmail-history:v1:110",
        "terminal": True,
        "publish_checkpoint": True,
        "coverage_terminal": True,
        "evidence_digest": None,
        "evidence_cursor": None,
        "effects": effects,
        "lifecycle_receipts": [],
    }
    attempt_key = "attempt.shared.gmail.sync.753800"
    attempt_record = {
        "attempt_id": str(attempt_id),
        "attempt_key": attempt_key,
        "admission": {
            "workspace_id": str(binding.workspace_id),
            "environment_id": str(binding.environment_id),
            "connection_id": str(case.source.connection_id),
            "connection_version_id": str(synthetic_uuid(753807)),
            "resource_scope_reference": str(synthetic_uuid(753808)),
            "mailbox_subject_reference": str(synthetic_uuid(753809)),
            "mailbox_binding_id": str(mailbox_binding_id),
            "authorization_generation": 1,
            "configuration_generation": 1,
            "scope_generation": 1,
        },
        "status": "running",
        "committed_checkpoint": "gmail-history:v1:110",
        "failure_code": None,
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "revision": 1,
    }
    engine = create_async_engine(
        postgresql_contract_database.migration_url.get_secret_value(),
        hide_parameters=True,
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO spine.email_sync_attempts "
                    "(workspace_id, environment_id, attempt_id, attempt_key, revision, "
                    "authorization_generation, configuration_generation, scope_generation, "
                    "committed_checkpoint, record, created_at, updated_at) VALUES "
                    "(:workspace_id, :environment_id, :attempt_id, :attempt_key, 1, 1, 1, 1, "
                    ":checkpoint, CAST(:record AS jsonb), :now, :now)"
                ),
                {
                    "workspace_id": binding.workspace_id,
                    "environment_id": binding.environment_id,
                    "attempt_id": attempt_id,
                    "attempt_key": attempt_key,
                    "checkpoint": attempt_record["committed_checkpoint"],
                    "record": json.dumps(attempt_record),
                    "now": NOW,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO spine.email_sync_pages "
                    "(workspace_id, environment_id, attempt_id, page_id, page_digest, committed, record) "
                    "VALUES (:workspace_id, :environment_id, :attempt_id, :page_id, :digest, true, "
                    "CAST(:record AS jsonb))"
                ),
                {
                    "workspace_id": binding.workspace_id,
                    "environment_id": binding.environment_id,
                    "attempt_id": attempt_id,
                    "page_id": page_id,
                    "digest": "a" * 64,
                    "record": json.dumps(page_record),
                },
            )
            for key, receipt in (
                ("gmail.message.disposed.753800", acceptance_id),
                ("gmail.message.unrelated.753800", message_b_acceptance_id),
                ("gmail.label.disposed.753800", observation_id),
                ("gmail.label.active.753821", message_b_observation_id),
            ):
                await connection.execute(
                    text(
                        "INSERT INTO spine.email_sync_effect_receipts "
                        "(workspace_id, environment_id, attempt_id, page_id, effect_key, "
                        "receipt_reference, durable) VALUES (:workspace_id, :environment_id, "
                        ":attempt_id, :page_id, :effect_key, :receipt, true)"
                    ),
                    {
                        "workspace_id": binding.workspace_id,
                        "environment_id": binding.environment_id,
                        "attempt_id": attempt_id,
                        "page_id": page_id,
                        "effect_key": key,
                        "receipt": str(receipt),
                    },
                )
            for index, observation in enumerate((observation, message_b_observation)):
                await connection.execute(
                    text(
                        "INSERT INTO spine.outbox_intents "
                        "(event_id, workspace_id, environment_id, event_type, event_schema_version, "
                        "aggregate_type, aggregate_id, aggregate_schema_version, producer_deduplication_id, "
                        "payload, trace_id, created_at) VALUES (:event_id, :workspace_id, :environment_id, "
                        "'email.sync.state_observed', 1, 'mailbox_binding', :mailbox, 1, :dedup, "
                        "CAST(:payload AS jsonb), :trace, :now)"
                    ),
                    {
                        "event_id": observation["outbox_event_id"],
                        "workspace_id": binding.workspace_id,
                        "environment_id": binding.environment_id,
                        "mailbox": mailbox_binding_id,
                        "dedup": f"email-sync-state:{observation['observation_id']}",
                        "payload": json.dumps({
                            "observation_id": observation["observation_id"],
                            "connection_id": observation["connection_id"],
                            "mailbox_binding_id": observation["mailbox_binding_id"],
                            "state_kind": observation["state_kind"],
                            "provider_event_digest": observation["provider_event_digest"],
                        }),
                        "trace": synthetic_uuid(753810 + index),
                        "now": NOW,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO spine.email_sync_state_observations "
                        "(workspace_id, environment_id, observation_id, connection_id, mailbox_binding_id, "
                        "state_kind, provider_event_digest, outbox_event_id, record) VALUES "
                        "(:workspace_id, :environment_id, :observation_id, :connection_id, :mailbox, "
                        ":state_kind, :event_digest, :outbox_event_id, CAST(:record AS jsonb))"
                    ),
                    {
                        "workspace_id": binding.workspace_id,
                        "environment_id": binding.environment_id,
                        "observation_id": observation["observation_id"],
                        "connection_id": observation["connection_id"],
                        "mailbox": observation["mailbox_binding_id"],
                        "state_kind": observation["state_kind"],
                        "event_digest": observation["provider_event_digest"],
                        "outbox_event_id": observation["outbox_event_id"],
                        "record": json.dumps(observation),
                    },
                )
        with pytest.raises(DBAPIError) as immutable_outbox_error:
            async with engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM spine.outbox_intents WHERE event_id = :event_id"),
                    {"event_id": outbox_event_id},
                )
        assert immutable_outbox_error.value.orig.sqlstate == "23514"
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE spine.email_sync_pages SET record = "
                    "jsonb_set(record, '{page_schema_version}', "
                    "to_jsonb(CAST(:schema_version AS text))) WHERE attempt_id = :attempt_id"
                ),
                {"attempt_id": attempt_id, "schema_version": "email-sync-page:v99"},
            )
        unknown_schema_runtime = create_async_engine(
            postgresql_contract_database.runtime_url.get_secret_value(),
            hide_parameters=True,
        )
        try:
            with pytest.raises(DBAPIError) as unknown_page_schema_error:
                async with unknown_schema_runtime.begin() as connection:
                    await connection.execute(
                        text(
                            "SELECT set_config('spine.workspace_id', :workspace_id, true), "
                            "set_config('spine.environment_id', :environment_id, true)"
                        ),
                        {
                            "workspace_id": str(binding.workspace_id),
                            "environment_id": str(binding.environment_id),
                        },
                    )
                    await connection.execute(
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
            assert unknown_page_schema_error.value.orig.sqlstate == "23514"
        finally:
            await unknown_schema_runtime.dispose()
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE spine.email_sync_pages SET record = "
                    "jsonb_set(record, '{page_schema_version}', "
                    "to_jsonb(CAST(:schema_version AS text))) WHERE attempt_id = :attempt_id"
                ),
                {"attempt_id": attempt_id, "schema_version": "email-sync-page:v1"},
            )
        runtime_engine = create_async_engine(
            postgresql_contract_database.runtime_url.get_secret_value(),
            hide_parameters=True,
        )
        try:
            async with runtime_engine.begin() as connection:
                await connection.execute(
                    text(
                        "SELECT set_config('spine.workspace_id', :workspace_id, true), "
                        "set_config('spine.environment_id', :environment_id, true)"
                    ),
                    {
                        "workspace_id": str(binding.workspace_id),
                        "environment_id": str(binding.environment_id),
                    },
                )
                await connection.execute(
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
        finally:
            await runtime_engine.dispose()
        async with engine.connect() as connection:
            result = (
                await connection.execute(
                    text(
                        "SELECT p.page_id, p.page_digest, p.record, p.committed, a.committed_checkpoint, a.revision, "
                        "(SELECT count(*) FROM spine.email_sync_effect_receipts r "
                        " WHERE r.workspace_id = p.workspace_id AND r.environment_id = p.environment_id "
                        " AND r.attempt_id = p.attempt_id AND r.page_id = p.page_id) AS receipts, "
                        "(SELECT count(*) FROM spine.email_sync_state_observations o "
                        " WHERE o.observation_id = :observation_id) AS observations, "
                        "(SELECT count(*) FROM spine.outbox_intents i WHERE i.event_id = :outbox_event_id) AS outbox, "
                        "(SELECT count(*) FROM spine.source_objects s "
                        " WHERE s.source_object_id = :source_object_id) AS source_a, "
                        "(SELECT count(*) FROM spine.canonical_acceptances c "
                        " WHERE c.canonical_acceptance_id = :acceptance_id) AS acceptance_a, "
                        "(SELECT count(*) FROM spine.source_objects s "
                        " WHERE s.source_object_id = :source_b_id) AS source_b, "
                        "(SELECT count(*) FROM spine.canonical_acceptances c "
                        " WHERE c.canonical_acceptance_id = :acceptance_b_id) AS acceptance_b, "
                        "(SELECT count(*) FROM spine.email_sync_state_observations o "
                        " WHERE o.observation_id = :observation_b_id) AS observation_b, "
                        "(SELECT count(*) FROM spine.outbox_intents i WHERE i.event_id = :outbox_b_id) AS outbox_b "
                        "FROM spine.email_sync_pages p JOIN spine.email_sync_attempts a "
                        "USING (workspace_id, environment_id, attempt_id) WHERE p.attempt_id = :attempt_id"
                    ),
                    {
                        "attempt_id": attempt_id,
                        "observation_id": observation_id,
                        "outbox_event_id": outbox_event_id,
                        "source_object_id": case.source.source_object_id,
                        "acceptance_id": acceptance_id,
                        "source_b_id": message_b.source.source_object_id,
                        "acceptance_b_id": message_b_acceptance_id,
                        "observation_b_id": message_b_observation_id,
                        "outbox_b_id": message_b_outbox_event_id,
                    },
                )
            ).mappings().one()
            surviving = result["record"]["effects"]
            assert result["page_id"].startswith("erased.")
            assert result["committed_checkpoint"] == "gmail-history:v1:110"
            assert result["revision"] == 2
            assert [effect["effect_key"] for effect in surviving] == [
                "gmail.message.unrelated.753800",
                "gmail.label.active.753821",
            ]
            assert result["receipts"] == 2
            assert result["observations"] == 0
            assert result["outbox"] == 0
            assert result["source_a"] == 0 and result["acceptance_a"] == 0
            assert result["source_b"] == 1 and result["acceptance_b"] == 1
            assert result["observation_b"] == 1 and result["outbox_b"] == 1
            receipt_rows = (
                await connection.execute(
                    text(
                        "SELECT effect_key, receipt_reference, durable "
                        "FROM spine.email_sync_effect_receipts "
                        "WHERE attempt_id = :attempt_id AND page_id = :page_id "
                        "ORDER BY effect_key"
                    ),
                    {"attempt_id": attempt_id, "page_id": result["page_id"]},
                )
            ).mappings().all()
            persisted_state = dict(result["record"])
            persisted_state["page_digest"] = result["page_digest"]
            persisted_state["committed"] = result["committed"]
            persisted_state["effect_receipts"] = [
                {
                    "effect_key": row["effect_key"],
                    "receipt_reference": row["receipt_reference"],
                    "durable": row["durable"],
                }
                for row in receipt_rows
            ]
            loaded_state = EmailSyncPageState.model_validate(persisted_state)
            loaded_page = EmailSyncCoordinator._page_from_state(loaded_state)
            assert loaded_state.page_digest_contract == EMAIL_SYNC_PAGE_DIGEST_V2
            assert result["page_digest"] == EmailSyncCoordinator._page_digest(
                loaded_page, contract=EMAIL_SYNC_PAGE_DIGEST_V2
            )
            attempt_record["revision"] = result["revision"]
            email_repository = PostgreSQLEmailSyncRepository(
                postgresql_adapter.uow_factory,
                lambda operation, _identity: postgresql_adapter.ingestion_context(
                    binding.workspace_id, binding.environment_id, operation.value
                ),
            )
            persisted_attempt = await email_repository.get(
                attempt_id, binding.workspace_id, binding.environment_id
            )
            assert persisted_attempt is not None
            assert persisted_attempt.pages[0].page_digest == result["page_digest"]
            assert persisted_attempt.committed_checkpoint == "gmail-history:v1:110"
            assert {
                receipt.effect_key: receipt.receipt_reference
                for receipt in persisted_attempt.pages[0].effect_receipts
            } == {
                "gmail.message.unrelated.753800": str(message_b_acceptance_id),
                "gmail.label.active.753821": str(message_b_observation_id),
            }

            class _CurrentAdmission:
                async def check(self, _admission):
                    return EmailSyncAdmissionState.ALLOWED

            class _EffectsMustNotReplay:
                def __init__(self) -> None:
                    self.calls = 0

                async def apply(self, effect):
                    self.calls += 1
                    return EmailSyncEffectReceipt(
                        effect_key=effect.effect_key,
                        receipt_reference="unexpected-replay",
                        durable=True,
                    )

            replay_effects = _EffectsMustNotReplay()
            coordinator = EmailSyncCoordinator(
                repository=email_repository,
                admission=_CurrentAdmission(),
                effects=replay_effects,
                clock=lambda: NOW,
            )
            original_page = EmailSyncPage.model_validate(
                {
                    key: value
                    for key, value in page_record.items()
                    if key not in {
                        "page_digest",
                        "covered_checkpoint",
                        "page_schema_version",
                    }
                }
            )
            replay_result = await coordinator.apply_page(
                attempt_id,
                original_page,
                workspace_id=binding.workspace_id,
                environment_id=binding.environment_id,
            )
            assert replay_result.status is EmailSyncStatus.RECONCILIATION_REQUIRED
            assert replay_result.committed_checkpoint == "gmail-history:v1:110"
            assert replay_effects.calls == 0
            assert await connection.scalar(
                text(
                    "SELECT count(*) FROM spine.source_objects "
                    "WHERE source_object_id = :source_object_id"
                ),
                {"source_object_id": case.source.source_object_id},
            ) == 0
            serialized = json.dumps(result["record"])
            assert source_digest not in serialized and event_digest not in serialized
            assert str(acceptance_id) not in serialized
            assert source_digest not in serialized and event_digest not in serialized
    finally:
        await engine.dispose()
