from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from spine.application.tombstone_authorization import (
    IssueTombstoneAuthorizationCommand,
    TombstoneAuthorizationOutcome,
    TombstoneAuthorizationService,
)
from spine.domain.sources.deletion_authorization import (
    TombstoneAuthorizationDecision,
    TombstoneAuthorizationDisposition,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    audit_events,
    tombstone_authorizations,
)
from spine.infrastructure.persistence.postgresql_tombstone_authorization import (
    PostgreSQLTombstoneAuthorizationRepository,
)
from tests.application.test_tombstone_authorization import (
    NOW,
    _approval,
    _authorization,
    _evidence,
    _issuer,
)
from tests.integration.postgresql.test_postgresql_lifecycle_authority import (
    _prepare_lifecycle_case,
)
from tests.integration.postgresql.test_postgresql_unit_of_work import (
    PersistenceAdapter,
    PostgreSQLContractDatabase,
    postgresql_adapter,
    postgresql_contract_database,
    synthetic_uuid,
)

pytestmark = [pytest.mark.postgresql, pytest.mark.asyncio(loop_scope="session")]


class _Effects:
    async def persist_policy_approval(self, approval, context, transaction):
        await self._audit(
            approval.audit_event_id,
            approval.approval_id,
            approval.approved_at,
            context,
            transaction,
        )
        return approval.audit_event_id

    async def persist_issuer(self, issuer, context, transaction):
        await self._audit(
            issuer.audit_event_id,
            issuer.issuer_authority_id,
            issuer.valid_from,
            context,
            transaction,
        )
        return issuer.audit_event_id

    async def persist_authorization(self, authorization, context, transaction):
        await self._audit(
            authorization.audit_event_id,
            authorization.authorization_id,
            authorization.issued_at,
            context,
            transaction,
        )
        return authorization.audit_event_id

    async def persist_decision(self, decision, context, transaction):
        await self._audit(
            decision.audit_event_id,
            decision.decision_id,
            decision.decided_at,
            context,
            transaction,
        )
        return decision.audit_event_id

    async def _audit(self, audit_id, target_id, occurred_at, context, transaction):
        await transaction._execute(
            insert(audit_events).values(
                audit_event_id=audit_id,
                workspace_id=context.scope.workspace_id,
                environment_id=context.scope.environment_id,
                event_type="source.tombstone_authority",
                schema_version=1,
                origin=context.origin.value,
                acting_subject_id=context.acting_subject_id,
                service_principal_id=context.service_principal_id,
                trace_id=context.trace_id,
                correlation_id=None,
                causation_id=None,
                occurred_at=occurred_at,
                appended_at=occurred_at,
                target_type="source_object",
                target_id=target_id,
                target_schema_version=1,
                outcome="committed",
                reason="tombstone_authority",
                producer_deduplication_id=f"tombstone-authority-{audit_id}",
                payload={"target_id": str(target_id)},
            )
        )


class _CommitVerifier:
    async def verify(self, authorization, context, transaction):
        assert transaction is not None
        return True


async def _repository_case(
    adapter: PersistenceAdapter,
    database: PostgreSQLContractDatabase,
    number: int,
):
    case = await _prepare_lifecycle_case(adapter, number)
    async with adapter.uow_factory(case.context) as uow:
        await uow.canonical.accept(case.command)
        await uow.commit()
    context = adapter.ingestion_context(
        case.source.workspace_id, case.source.environment_id, "tombstone_authorization"
    )
    evidence_context = adapter.source_context(
        case.source.workspace_id, case.source.environment_id
    )
    authority_reference = synthetic_uuid(number + 20)
    human_id = synthetic_uuid(number + 31)
    migration_engine = create_async_engine(
        database.migration_url.get_secret_value(), hide_parameters=True
    )
    try:
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO spine.qualification_authority_bindings ("
                    "binding_id, version, qualification_id, workspace_id, environment_id, "
                    "principal_kind, principal_id, purpose, operation, expires_at, status, "
                    "change_id, recorded_by) VALUES ("
                    ":binding_id, 1, :qualification_id, :workspace_id, :environment_id, "
                    "'service', :principal_id, :purpose, :operation, :expires_at, 'active', "
                    ":change_id, 'issue-735-test')"
                ),
                {
                    "binding_id": authority_reference,
                    "qualification_id": synthetic_uuid(number + 21),
                    "workspace_id": case.source.workspace_id,
                    "environment_id": case.source.environment_id,
                    "principal_id": evidence_context.service_principal_id,
                    "purpose": evidence_context.purpose.value,
                    "operation": evidence_context.operation.value,
                    "expires_at": NOW + timedelta(days=1),
                    "change_id": synthetic_uuid(number + 22),
                },
            )
            for statement, parameters in (
                (
                    "INSERT INTO spine.canonical_human_identities "
                    "(identity_id, created_change_id, created_by) VALUES "
                    "(:human_id, :change_id, 'issue-735-test')",
                    {"human_id": human_id, "change_id": synthetic_uuid(number + 60)},
                ),
                (
                    "INSERT INTO spine.canonical_human_identity_states "
                    "(identity_id, version, status, change_id, recorded_by) VALUES "
                    "(:human_id, 1, 'active', :change_id, 'issue-735-test')",
                    {"human_id": human_id, "change_id": synthetic_uuid(number + 61)},
                ),
                (
                    "INSERT INTO spine.workspace_memberships "
                    "(membership_id, workspace_id, canonical_human_identity_id, version, "
                    "status, change_id, recorded_by) VALUES "
                    "(:membership_id, :workspace_id, :human_id, 1, 'active', :change_id, "
                    "'issue-735-test')",
                    {
                        "membership_id": synthetic_uuid(number + 62),
                        "workspace_id": case.source.workspace_id,
                        "human_id": human_id,
                        "change_id": synthetic_uuid(number + 63),
                    },
                ),
                (
                    "INSERT INTO spine.environment_memberships "
                    "(membership_id, workspace_id, environment_id, "
                    "canonical_human_identity_id, version, status, change_id, recorded_by) "
                    "VALUES (:membership_id, :workspace_id, :environment_id, :human_id, 1, "
                    "'active', :change_id, 'issue-735-test')",
                    {
                        "membership_id": synthetic_uuid(number + 64),
                        "workspace_id": case.source.workspace_id,
                        "environment_id": case.source.environment_id,
                        "human_id": human_id,
                        "change_id": synthetic_uuid(number + 65),
                    },
                ),
                (
                    "INSERT INTO spine.environment_role_bindings "
                    "(binding_id, workspace_id, environment_id, "
                    "canonical_human_identity_id, role, version, status, change_id, "
                    "recorded_by) VALUES (:binding_id, :workspace_id, :environment_id, "
                    ":human_id, 'administrator', 1, 'active', :change_id, 'issue-735-test')",
                    {
                        "binding_id": synthetic_uuid(number + 66),
                        "workspace_id": case.source.workspace_id,
                        "environment_id": case.source.environment_id,
                        "human_id": human_id,
                        "change_id": synthetic_uuid(number + 67),
                    },
                ),
            ):
                await connection.execute(text(statement), parameters)
            human_generation = (
                await connection.execute(
                    text(
                        "SELECT generation FROM spine.authorization_generations "
                        "WHERE workspace_id = :workspace_id AND environment_id = :environment_id"
                    ),
                    {
                        "workspace_id": case.source.workspace_id,
                        "environment_id": case.source.environment_id,
                    },
                )
            ).scalar_one()
    finally:
        await migration_engine.dispose()
    async with adapter.uow_factory(evidence_context) as uow:
        authority = (
            await uow._execute(
                text(
                    "SELECT binding_id, version "
                    "FROM spine.resolve_current_deletion_qualification_authority("
                    ":principal_id, :workspace_id, :environment_id, :purpose, "
                    ":operation)"
                ),
                {
                    "principal_id": str(evidence_context.service_principal_id),
                    "workspace_id": str(case.source.workspace_id),
                    "environment_id": str(case.source.environment_id),
                    "purpose": evidence_context.purpose.value,
                    "operation": evidence_context.operation.value,
                },
            )
        ).one()
        await uow.rollback()
    assert case.source.connection_id is not None
    evidence = _evidence().model_copy(
        update={
            "workspace_id": case.source.workspace_id,
            "environment_id": case.source.environment_id,
            "source_object_id": case.source.source_object_id,
            "connection_id": case.source.connection_id,
            "connection_version_id": synthetic_uuid(number + 20),
            "resource_scope_reference": synthetic_uuid(number + 21),
            "scope_approval_version_id": synthetic_uuid(number + 22),
            "qualifier_service_principal_id": evidence_context.service_principal_id,
            "qualification_authority_reference": authority.binding_id,
            "qualification_authority_generation": authority.version,
            "observation_id": synthetic_uuid(number + 23),
            "evidence_id": synthetic_uuid(number + 24),
            "evidence_references": (synthetic_uuid(number + 25),),
        }
    )
    approval = _approval().model_copy(
        update={
            "approval_id": synthetic_uuid(number + 30),
            "workspace_id": case.source.workspace_id,
            "environment_id": case.source.environment_id,
            "connection_id": case.source.connection_id,
            "resource_scope_reference": evidence.resource_scope_reference,
            "approver_human_id": human_id,
            "approver_authority_reference": human_id,
            "approver_authority_generation": human_generation,
            "audit_event_id": synthetic_uuid(number + 32),
            "authority_generation": 1,
        }
    )
    issuer = _issuer().model_copy(
        update={
            "issuer_authority_id": synthetic_uuid(number + 40),
            "workspace_id": case.source.workspace_id,
            "environment_id": case.source.environment_id,
            "service_principal_id": context.service_principal_id,
            "authorized_by_human_id": human_id,
            "authorized_by_authority_generation": human_generation,
            "audit_event_id": synthetic_uuid(number + 41),
            "authority_generation": 1,
        }
    )
    authorization = _authorization().model_copy(
        update={
            "authorization_id": synthetic_uuid(number + 50),
            "workspace_id": case.source.workspace_id,
            "environment_id": case.source.environment_id,
            "source_object_id": case.source.source_object_id,
            "connection_id": case.source.connection_id,
            "connection_version_id": evidence.connection_version_id,
            "resource_scope_reference": evidence.resource_scope_reference,
            "scope_approval_version_id": evidence.scope_approval_version_id,
            "qualified_evidence_id": evidence.evidence_id,
            "observation_id": evidence.observation_id,
            "deletion_policy_approval_id": approval.approval_id,
            "deletion_policy_version_id": approval.policy_version_id,
            "deletion_policy_authority_generation": approval.authority_generation,
            "approver_human_id": approval.approver_human_id,
            "approver_authority_generation": approval.approver_authority_generation,
            "issuer_authority_id": issuer.issuer_authority_id,
            "issuer_service_principal_id": issuer.service_principal_id,
            "issuer_authority_generation": issuer.authority_generation,
            "expected_current_revision_id": case.revision.revision_id,
            "expected_current_acceptance_id": case.command.canonical_acceptance_id,
            "expected_current_cas_version": 1,
            "tombstone_revision_id": synthetic_uuid(number + 51),
            "transition_id": synthetic_uuid(number + 52),
            "retention_eligibility_reference": synthetic_uuid(number + 53),
            "legal_hold_check_reference": synthetic_uuid(number + 54),
            "idempotency_key": f"tombstone-{number}",
            "audit_event_id": synthetic_uuid(number + 55),
        }
    )
    repository = PostgreSQLTombstoneAuthorizationRepository(
        uow_factory=adapter.uow_factory,
        effects=_Effects(),
        commit_verifier=_CommitVerifier(),
        clock=lambda: NOW,
    )
    return (
        case,
        context,
        evidence_context,
        repository,
        evidence,
        approval,
        issuer,
        authorization,
    )


async def _resolve_deletion_qualification(
    database: PostgreSQLContractDatabase,
    evidence_context,
    evidence,
):
    runtime_engine = create_async_engine(
        database.runtime_url.get_secret_value(), hide_parameters=True
    )
    try:
        async with runtime_engine.begin() as connection:
            await connection.execute(
                text(
                    "SELECT set_config('spine.workspace_id', :workspace_id, true), "
                    "set_config('spine.environment_id', :environment_id, true)"
                ),
                {
                    "workspace_id": str(evidence.workspace_id),
                    "environment_id": str(evidence.environment_id),
                },
            )
            result = await connection.execute(
                text(
                    "SELECT binding_id, version "
                    "FROM spine.resolve_current_deletion_qualification_authority("
                    ":principal_id, :workspace_id, :environment_id, :purpose, :operation)"
                ),
                {
                    "principal_id": str(evidence.qualifier_service_principal_id),
                    "workspace_id": str(evidence.workspace_id),
                    "environment_id": str(evidence.environment_id),
                    "purpose": evidence_context.purpose.value,
                    "operation": evidence_context.operation.value,
                },
            )
            return result.one_or_none()
    finally:
        await runtime_engine.dispose()


async def _admitted_case(
    adapter: PersistenceAdapter,
    database: PostgreSQLContractDatabase,
    number: int,
):
    values = await _repository_case(adapter, database, number)
    case, context, evidence_context, repository, evidence, approval, issuer, _ = values
    await repository.record_evidence(evidence, evidence_context)
    approval_context = adapter.governance_context(
        approval.workspace_id,
        approval.environment_id,
        approval.approver_human_id,
        "approve_tombstone_policy",
    )
    issuer_context = adapter.governance_context(
        issuer.workspace_id,
        issuer.environment_id,
        issuer.authorized_by_human_id,
        "activate_tombstone_issuer",
    )
    await repository.record_policy_approval(approval, approval_context)
    await repository.record_issuer(issuer, issuer_context)
    return (*values, approval_context, issuer_context)


async def test_postgresql_authorization_replays_one_logical_effect(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    _, context, _, repository, evidence, approval, issuer, authorization, _, _ = (
        await _admitted_case(postgresql_adapter, postgresql_contract_database, 735100)
    )
    assert await repository.resolve_evidence(authorization, context) == evidence
    assert (
        await repository.resolve_current_policy_approval(authorization, context)
        == approval
    )
    assert await repository.resolve_current_issuer(authorization, context) == issuer
    assert await repository.resolve_current_head(authorization, context) is not None
    assert await repository.retention_and_legal_eligible(authorization, context) is True
    service = TombstoneAuthorizationService(repository, repository, clock=lambda: NOW)

    first = await service.issue(
        IssueTombstoneAuthorizationCommand(authorization), context
    )
    replay = await service.issue(
        IssueTombstoneAuthorizationCommand(authorization), context
    )
    assert first.outcome is TombstoneAuthorizationOutcome.ISSUED
    assert replay.outcome is TombstoneAuthorizationOutcome.REPLAYED

    async with postgresql_adapter.uow_factory(context) as uow:
        count = (
            await uow._execute(
                select(tombstone_authorizations.c.authorization_id).where(
                    tombstone_authorizations.c.authorization_id
                    == authorization.authorization_id
                )
            )
        ).all()
        assert len(count) == 1
        await uow.rollback()


async def test_postgresql_revocation_makes_issued_authorization_non_current(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    _, context, _, repository, _, _, issuer, authorization, _, _ = (
        await _admitted_case(postgresql_adapter, postgresql_contract_database, 735200)
    )
    service = TombstoneAuthorizationService(repository, repository, clock=lambda: NOW)
    issued = await service.issue(
        IssueTombstoneAuthorizationCommand(authorization), context
    )
    assert issued.outcome is TombstoneAuthorizationOutcome.ISSUED
    current = await repository.current_decision(authorization.authorization_id, context)
    assert current is not None
    revocation = TombstoneAuthorizationDecision(
        decision_id=synthetic_uuid(735290),
        workspace_id=authorization.workspace_id,
        environment_id=authorization.environment_id,
        source_object_id=authorization.source_object_id,
        authorization_id=authorization.authorization_id,
        disposition=TombstoneAuthorizationDisposition.REVOKED,
        expected_current_decision_id=current.decision_id,
        expected_current_generation=current.generation,
        generation=current.generation + 1,
        authority_reference=issuer.issuer_authority_id,
        authority_generation=issuer.authority_generation,
        audit_event_id=synthetic_uuid(735291),
        decided_at=NOW + timedelta(seconds=1),
    )
    await repository.record_decision(revocation, context)
    assert await repository.is_current(authorization.authorization_id, context) is False


async def test_postgresql_current_policy_or_issuer_revocation_denies_issuance(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    (
        _,
        context,
        _,
        repository,
        _,
        approval,
        issuer,
        authorization,
        approval_context,
        issuer_context,
    ) = await _admitted_case(postgresql_adapter, postgresql_contract_database, 735300)
    revoked_approval = approval.model_copy(
        update={
            "approval_id": synthetic_uuid(735392),
            "authority_generation": 2,
            "predecessor_approval_id": approval.approval_id,
            "revoked": True,
            "approved_at": NOW + timedelta(seconds=2),
            "audit_event_id": synthetic_uuid(735393),
        }
    )
    revoked_issuer = issuer.model_copy(
        update={
            "issuer_authority_id": synthetic_uuid(735394),
            "authority_generation": 2,
            "predecessor_issuer_authority_id": issuer.issuer_authority_id,
            "revoked": True,
            "valid_from": NOW + timedelta(seconds=2),
            "audit_event_id": synthetic_uuid(735395),
        }
    )
    await repository.record_policy_approval(revoked_approval, approval_context)
    await repository.record_issuer(revoked_issuer, issuer_context)
    assert (
        await repository.resolve_current_policy_approval(authorization, context) is None
    )
    assert (
        await repository.resolve_current_issuer(authorization, context)
    ).revoked is True
    service = TombstoneAuthorizationService(repository, repository, clock=lambda: NOW)
    denied = await service.issue(
        IssueTombstoneAuthorizationCommand(authorization), context
    )
    assert denied.outcome is TombstoneAuthorizationOutcome.TOMBSTONE_NOT_AUTHORIZED


async def test_postgresql_authorization_is_not_visible_across_workspaces(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    case, context, _, repository, _, _, _, authorization, _, _ = await _admitted_case(
        postgresql_adapter, postgresql_contract_database, 735400
    )
    service = TombstoneAuthorizationService(repository, repository, clock=lambda: NOW)
    issued = await service.issue(
        IssueTombstoneAuthorizationCommand(authorization), context
    )
    assert issued.outcome is TombstoneAuthorizationOutcome.ISSUED
    other_context = postgresql_adapter.ingestion_context(
        synthetic_uuid(735999), case.source.environment_id, "tombstone_authorization"
    )
    assert (
        await repository.is_current(authorization.authorization_id, other_context)
        is False
    )


async def test_postgresql_authorization_row_is_immutable(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    _, context, _, repository, _, _, _, authorization, _, _ = await _admitted_case(
        postgresql_adapter, postgresql_contract_database, 735500
    )
    service = TombstoneAuthorizationService(repository, repository, clock=lambda: NOW)
    issued = await service.issue(
        IssueTombstoneAuthorizationCommand(authorization), context
    )
    assert issued.outcome is TombstoneAuthorizationOutcome.ISSUED
    migration_engine = create_async_engine(
        postgresql_contract_database.migration_url.get_secret_value(),
        hide_parameters=True,
    )
    try:
        with pytest.raises(DBAPIError) as raised:
            async with migration_engine.begin() as connection:
                await connection.execute(
                    update(tombstone_authorizations)
                    .where(
                        tombstone_authorizations.c.authorization_id
                        == authorization.authorization_id
                    )
                    .values(command_digest="0" * 64)
                )
        assert getattr(raised.value.orig, "sqlstate", None) == "23514"
        assert (
            getattr(getattr(raised.value.orig, "diag", None), "constraint_name", None)
            == "ck_tombstone_authorizations_immutable"
        )
    finally:
        await migration_engine.dispose()


async def test_postgresql_revoked_qualification_successor_hides_active_predecessor(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    _, _, evidence_context, repository, evidence, _, _, _ = await _repository_case(
        postgresql_adapter, postgresql_contract_database, 735700
    )
    migration_engine = create_async_engine(
        postgresql_contract_database.migration_url.get_secret_value(), hide_parameters=True
    )
    try:
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO spine.qualification_authority_bindings ("
                    "binding_id, version, qualification_id, workspace_id, environment_id, "
                    "principal_kind, principal_id, purpose, operation, expires_at, status, "
                    "change_id, recorded_by) VALUES ("
                    ":binding_id, 2, :qualification_id, :workspace_id, :environment_id, "
                    "'service', :principal_id, :purpose, :operation, :expires_at, 'revoked', "
                    ":change_id, 'issue-735-test')"
                ),
                {
                    "binding_id": evidence.qualification_authority_reference,
                    "qualification_id": synthetic_uuid(735721),
                    "workspace_id": evidence.workspace_id,
                    "environment_id": evidence.environment_id,
                    "principal_id": evidence.qualifier_service_principal_id,
                    "purpose": evidence_context.purpose.value,
                    "operation": evidence_context.operation.value,
                    "expires_at": NOW + timedelta(days=1),
                    "change_id": synthetic_uuid(739702),
                },
            )
    finally:
        await migration_engine.dispose()

    assert (
        await _resolve_deletion_qualification(
            postgresql_contract_database, evidence_context, evidence
        )
        is None
    )


async def test_postgresql_other_qualification_scope_does_not_hide_requested_binding(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    _, _, evidence_context, repository, evidence, _, _, _ = await _repository_case(
        postgresql_adapter, postgresql_contract_database, 735800
    )
    migration_engine = create_async_engine(
        postgresql_contract_database.migration_url.get_secret_value(), hide_parameters=True
    )
    try:
        async with migration_engine.begin() as connection:
            statement = text(
                "INSERT INTO spine.qualification_authority_bindings ("
                "binding_id, version, qualification_id, workspace_id, environment_id, "
                "principal_kind, principal_id, purpose, operation, expires_at, status, "
                "change_id, recorded_by) VALUES ("
                ":binding_id, :version, :qualification_id, :workspace_id, :environment_id, "
                "'service', :principal_id, 'other_purpose', 'other_operation', "
                ":expires_at, 'active', :change_id, 'issue-735-test')"
            )
            common = {
                "binding_id": synthetic_uuid(739801),
                "qualification_id": synthetic_uuid(739802),
                "workspace_id": evidence.workspace_id,
                "environment_id": evidence.environment_id,
                "principal_id": evidence.qualifier_service_principal_id,
                "expires_at": NOW + timedelta(days=1),
            }
            await connection.execute(
                statement,
                {**common, "version": 1, "change_id": synthetic_uuid(739803)},
            )
            await connection.execute(
                statement,
                {**common, "version": 2, "change_id": synthetic_uuid(739804)},
            )
    finally:
        await migration_engine.dispose()

    current = await _resolve_deletion_qualification(
        postgresql_contract_database, evidence_context, evidence
    )
    assert current is not None
    assert tuple(current) == (
        evidence.qualification_authority_reference,
        evidence.qualification_authority_generation,
    )
    fresh_context = postgresql_adapter.source_context(
        evidence.workspace_id, evidence.environment_id
    )
    assert await repository.record_evidence(evidence, fresh_context) == evidence


async def test_postgresql_authority_registration_rejects_self_appointed_roles(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    _, issuer_context, evidence_context, repository, evidence, _, issuer, _ = (
        await _repository_case(postgresql_adapter, postgresql_contract_database, 735600)
    )
    with pytest.raises(ValueError, match="evidence authority"):
        await repository.record_evidence(evidence, issuer_context)

    wrong_admin = postgresql_adapter.governance_context(
        issuer.workspace_id,
        issuer.environment_id,
        synthetic_uuid(735699),
        "activate_tombstone_issuer",
    )
    with pytest.raises(ValueError, match="issuer authority"):
        await repository.record_issuer(issuer, wrong_admin)

    await repository.record_evidence(evidence, evidence_context)


async def test_postgresql_policy_approval_rejects_fabricated_human_authority(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    _, _, _, repository, _, approval, _, _ = await _repository_case(
        postgresql_adapter, postgresql_contract_database, 735900
    )
    approval_context = postgresql_adapter.governance_context(
        approval.workspace_id,
        approval.environment_id,
        approval.approver_human_id,
        "approve_tombstone_policy",
    )
    fabricated = approval.model_copy(
        update={"approver_authority_reference": synthetic_uuid(739901)}
    )

    with pytest.raises(ValueError, match="approval head conflicts"):
        await repository.record_policy_approval(fabricated, approval_context)


async def test_postgresql_policy_supersession_requires_current_predecessor(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    _, _, _, repository, _, approval, _, _, approval_context, _ = (
        await _admitted_case(postgresql_adapter, postgresql_contract_database, 736000)
    )
    successor = approval.model_copy(
        update={
            "approval_id": synthetic_uuid(739911),
            "authority_generation": 2,
            "predecessor_approval_id": synthetic_uuid(739912),
            "audit_event_id": synthetic_uuid(739913),
        }
    )

    with pytest.raises(ValueError, match="approval head conflicts"):
        await repository.record_policy_approval(successor, approval_context)


async def test_postgresql_issuer_supersession_requires_current_predecessor(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    _, _, _, repository, _, _, issuer, _, _, issuer_context = await _admitted_case(
        postgresql_adapter, postgresql_contract_database, 736100
    )
    successor = issuer.model_copy(
        update={
            "issuer_authority_id": synthetic_uuid(739921),
            "authority_generation": 2,
            "predecessor_issuer_authority_id": synthetic_uuid(739922),
            "audit_event_id": synthetic_uuid(739923),
        }
    )

    with pytest.raises(ValueError, match="issuer head conflicts"):
        await repository.record_issuer(successor, issuer_context)
