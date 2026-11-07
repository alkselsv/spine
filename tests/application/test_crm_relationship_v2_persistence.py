from __future__ import annotations

from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

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
    CrmRelationshipV2HandoffRejected,
    CrmRelationshipV2PersistenceRequest,
    CrmRelationshipV2PersistenceService,
    materialize_verified_observation,
)
from spine.application.crm_relationship_v2_source_match import (
    CanonicalSourceAuthorityFence,
    CrmFilesItemIdentity,
    CrmMappedDiskRevisionEvidence,
    CrmRelationshipV2SourceMatchResult,
    DiskRevisionIdentity,
)
from spine.application.diagnostics.audit import AuditEventRegistry
from spine.application.persistence.outbox import OpaqueObjectReference, OutboxIntent
from spine.domain.audit import (
    AuditObjectReference,
    AuditOutcome,
    CommandAuditPayload,
)
from spine.domain.audit.models import ContextOrigin
from spine.domain.connections.provider import OpaqueProviderReference
from spine.domain.sources.crm_relationship_v2 import (
    CrmEligibilityOutcome,
    CrmRelationshipEligibilityHead,
    CrmRelationshipMappingProfile,
)
from tests.application.test_crm_relationship_v2_source_match import (
    current_head,
    processing_admission,
    source_candidate,
)

NOW = datetime(2026, 11, 7, 12, 30, tzinfo=timezone.utc)


def ident(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"crm-v2-source-match:{value}")


def profile() -> CrmRelationshipMappingProfile:
    return CrmRelationshipMappingProfile(
        schema_version="bitrix24-crm-files-mapping-profile:v1",
        record_kind="mapping_profile",
        profile_id=ident("profile"),
        workspace_id=ident("workspace"),
        environment_id=ident("environment"),
        profile_version=2,
        provider_instance="portal.example.invalid",
        connection_id=ident("connection"),
        connection_approval_id=ident("connection-approval"),
        qualification_reference=ident("qualification"),
        approval_reference=ident("approval"),
        activity_namespace="bitrix24-crm-activity",
        activity_type_ids=("meeting",),
        raw_entity_type_ids=("DEAL",),
        entity_namespace="bitrix24-crm-entity",
        files_namespace="bitrix24-crm-files-item",
        normalizer_id="crm.activity.binding.list:entityTypeId",
        normalizer_version="v1",
        read_methods=("crm.activity.binding.list", "crm.activity.get"),
        mapping_strategy="direct_disk_object",
        disk_namespace="bitrix24-disk-object",
    )


def admitted_read() -> Bitrix24CrmRelationshipV2ReadCommand:
    selected = profile()
    return Bitrix24CrmRelationshipV2ReadCommand(
        workspace_id=selected.workspace_id,
        environment_id=selected.environment_id,
        acting_human_id=ident("human"),
        human_authorization_generation=7,
        connection_id=selected.connection_id,
        connection_version_id=ident("connection-version"),
        authorization_generation=3,
        configuration_generation=4,
        resource_scope_reference=OpaqueProviderReference(value=ident("scope")),
        provider_instance=selected.provider_instance,
        provider_acting_user_id="provider-user-20",
        activity_namespace=selected.activity_namespace,
        activity_type_id="meeting",
        activity_id="activity-42",
        raw_entity_type_id="DEAL",
        entity_namespace=selected.entity_namespace,
        entity_id="deal-7",
        mapping_profile=selected,
        operations_profile=CrmRelationshipBindingOperationsProfile(
            profile_id="synthetic.crm-binding:v1",
            profile_version="1",
            page_size=20,
            max_pages=5,
            max_response_bytes=100_000,
            max_attempts=10,
        ),
        pinned_grant=Bitrix24PinnedGrant(
            binding_id=ident("grant"),
            acting_user_id="provider-user-20",
            token_epoch=2,
            security_fence=0,
            tokens=SecretTokenPair("access-secret", "refresh-secret"),
        ),
    )


def binding_result(
    outcome: CrmRelationshipBindingOutcome = CrmRelationshipBindingOutcome.POSITIVE_OBSERVED,
) -> Bitrix24CrmRelationshipV2ReadResult:
    return Bitrix24CrmRelationshipV2ReadResult(
        binding_evidence_reference="binding-observation:synthetic",
        binding_evidence_digest="a" * 64,
        positive_pair_observed=outcome
        is CrmRelationshipBindingOutcome.POSITIVE_OBSERVED,
        outcome=outcome,
        binding_method="crm.activity.binding.list",
        raw_entity_type_id="DEAL",
        entity_id="deal-7",
        normalizer_id="crm.activity.binding.list:entityTypeId",
        normalizer_version="v1",
        page_count=1,
        attempt_count=1,
        response_bytes=64,
        snapshot_atomic=False,
    )


def mapped_disk() -> CrmMappedDiskRevisionEvidence:
    return CrmMappedDiskRevisionEvidence(
        workspace_id=ident("workspace"),
        environment_id=ident("environment"),
        connection_id=ident("connection"),
        connection_version_id=ident("connection-version"),
        mapping_strategy="direct_disk_object",
        mapping_profile_id=ident("profile"),
        mapping_profile_version=2,
        files_item=CrmFilesItemIdentity(
            namespace="bitrix24-crm-files-item", value="files-42"
        ),
        disk_revision=DiskRevisionIdentity(
            namespace="bitrix24-disk-object",
            object_id="disk-42",
            provider_revision="17",
        ),
    )


def source_match(
    outcome: CrmEligibilityOutcome = CrmEligibilityOutcome.ELIGIBLE_VERIFIED,
) -> CrmRelationshipV2SourceMatchResult:
    if outcome is not CrmEligibilityOutcome.ELIGIBLE_VERIFIED:
        return CrmRelationshipV2SourceMatchResult(outcome=outcome)
    candidate = source_candidate()
    authority = CanonicalSourceAuthorityFence(
        source_head=current_head(candidate),
        processing_admission=processing_admission(candidate),
        lifecycle=candidate.lifecycle,
    )
    return CrmRelationshipV2SourceMatchResult(
        outcome=outcome,
        source_object_id=candidate.source.source_object_id,
        source_revision_id=candidate.revision.revision_id,
        authority=authority,
        mapping_evidence=mapped_disk(),
    )


def effects(record_id: UUID, kind: str):
    payload = CommandAuditPayload(command_type=f"crm.relationship.{kind}")
    trace_id = ident(f"{kind}-trace")
    audit = AuditEventRegistry.with_default_families().build_event(
        workspace_id=ident("workspace"),
        environment_id=ident("environment"),
        event_type="command.outcome",
        schema_version=1,
        payload=payload,
        origin=ContextOrigin.INTERACTIVE,
        acting_subject_id=ident("human"),
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
        audit_event_id=ident(f"{kind}-audit"),
    )
    outbox = OutboxIntent(
        event_id=ident(f"{kind}-outbox"),
        workspace_id=ident("workspace"),
        environment_id=ident("environment"),
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


class RepositoryFake:
    def __init__(self) -> None:
        self.handoffs = []

    async def persist_verified_handoff(self, handoff):
        self.handoffs.append(handoff)
        receipt, eligibility = materialize_verified_observation(
            handoff,
            verification_generation=handoff.expected_verification_generation + 1,
            lifecycle_generation=handoff.expected_lifecycle_generation,
        )
        return CrmRelationshipEligibilityHead(
            workspace_id=handoff.claim.workspace_id,
            environment_id=handoff.claim.environment_id,
            claim_id=handoff.claim.claim_id,
            verification_generation=receipt.verification_generation,
            lifecycle_generation=eligibility.lifecycle_generation,
            outcome=eligibility.outcome,
            receipt_id=receipt.receipt_id,
            eligibility_id=eligibility.eligibility_id,
            updated_at=receipt.observed_at,
        )


def request(**updates: object) -> CrmRelationshipV2PersistenceRequest:
    claim_id = ident("claim")
    receipt_id = ident("receipt")
    claim_audit, claim_outbox = effects(claim_id, "claim")
    receipt_audit, receipt_outbox = effects(receipt_id, "receipt")
    values = {
        "admitted_read": admitted_read(),
        "binding_evidence": binding_result(),
        "mapping_evidence": mapped_disk(),
        "source_match": source_match(),
        "claim_id": claim_id,
        "receipt_id": receipt_id,
        "eligibility_id": ident("eligibility"),
        "idempotency_key": ident("idempotency"),
        "observed_at": NOW,
        "expected_verification_generation": 0,
        "lifecycle_generation": 0,
        "claim_audit_event": claim_audit,
        "claim_outbox_intent": claim_outbox,
        "receipt_audit_event": receipt_audit,
        "receipt_outbox_intent": receipt_outbox,
    }
    values.update(updates)
    return CrmRelationshipV2PersistenceRequest(**values)


@pytest.mark.asyncio
async def test_verified_handoff_constructs_exact_typed_identity_claim_and_receipt() -> (
    None
):
    repository = RepositoryFake()

    result = await CrmRelationshipV2PersistenceService(repository).persist(request())

    assert result.outcome is CrmEligibilityOutcome.ELIGIBLE_VERIFIED
    [handoff] = repository.handoffs
    assert handoff.identity.files_item_id == "files-42"
    assert handoff.claim.disk_object_id == "disk-42"
    assert handoff.claim.disk_version_evidence == "17"
    assert handoff.claim.human_authorization_generation == 7
    receipt, _ = materialize_verified_observation(
        handoff, verification_generation=1, lifecycle_generation=0
    )
    assert receipt.human_authorization_generation == 7
    assert receipt.binding_evidence_digest == "a" * 64
    assert receipt.source_revision_id == handoff.claim.source_revision_id
    assert receipt.ownership_proof_reference is None
    assert handoff.claim.idempotency_key == ident("idempotency")
    assert receipt.idempotency_key != handoff.claim.idempotency_key


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        (
            {
                "binding_evidence": binding_result(
                    CrmRelationshipBindingOutcome.NOT_VERIFIED
                )
            },
            "binding_not_verified",
        ),
        (
            {"source_match": source_match(CrmEligibilityOutcome.STALE_CONTEXT)},
            "source_not_current",
        ),
        (
            {
                "mapping_evidence": mapped_disk().model_copy(
                    update={
                        "files_item": CrmFilesItemIdentity(
                            namespace="other", value="files-42"
                        )
                    }
                )
            },
            "evidence_mismatch",
        ),
    ],
)
async def test_unverified_or_mismatched_handoff_is_rejected_without_persistence(
    updates: dict[str, object], reason: str
) -> None:
    repository = RepositoryFake()

    with pytest.raises(CrmRelationshipV2HandoffRejected) as rejected:
        await CrmRelationshipV2PersistenceService(repository).persist(
            request(**updates)
        )

    assert rejected.value.reason == reason
    assert repository.handoffs == []


@pytest.mark.asyncio
async def test_generation_and_payload_are_stable_for_exact_retry() -> None:
    repository = RepositoryFake()
    service = CrmRelationshipV2PersistenceService(repository)
    command = request()

    await service.persist(command)
    await service.persist(command)

    first, replay = repository.handoffs
    assert replay == first
    assert first.claim.payload_digest == replay.claim.payload_digest
    first_receipt, _ = materialize_verified_observation(
        first, verification_generation=1, lifecycle_generation=0
    )
    replay_receipt, _ = materialize_verified_observation(
        replay, verification_generation=1, lifecycle_generation=0
    )
    assert first_receipt.payload_digest == replay_receipt.payload_digest
