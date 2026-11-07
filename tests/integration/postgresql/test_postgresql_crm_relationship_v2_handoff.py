from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from spine.application.connections.bitrix24_oauth import (
    Bitrix24PinnedGrant,
    SecretTokenPair,
)
from spine.application.crm_relationship_v2_contracts import (
    Bitrix24CrmRelationshipV2ReadCommand,
    Bitrix24CrmRelationshipV2ReadResult,
    CrmRelationshipBindingOperationsProfile,
    CrmRelationshipBindingOutcome,
)
from spine.application.crm_relationship_v2_persistence import (
    CrmRelationshipV2PersistenceRequest,
    CrmRelationshipV2PersistenceService,
)
from spine.application.crm_relationship_v2_source_match import (
    CanonicalSourceAuthorityFence,
    CrmFilesItemIdentity,
    CrmMappedDiskRevisionEvidence,
    CrmRelationshipV2SourceMatchResult,
    DiskRevisionIdentity,
)
from spine.application.diagnostics.audit import AuditEventRegistry
from spine.application.persistence.errors import (
    IdempotencyConflictError,
    OptimisticConflictError,
    UnknownCommitOutcomeError,
)
from spine.application.persistence.outbox import OpaqueObjectReference, OutboxIntent
from spine.domain.audit import AuditObjectReference, AuditOutcome, CommandAuditPayload
from spine.domain.audit.models import ContextOrigin
from spine.domain.connections.provider import OpaqueProviderReference
from spine.domain.sources import LifecycleEligibilityStatus
from spine.domain.sources.crm_relationship_v2 import (
    CrmEligibilityOutcome,
    CrmRelationshipMappingProfile,
)
from spine.domain.sources.lifecycle import (
    LifecycleDecisionEffect,
    LifecycleObservationKind,
    LifecycleReasonCode,
)
from spine.infrastructure.persistence.crm_relationship_v2 import (
    PostgreSQLCrmRelationshipV2Repository,
)
from spine.infrastructure.persistence.postgresql_lifecycle import (
    PostgreSQLLifecycleAuthorityRepository,
)
from tests.integration.postgresql.test_postgresql_lifecycle_authority import (
    _AllowAuthority,
    _AllowObservationAuthority,
    _DecisionEffects,
    _decision,
    _observation,
)
from tests.integration.postgresql.test_postgresql_unit_of_work import (
    POSTGRESQL_SEARCH_PATH_OPTIONS,
    PersistenceAdapter,
    PostgreSQLContractDatabase,
    postgresql_adapter,
    postgresql_contract_database,
    prepare_canonical_case,
    synthetic_uuid,
)
from tests.contracts.persistence.test_source_observation_contract import (
    connector_source,
)

pytestmark = [pytest.mark.postgresql, pytest.mark.asyncio(loop_scope="session")]
NOW = datetime(2026, 11, 7, 13, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class HandoffCase:
    service: CrmRelationshipV2PersistenceService
    repository: PostgreSQLCrmRelationshipV2Repository
    request: CrmRelationshipV2PersistenceRequest
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID


async def _prepare_handoff_case(
    adapter: PersistenceAdapter,
    database: PostgreSQLContractDatabase,
    number: int,
    *,
    uow_factory=None,
) -> HandoffCase:
    workspace_seed = synthetic_uuid(number)
    environment_seed = synthetic_uuid(number + 1)
    case = await prepare_canonical_case(
        adapter,
        number,
        source_object=connector_source(
            workspace_seed, environment_seed, f"crm-v2-{number}"
        ),
    )
    workspace_id = case.source.workspace_id
    environment_id = case.source.environment_id
    actor_id = synthetic_uuid(961)

    lifecycle_context = adapter.ingestion_context(
        workspace_id, environment_id, "lifecycle_decision"
    )
    lifecycle_repository = PostgreSQLLifecycleAuthorityRepository(
        uow_factory=adapter.uow_factory,
        observation_authority=_AllowObservationAuthority(),
        decision_authority=_AllowAuthority(),
        decision_effects=_DecisionEffects(),
    )
    async with adapter.uow_factory(case.context) as uow:
        await uow.canonical.accept(case.command)
        await uow.commit()
    observation = _observation(case, number + 100).model_copy(
        update={
            "kind": LifecycleObservationKind.MOVE_WITHIN_SCOPE,
            "reason_code": LifecycleReasonCode.MOVE_WITHIN_SCOPE,
        }
    )
    decision = _decision(case, observation, lifecycle_context, number + 200).model_copy(
        update={
            "effect": LifecycleDecisionEffect.ELIGIBLE,
            "status": LifecycleEligibilityStatus.ELIGIBLE,
        }
    )
    await lifecycle_repository.record_observation(observation, lifecycle_context)
    await lifecycle_repository.record_decision(decision, lifecycle_context)
    async with adapter.uow_factory(case.context) as uow:
        source_head = await uow.canonical.resolve_current(case.source.source_object_id)
        await uow.rollback()
    assert source_head is not None

    connection_id = synthetic_uuid(number + 300)
    connection_version_id = synthetic_uuid(number + 301)
    approval_id = synthetic_uuid(number + 302)
    qualification_ref = synthetic_uuid(number + 303)
    approval_ref = synthetic_uuid(number + 304)
    profile_id = synthetic_uuid(number + 305)
    migration_engine = create_async_engine(database.migration_url.get_secret_value())
    try:
        async with migration_engine.begin() as connection:
            await _seed_human_authority(
                connection, workspace_id, environment_id, actor_id, number
            )
            human_generation = await connection.scalar(
                text(
                    "SELECT generation FROM spine.authorization_generations "
                    "WHERE workspace_id = :workspace AND environment_id = :environment"
                ),
                {"workspace": workspace_id, "environment": environment_id},
            )
            assert human_generation is not None
            await connection.execute(
                text(
                    "INSERT INTO spine.provider_connection_approvals "
                    "(approval_id, workspace_id, environment_id, service_principal_id, "
                    "provider_kind, credential_reference, grant_reference, "
                    "approved_resource_scope_references, authorization_generation, "
                    "configuration_generation, status, recorded_at) VALUES "
                    "(:approval, :workspace, :environment, :principal, 'bitrix24', "
                    ":credential, :grant, :scopes, :generation, 1, 'active', :now)"
                ),
                {
                    "approval": approval_id,
                    "workspace": workspace_id,
                    "environment": environment_id,
                    "principal": synthetic_uuid(number + 306),
                    "credential": synthetic_uuid(number + 307),
                    "grant": synthetic_uuid(number + 308),
                    "scopes": [qualification_ref, approval_ref],
                    "generation": human_generation,
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
                    "(:version_id, :connection, :approval, 1, NULL, :workspace, "
                    ":environment, :principal, 'bitrix24', :credential, :grant, :scopes, "
                    ":generation, 1, 'active', '[]'::jsonb, 'healthy', "
                    "ARRAY[]::varchar[], :now, NULL, NULL, :receipt, :key, :digest, :now)"
                ),
                {
                    "version_id": connection_version_id,
                    "connection": connection_id,
                    "approval": approval_id,
                    "workspace": workspace_id,
                    "environment": environment_id,
                    "principal": synthetic_uuid(number + 306),
                    "credential": synthetic_uuid(number + 307),
                    "grant": synthetic_uuid(number + 308),
                    "scopes": [qualification_ref, approval_ref],
                    "generation": human_generation,
                    "now": NOW,
                    "receipt": synthetic_uuid(number + 309),
                    "key": f"crm-v2-handoff-connection-{number}",
                    "digest": "d" * 64,
                },
            )
    finally:
        await migration_engine.dispose()

    context_factory = (
        lambda operation, trace_id: adapter.interactive_environment_context(
            workspace_id, environment_id, operation, trace_id
        )
    )
    setup_repository = PostgreSQLCrmRelationshipV2Repository(
        uow_factory=adapter.uow_factory,
        context_factory=context_factory,
    )
    profile = CrmRelationshipMappingProfile(
        schema_version="bitrix24-crm-files-mapping-profile:v1",
        record_kind="mapping_profile",
        profile_id=profile_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        profile_version=1,
        provider_instance="synthetic.bitrix.portal",
        connection_id=connection_id,
        connection_approval_id=approval_id,
        qualification_reference=qualification_ref,
        approval_reference=approval_ref,
        activity_namespace="bitrix24-crm-activity",
        activity_type_ids=("meeting",),
        raw_entity_type_ids=("DEAL",),
        entity_namespace="bitrix24-crm-entity",
        files_namespace="bitrix24-crm-files-item",
        normalizer_id="crm.activity.binding.list:entityTypeId",
        normalizer_version="v1",
        read_methods=("crm.activity.binding.list",),
        mapping_strategy="direct_disk_object",
        disk_namespace="bitrix24-disk-object",
    )
    await setup_repository.register_profile(
        profile=profile,
        digest="e" * 64,
        created_at=NOW,
        actor_id=actor_id,
    )
    repository = PostgreSQLCrmRelationshipV2Repository(
        uow_factory=uow_factory or adapter.uow_factory,
        context_factory=context_factory,
    )

    admission = SimpleNamespace(
        processing_admission_id=synthetic_uuid(number + 310),
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_object_id=case.source.source_object_id,
        source_revision_id=case.revision.revision_id,
        provider_connection_id=connection_id,
        provider_connection_version_id=connection_version_id,
        authorization_generation=int(human_generation),
        configuration_generation=1,
        lifecycle_eligibility_reference=decision.decision_id,
        lifecycle_eligibility_generation=decision.generation,
    )
    lifecycle = SimpleNamespace(
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_object_id=case.source.source_object_id,
        current_decision_id=decision.decision_id,
        generation=decision.generation,
        status=LifecycleEligibilityStatus.ELIGIBLE,
    )
    authority = CanonicalSourceAuthorityFence.model_construct(
        source_head=source_head,
        processing_admission=admission,
        lifecycle=lifecycle,
    )
    mapping = CrmMappedDiskRevisionEvidence(
        workspace_id=workspace_id,
        environment_id=environment_id,
        connection_id=connection_id,
        connection_version_id=connection_version_id,
        mapping_strategy="direct_disk_object",
        mapping_profile_id=profile_id,
        mapping_profile_version=1,
        files_item=CrmFilesItemIdentity(
            namespace=profile.files_namespace, value=f"files-{number}"
        ),
        disk_revision=DiskRevisionIdentity(
            namespace=profile.disk_namespace,
            object_id=f"disk-{number}",
            provider_revision="17",
        ),
    )
    source_match = CrmRelationshipV2SourceMatchResult(
        outcome=CrmEligibilityOutcome.ELIGIBLE_VERIFIED,
        source_object_id=case.source.source_object_id,
        source_revision_id=case.revision.revision_id,
        authority=authority,
        mapping_evidence=mapping,
    )
    command = Bitrix24CrmRelationshipV2ReadCommand(
        workspace_id=workspace_id,
        environment_id=environment_id,
        acting_human_id=actor_id,
        human_authorization_generation=int(human_generation),
        connection_id=connection_id,
        connection_version_id=connection_version_id,
        authorization_generation=int(human_generation),
        configuration_generation=1,
        resource_scope_reference=OpaqueProviderReference(value=qualification_ref),
        provider_instance=profile.provider_instance,
        provider_acting_user_id="synthetic-human",
        activity_namespace=profile.activity_namespace,
        activity_type_id="meeting",
        activity_id=f"activity-{number}",
        raw_entity_type_id="DEAL",
        entity_namespace=profile.entity_namespace,
        entity_id=f"deal-{number}",
        mapping_profile=profile,
        operations_profile=CrmRelationshipBindingOperationsProfile(
            profile_id="synthetic.crm-binding:v1",
            profile_version="1",
            page_size=20,
            max_pages=2,
            max_response_bytes=4096,
            max_attempts=2,
        ),
        pinned_grant=Bitrix24PinnedGrant(
            binding_id=synthetic_uuid(number + 311),
            acting_user_id="synthetic-human",
            token_epoch=1,
            security_fence=0,
            tokens=SecretTokenPair("access", "refresh"),
        ),
    )
    binding = Bitrix24CrmRelationshipV2ReadResult(
        binding_evidence_reference=f"binding-observation:{number}",
        binding_evidence_digest="a" * 64,
        positive_pair_observed=True,
        outcome=CrmRelationshipBindingOutcome.POSITIVE_OBSERVED,
        binding_method="crm.activity.binding.list",
        raw_entity_type_id="DEAL",
        entity_id=f"deal-{number}",
        normalizer_id=profile.normalizer_id,
        normalizer_version=profile.normalizer_version,
        page_count=1,
        attempt_count=1,
        response_bytes=64,
        snapshot_atomic=False,
    )
    claim_id = synthetic_uuid(number + 312)
    receipt_id = synthetic_uuid(number + 313)
    claim_audit, claim_outbox = _effects(
        workspace_id, environment_id, actor_id, claim_id, number + 320, "claim"
    )
    receipt_audit, receipt_outbox = _effects(
        workspace_id, environment_id, actor_id, receipt_id, number + 330, "receipt"
    )
    request = CrmRelationshipV2PersistenceRequest(
        admitted_read=command,
        binding_evidence=binding,
        mapping_evidence=mapping,
        source_match=source_match,
        claim_id=claim_id,
        receipt_id=receipt_id,
        eligibility_id=synthetic_uuid(number + 314),
        idempotency_key=synthetic_uuid(number + 315),
        observed_at=NOW,
        expected_verification_generation=0,
        lifecycle_generation=0,
        claim_audit_event=claim_audit,
        claim_outbox_intent=claim_outbox,
        receipt_audit_event=receipt_audit,
        receipt_outbox_intent=receipt_outbox,
    )
    return HandoffCase(
        service=CrmRelationshipV2PersistenceService(repository),
        repository=repository,
        request=request,
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_object_id=case.source.source_object_id,
        source_revision_id=case.revision.revision_id,
    )


async def _seed_human_authority(
    connection, workspace_id: UUID, environment_id: UUID, human_id: UUID, number: int
) -> None:
    for statement, parameters in (
        (
            "INSERT INTO spine.canonical_human_identities "
            "(identity_id, created_change_id, created_by) VALUES "
            "(:human, :change, 'issue-1034-test') ON CONFLICT DO NOTHING",
            {"human": human_id, "change": synthetic_uuid(number + 400)},
        ),
        (
            "INSERT INTO spine.canonical_human_identity_states "
            "(identity_id, version, status, change_id, recorded_by) VALUES "
            "(:human, 1, 'active', :change, 'issue-1034-test') "
            "ON CONFLICT DO NOTHING",
            {"human": human_id, "change": synthetic_uuid(number + 401)},
        ),
        (
            "INSERT INTO spine.workspace_memberships "
            "(membership_id, workspace_id, canonical_human_identity_id, version, "
            "status, change_id, recorded_by) VALUES "
            "(:membership, :workspace, :human, 1, 'active', :change, 'issue-1034-test')",
            {
                "membership": synthetic_uuid(number + 402),
                "workspace": workspace_id,
                "human": human_id,
                "change": synthetic_uuid(number + 403),
            },
        ),
        (
            "INSERT INTO spine.environment_memberships "
            "(membership_id, workspace_id, environment_id, canonical_human_identity_id, "
            "version, status, change_id, recorded_by) VALUES "
            "(:membership, :workspace, :environment, :human, 1, 'active', :change, "
            "'issue-1034-test')",
            {
                "membership": synthetic_uuid(number + 404),
                "workspace": workspace_id,
                "environment": environment_id,
                "human": human_id,
                "change": synthetic_uuid(number + 405),
            },
        ),
        (
            "INSERT INTO spine.environment_role_bindings "
            "(binding_id, workspace_id, environment_id, canonical_human_identity_id, "
            "role, version, status, change_id, recorded_by) VALUES "
            "(:binding, :workspace, :environment, :human, 'administrator', 1, 'active', "
            ":change, 'issue-1034-test')",
            {
                "binding": synthetic_uuid(number + 406),
                "workspace": workspace_id,
                "environment": environment_id,
                "human": human_id,
                "change": synthetic_uuid(number + 407),
            },
        ),
        (
            "INSERT INTO spine.qualification_authority_bindings "
            "(binding_id, version, qualification_id, workspace_id, environment_id, "
            "principal_kind, principal_id, purpose, operation, expires_at, status, "
            "change_id, recorded_by) VALUES "
            "(:binding, 1, :qualification, :workspace, :environment, 'human', :human, "
            "NULL, NULL, :expires, 'active', "
            ":change, 'issue-1034-test')",
            {
                "binding": synthetic_uuid(number + 408),
                "qualification": synthetic_uuid(number + 409),
                "workspace": workspace_id,
                "environment": environment_id,
                "human": human_id,
                "expires": NOW + timedelta(days=365),
                "change": synthetic_uuid(number + 410),
            },
        ),
    ):
        await connection.execute(text(statement), parameters)


def _effects(
    workspace_id: UUID,
    environment_id: UUID,
    actor_id: UUID,
    record_id: UUID,
    number: int,
    kind: str,
):
    payload = CommandAuditPayload(command_type=f"crm.relationship.{kind}")
    trace_id = synthetic_uuid(number)
    audit = AuditEventRegistry.with_default_families().build_event(
        workspace_id=workspace_id,
        environment_id=environment_id,
        event_type="command.outcome",
        schema_version=1,
        payload=payload,
        origin=ContextOrigin.INTERACTIVE,
        acting_subject_id=actor_id,
        service_principal_id=None,
        trace_id=trace_id,
        occurred_at=NOW,
        outcome=AuditOutcome.ACCEPTED,
        reason="crm_relationship_evidence_recorded",
        target=AuditObjectReference(
            object_type="crm_relationship_record",
            object_id=record_id,
            schema_version=1,
        ),
        audit_event_id=synthetic_uuid(number + 1),
    )
    outbox = OutboxIntent(
        event_id=synthetic_uuid(number + 2),
        workspace_id=workspace_id,
        environment_id=environment_id,
        event_type="crm.relationship.evidence",
        schema_version=1,
        payload=payload,
        aggregate=OpaqueObjectReference(
            object_type="crm_relationship_record",
            object_id=record_id,
            schema_version=1,
        ),
        trace_id=trace_id,
    )
    return audit, outbox


async def _counts(database: PostgreSQLContractDatabase, case: HandoffCase):
    engine = create_async_engine(
        database.migration_url.get_secret_value(),
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT "
                        "(SELECT count(*) FROM spine.crm_relationship_claims "
                        " WHERE claim_id = :claim), "
                        "(SELECT count(*) FROM spine.crm_relationship_receipts "
                        " WHERE receipt_id = :receipt), "
                        "(SELECT count(*) FROM spine.crm_relationship_eligibility_heads "
                        " WHERE claim_id = :claim), "
                        "(SELECT count(*) FROM spine.audit_events "
                        " WHERE audit_event_id IN (:claim_audit, :receipt_audit)), "
                        "(SELECT count(*) FROM spine.outbox_intents "
                        " WHERE event_id IN (:claim_outbox, :receipt_outbox))"
                    ),
                    {
                        "claim": case.request.claim_id,
                        "receipt": case.request.receipt_id,
                        "claim_audit": case.request.claim_audit_event.audit_event_id,
                        "receipt_audit": case.request.receipt_audit_event.audit_event_id,
                        "claim_outbox": case.request.claim_outbox_intent.event_id,
                        "receipt_outbox": case.request.receipt_outbox_intent.event_id,
                    },
                )
            ).one()
            return tuple(row)
    finally:
        await engine.dispose()


async def test_verified_handoff_commits_append_head_audit_and_outbox_atomically(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    case = await _prepare_handoff_case(
        postgresql_adapter, postgresql_contract_database, 103400
    )

    result = await case.service.persist(case.request)

    assert result.outcome is CrmEligibilityOutcome.ELIGIBLE_VERIFIED
    assert await _counts(postgresql_contract_database, case) == (1, 1, 1, 2, 2)


async def test_verified_handoff_exact_retry_replays_and_changed_payload_conflicts(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    case = await _prepare_handoff_case(
        postgresql_adapter, postgresql_contract_database, 103500
    )
    first = await case.service.persist(case.request)
    replay = await case.service.persist(case.request)
    changed_mapping = case.request.mapping_evidence.model_copy(
        update={
            "disk_revision": case.request.mapping_evidence.disk_revision.model_copy(
                update={"provider_revision": "18"}
            )
        }
    )
    changed = replace(
        case.request,
        mapping_evidence=changed_mapping,
        source_match=case.request.source_match.model_copy(
            update={"mapping_evidence": changed_mapping}
        ),
    )

    assert replay == first
    with pytest.raises(IdempotencyConflictError):
        await case.service.persist(changed)
    assert await _counts(postgresql_contract_database, case) == (1, 1, 1, 2, 2)


async def test_verified_handoff_stale_human_generation_rolls_back_every_effect(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    case = await _prepare_handoff_case(
        postgresql_adapter, postgresql_contract_database, 103600
    )
    stale_command = replace(
        case.request.admitted_read,
        human_authorization_generation=(
            case.request.admitted_read.human_authorization_generation + 1
        ),
    )
    stale = replace(case.request, admitted_read=stale_command)

    with pytest.raises(OptimisticConflictError, match="acting-human authority"):
        await case.service.persist(stale)
    assert await _counts(postgresql_contract_database, case) == (0, 0, 0, 0, 0)


@pytest.mark.parametrize(
    "generation_update",
    [
        {"expected_verification_generation": 1},
        {"lifecycle_generation": 1},
    ],
)
async def test_verified_handoff_late_generation_is_rejected_without_effects(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
    generation_update: dict[str, int],
) -> None:
    number = 103545 + ("lifecycle_generation" in generation_update)
    case = await _prepare_handoff_case(
        postgresql_adapter, postgresql_contract_database, number
    )

    with pytest.raises(OptimisticConflictError):
        await case.service.persist(replace(case.request, **generation_update))

    assert await _counts(postgresql_contract_database, case) == (0, 0, 0, 0, 0)


@pytest.mark.parametrize("authority_part", ["source_head", "lifecycle"])
async def test_verified_handoff_changed_t5_authority_is_rejected_atomically(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
    authority_part: str,
) -> None:
    number = 103548 + (authority_part == "lifecycle")
    case = await _prepare_handoff_case(
        postgresql_adapter, postgresql_contract_database, number
    )
    authority = case.request.source_match.authority
    assert authority is not None
    if authority_part == "source_head":
        changed_authority = authority.model_copy(
            update={
                "source_head": authority.source_head.model_copy(
                    update={"cas_version": authority.source_head.cas_version + 1}
                )
            }
        )
    else:
        changed_authority = authority.model_copy(
            update={
                "lifecycle": SimpleNamespace(
                    **{
                        **vars(authority.lifecycle),
                        "generation": authority.lifecycle.generation + 1,
                    }
                )
            }
        )
    changed_match = case.request.source_match.model_copy(
        update={"authority": changed_authority}
    )

    with pytest.raises(OptimisticConflictError):
        await case.service.persist(replace(case.request, source_match=changed_match))

    assert await _counts(postgresql_contract_database, case) == (0, 0, 0, 0, 0)


async def test_verified_handoff_concurrent_connection_successor_is_fenced(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    case = await _prepare_handoff_case(
        postgresql_adapter, postgresql_contract_database, 103650
    )
    old_version = case.request.admitted_read.connection_version_id
    engine = create_async_engine(
        postgresql_contract_database.migration_url.get_secret_value()
    )
    try:
        async with engine.begin() as connection:
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
                    "idempotency_key, command_digest, recorded_at) "
                    "SELECT :new_version, connection_id, approval_id, version + 1, "
                    "connection_version_id, workspace_id, environment_id, "
                    "service_principal_id, provider_kind, credential_reference, "
                    "grant_reference, approved_resource_scope_references, "
                    "authorization_generation, configuration_generation, status, "
                    "capabilities, health, diagnostic_codes, :now, cursor_lag_seconds, "
                    "last_successful_reconciliation_at, :receipt, :key, :digest, :now "
                    "FROM spine.provider_connection_versions "
                    "WHERE connection_version_id = :old_version"
                ),
                {
                    "new_version": synthetic_uuid(103699),
                    "old_version": old_version,
                    "receipt": synthetic_uuid(103698),
                    "key": "crm-v2-concurrent-connection-successor",
                    "digest": "9" * 64,
                    "now": NOW,
                },
            )
    finally:
        await engine.dispose()

    with pytest.raises(OptimisticConflictError, match="no longer current"):
        await case.service.persist(case.request)
    assert await _counts(postgresql_contract_database, case) == (0, 0, 0, 0, 0)


async def test_verified_handoff_invalid_receipt_effect_rolls_back_claim_and_identity(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    case = await _prepare_handoff_case(
        postgresql_adapter, postgresql_contract_database, 103700
    )
    invalid_receipt_audit = case.request.receipt_audit_event.model_copy(
        update={
            "target": case.request.receipt_audit_event.target.model_copy(
                update={"object_id": synthetic_uuid(103799)}
            )
        }
    )
    invalid = replace(case.request, receipt_audit_event=invalid_receipt_audit)

    with pytest.raises(OptimisticConflictError):
        await case.service.persist(invalid)
    assert await _counts(postgresql_contract_database, case) == (0, 0, 0, 0, 0)


async def test_verified_handoff_unknown_commit_reconciles_with_original_payload(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    unknown = [True]

    class CommitUnknownAfterSuccess:
        def __init__(self, manager) -> None:
            self.manager = manager
            self.uow = None

        async def __aenter__(self):
            self.uow = await self.manager.__aenter__()
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            return await self.manager.__aexit__(exc_type, exc_value, traceback)

        def __getattr__(self, name):
            return getattr(self.uow, name)

        async def commit(self):
            await self.uow.commit()
            if unknown[0]:
                unknown[0] = False
                raise UnknownCommitOutcomeError("synthetic unknown commit")

    uow_factory = lambda context: CommitUnknownAfterSuccess(
        postgresql_adapter.uow_factory(context)
    )
    case = await _prepare_handoff_case(
        postgresql_adapter,
        postgresql_contract_database,
        103800,
        uow_factory=uow_factory,
    )

    result = await case.service.persist(case.request)

    assert result.outcome is CrmEligibilityOutcome.ELIGIBLE_VERIFIED
    assert unknown == [False]
    assert await _counts(postgresql_contract_database, case) == (1, 1, 1, 2, 2)


async def test_verified_handoff_concurrent_changed_payload_has_one_logical_effect(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    case = await _prepare_handoff_case(
        postgresql_adapter, postgresql_contract_database, 103900
    )
    contender = replace(
        case.request,
        receipt_id=synthetic_uuid(103999),
        eligibility_id=synthetic_uuid(103998),
    )

    outcomes = await asyncio.gather(
        case.service.persist(case.request),
        case.service.persist(contender),
        return_exceptions=True,
    )

    assert sum(not isinstance(value, BaseException) for value in outcomes) == 1
    assert (
        sum(isinstance(value, IdempotencyConflictError) for value in outcomes) == 1
    ), repr(outcomes)
    assert await _counts(postgresql_contract_database, case) == (1, 1, 1, 2, 2)


async def test_verified_handoff_foreign_tenant_context_is_denied_before_write(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    case = await _prepare_handoff_case(
        postgresql_adapter, postgresql_contract_database, 104000
    )
    foreign_repository = PostgreSQLCrmRelationshipV2Repository(
        uow_factory=postgresql_adapter.uow_factory,
        context_factory=lambda operation, trace_id: (
            postgresql_adapter.interactive_environment_context(
                synthetic_uuid(104090),
                synthetic_uuid(104091),
                operation,
                trace_id,
            )
        ),
    )

    with pytest.raises(OptimisticConflictError):
        await CrmRelationshipV2PersistenceService(foreign_repository).persist(
            case.request
        )
    assert await _counts(postgresql_contract_database, case) == (0, 0, 0, 0, 0)
