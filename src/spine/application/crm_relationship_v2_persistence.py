"""Compose verified CRM v2 adapter evidence into one persistence handoff."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid5

from spine.application.crm_relationship_v2_contracts import (
    Bitrix24CrmRelationshipV2ReadCommand,
    Bitrix24CrmRelationshipV2ReadResult,
    CrmRelationshipBindingOutcome,
)
from spine.application.crm_relationship_v2_source_match import (
    CanonicalSourceAuthorityFence,
    CrmMappedDiskRevisionEvidence,
    CrmRelationshipV2SourceMatchResult,
)
from spine.application.persistence.outbox import OutboxIntent
from spine.domain.audit import AuditEvent
from spine.domain.sources import LifecycleEligibilityStatus
from spine.domain.sources.crm_relationship_v2 import (
    CrmEligibilityOutcome,
    CrmRelationshipClaim,
    CrmRelationshipEligibility,
    CrmRelationshipEligibilityHead,
    CrmRelationshipIdentity,
    CrmRelationshipNaturalIdentity,
    CrmRelationshipObservationReceipt,
    relationship_key,
)


class CrmRelationshipV2HandoffRejected(ValueError):
    """Disclosure-safe rejection of incomplete or inconsistent T1-T5 evidence."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class CrmRelationshipV2PersistenceRequest:
    admitted_read: Bitrix24CrmRelationshipV2ReadCommand
    binding_evidence: Bitrix24CrmRelationshipV2ReadResult
    mapping_evidence: CrmMappedDiskRevisionEvidence
    source_match: CrmRelationshipV2SourceMatchResult
    claim_id: UUID
    receipt_id: UUID
    eligibility_id: UUID
    idempotency_key: UUID
    observed_at: datetime
    expected_verification_generation: int
    lifecycle_generation: int
    claim_audit_event: AuditEvent
    claim_outbox_intent: OutboxIntent
    receipt_audit_event: AuditEvent
    receipt_outbox_intent: OutboxIntent

    def __post_init__(self) -> None:
        if any(
            value.int == 0
            for value in (
                self.claim_id,
                self.receipt_id,
                self.eligibility_id,
                self.idempotency_key,
            )
        ):
            raise ValueError("CRM persistence identifiers must be non-zero")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("CRM observation time must be timezone-aware")
        if self.expected_verification_generation < 0 or self.lifecycle_generation < 0:
            raise ValueError("CRM persistence generations must be non-negative")


@dataclass(frozen=True, slots=True)
class CrmRelationshipV2VerifiedHandoff:
    identity: CrmRelationshipIdentity
    claim: CrmRelationshipClaim
    receipt_id: UUID
    eligibility_id: UUID
    binding_method: str
    binding_page_count: int
    binding_evidence_reference: str
    binding_evidence_digest: str
    ownership_proof_reference: str | None
    ownership_proof_digest: str | None
    evidence_digest: str
    observed_at: datetime
    receipt_idempotency_key: UUID
    expected_verification_generation: int
    expected_lifecycle_generation: int
    source_authority: CanonicalSourceAuthorityFence
    claim_audit_event: AuditEvent
    claim_outbox_intent: OutboxIntent
    receipt_audit_event: AuditEvent
    receipt_outbox_intent: OutboxIntent


class CrmRelationshipV2PersistencePort(Protocol):
    async def persist_verified_handoff(
        self, handoff: CrmRelationshipV2VerifiedHandoff
    ) -> CrmRelationshipEligibilityHead: ...


class CrmRelationshipV2PersistenceService:
    """Accept only a complete, mutually consistent T1-T5 evidence chain."""

    def __init__(self, repository: CrmRelationshipV2PersistencePort) -> None:
        self._repository = repository

    async def persist(
        self, requested: CrmRelationshipV2PersistenceRequest
    ) -> CrmRelationshipEligibilityHead:
        _validate_verified_evidence(requested)
        handoff = _build_handoff(requested)
        return await self._repository.persist_verified_handoff(handoff)


def _validate_verified_evidence(
    requested: CrmRelationshipV2PersistenceRequest,
) -> None:
    command = requested.admitted_read
    binding = requested.binding_evidence
    mapped = requested.mapping_evidence
    matched = requested.source_match
    profile = command.mapping_profile

    if (
        binding.outcome is not CrmRelationshipBindingOutcome.POSITIVE_OBSERVED
        or not binding.positive_pair_observed
        or binding.page_count < 1
    ):
        raise CrmRelationshipV2HandoffRejected("binding_not_verified")
    if not matched.eligible or matched.authority is None:
        raise CrmRelationshipV2HandoffRejected("source_not_current")
    if matched.mapping_evidence != mapped:
        raise CrmRelationshipV2HandoffRejected("evidence_mismatch")

    command_scope = (
        command.workspace_id,
        command.environment_id,
        command.connection_id,
        command.connection_version_id,
    )
    mapped_scope = (
        mapped.workspace_id,
        mapped.environment_id,
        mapped.connection_id,
        mapped.connection_version_id,
    )
    profile_scope = (
        profile.workspace_id,
        profile.environment_id,
        profile.connection_id,
    )
    if command_scope != mapped_scope or profile_scope != command_scope[:3]:
        raise CrmRelationshipV2HandoffRejected("evidence_mismatch")
    if (
        profile.profile_id != mapped.mapping_profile_id
        or profile.profile_version != mapped.mapping_profile_version
        or profile.provider_instance != command.provider_instance
        or profile.activity_namespace != command.activity_namespace
        or command.activity_type_id not in profile.activity_type_ids
        or command.raw_entity_type_id not in profile.raw_entity_type_ids
        or profile.entity_namespace != command.entity_namespace
        or profile.files_namespace != mapped.files_item.namespace
        or profile.mapping_strategy != mapped.mapping_strategy
        or profile.disk_namespace != mapped.disk_revision.namespace
        or (
            profile.mapping_strategy == "attached_object"
            and profile.ownership_proof_reference != mapped.ownership_proof_reference
        )
    ):
        raise CrmRelationshipV2HandoffRejected("evidence_mismatch")
    if (
        binding.binding_method != "crm.activity.binding.list"
        or binding.raw_entity_type_id != command.raw_entity_type_id
        or binding.entity_id != command.entity_id
        or binding.normalizer_id != profile.normalizer_id
        or binding.normalizer_version != profile.normalizer_version
    ):
        raise CrmRelationshipV2HandoffRejected("evidence_mismatch")

    authority = matched.authority
    head = authority.source_head
    admission = authority.processing_admission
    lifecycle = authority.lifecycle
    if (
        matched.source_object_id != head.source_object_id
        or matched.source_revision_id != head.current_revision_id
        or (head.workspace_id, head.environment_id)
        != (command.workspace_id, command.environment_id)
        or admission.provider_connection_id != command.connection_id
        or admission.provider_connection_version_id != command.connection_version_id
        or admission.authorization_generation != command.authorization_generation
        or admission.configuration_generation != command.configuration_generation
        or lifecycle.status is not LifecycleEligibilityStatus.ELIGIBLE
    ):
        raise CrmRelationshipV2HandoffRejected("evidence_mismatch")


def _build_handoff(
    requested: CrmRelationshipV2PersistenceRequest,
) -> CrmRelationshipV2VerifiedHandoff:
    command = requested.admitted_read
    binding = requested.binding_evidence
    mapped = requested.mapping_evidence
    matched = requested.source_match
    profile = command.mapping_profile
    assert matched.source_object_id is not None
    assert matched.source_revision_id is not None

    natural = CrmRelationshipNaturalIdentity(
        workspace_id=command.workspace_id,
        environment_id=command.environment_id,
        provider_instance=command.provider_instance,
        connection_id=command.connection_id,
        activity_namespace=command.activity_namespace,
        activity_type_id=command.activity_type_id,
        activity_id=command.activity_id,
        raw_entity_type_id=command.raw_entity_type_id,
        normalizer_id=profile.normalizer_id,
        normalizer_version=profile.normalizer_version,
        entity_namespace=command.entity_namespace,
        entity_id=command.entity_id,
        files_namespace=mapped.files_item.namespace,
        files_item_id=mapped.files_item.value,
        mapping_profile_id=profile.profile_id,
    )
    identity = CrmRelationshipIdentity(
        **natural.model_dump(mode="python"),
        relationship_key=relationship_key(natural),
    )

    claim = CrmRelationshipClaim(
        claim_id=requested.claim_id,
        relationship_key=identity.relationship_key,
        workspace_id=command.workspace_id,
        environment_id=command.environment_id,
        connection_id=command.connection_id,
        connection_version_id=command.connection_version_id,
        source_object_id=matched.source_object_id,
        source_revision_id=matched.source_revision_id,
        disk_namespace=mapped.disk_revision.namespace,
        disk_object_id=mapped.disk_revision.object_id,
        disk_version_evidence=mapped.disk_revision.provider_revision,
        mapping_profile_id=profile.profile_id,
        mapping_profile_version=profile.profile_version,
        acting_human_id=command.acting_human_id,
        human_authorization_generation=command.human_authorization_generation,
        authorization_generation=command.authorization_generation,
        configuration_generation=command.configuration_generation,
        idempotency_key=requested.idempotency_key,
        payload_digest="0" * 64,
        created_at=requested.observed_at,
    )
    claim = claim.model_copy(update={"payload_digest": _record_digest(claim)})

    assert matched.authority is not None
    return CrmRelationshipV2VerifiedHandoff(
        identity=identity,
        claim=claim,
        receipt_id=requested.receipt_id,
        eligibility_id=requested.eligibility_id,
        binding_method=binding.binding_method,
        binding_page_count=binding.page_count,
        binding_evidence_reference=binding.binding_evidence_reference,
        binding_evidence_digest=binding.binding_evidence_digest,
        ownership_proof_reference=mapped.ownership_proof_reference,
        ownership_proof_digest=mapped.ownership_proof_digest,
        evidence_digest=_evidence_digest(requested),
        observed_at=requested.observed_at,
        receipt_idempotency_key=uuid5(
            requested.idempotency_key, "crm-relationship-v2:receipt"
        ),
        expected_verification_generation=requested.expected_verification_generation,
        expected_lifecycle_generation=requested.lifecycle_generation,
        source_authority=matched.authority,
        claim_audit_event=requested.claim_audit_event,
        claim_outbox_intent=requested.claim_outbox_intent,
        receipt_audit_event=requested.receipt_audit_event,
        receipt_outbox_intent=requested.receipt_outbox_intent,
    )


def materialize_verified_observation(
    handoff: CrmRelationshipV2VerifiedHandoff,
    *,
    verification_generation: int,
    lifecycle_generation: int,
) -> tuple[CrmRelationshipObservationReceipt, CrmRelationshipEligibility]:
    """Build final records after PostgreSQL has locked and allocated generations."""
    identity, claim = handoff.identity, handoff.claim
    receipt = CrmRelationshipObservationReceipt(
        receipt_id=handoff.receipt_id,
        claim_id=claim.claim_id,
        workspace_id=claim.workspace_id,
        environment_id=claim.environment_id,
        provider_instance=identity.provider_instance,
        connection_id=claim.connection_id,
        connection_version_id=claim.connection_version_id,
        activity_namespace=identity.activity_namespace,
        activity_type_id=identity.activity_type_id,
        activity_id=identity.activity_id,
        raw_entity_type_id=identity.raw_entity_type_id,
        normalizer_id=identity.normalizer_id,
        normalizer_version=identity.normalizer_version,
        entity_namespace=identity.entity_namespace,
        entity_id=identity.entity_id,
        files_namespace=identity.files_namespace,
        files_item_id=identity.files_item_id,
        mapping_profile_id=claim.mapping_profile_id,
        mapping_profile_version=claim.mapping_profile_version,
        binding_method=handoff.binding_method,
        binding_page_count=handoff.binding_page_count,
        binding_evidence_reference=handoff.binding_evidence_reference,
        binding_evidence_digest=handoff.binding_evidence_digest,
        positive_pair_observed=True,
        ownership_proof_reference=handoff.ownership_proof_reference,
        ownership_proof_digest=handoff.ownership_proof_digest,
        disk_namespace=claim.disk_namespace,
        disk_object_id=claim.disk_object_id,
        disk_version_evidence=claim.disk_version_evidence,
        source_object_id=claim.source_object_id,
        source_revision_id=claim.source_revision_id,
        acting_human_id=claim.acting_human_id,
        human_authorization_generation=claim.human_authorization_generation,
        authorization_generation=claim.authorization_generation,
        configuration_generation=claim.configuration_generation,
        verification_generation=verification_generation,
        outcome=CrmEligibilityOutcome.ELIGIBLE_VERIFIED,
        evidence_digest=handoff.evidence_digest,
        observed_at=handoff.observed_at,
        idempotency_key=handoff.receipt_idempotency_key,
        payload_digest="0" * 64,
    )
    receipt = receipt.model_copy(update={"payload_digest": _record_digest(receipt)})
    eligibility = CrmRelationshipEligibility(
        eligibility_id=handoff.eligibility_id,
        claim_id=claim.claim_id,
        workspace_id=claim.workspace_id,
        environment_id=claim.environment_id,
        verification_generation=verification_generation,
        lifecycle_generation=lifecycle_generation,
        outcome=CrmEligibilityOutcome.ELIGIBLE_VERIFIED,
        evaluated_at=handoff.observed_at,
    )
    return receipt, eligibility


def _record_digest(
    record: CrmRelationshipClaim | CrmRelationshipObservationReceipt,
) -> str:
    payload = record.model_dump(mode="json", exclude={"payload_digest"})
    return _digest(payload)


def _evidence_digest(requested: CrmRelationshipV2PersistenceRequest) -> str:
    mapped = requested.mapping_evidence
    matched = requested.source_match
    assert matched.authority is not None
    return _digest(
        {
            "binding_digest": requested.binding_evidence.binding_evidence_digest,
            "ownership_digest": mapped.ownership_proof_digest,
            "disk_namespace": mapped.disk_revision.namespace,
            "disk_object_id": mapped.disk_revision.object_id,
            "disk_version": mapped.disk_revision.provider_revision,
            "source_object_id": str(matched.source_object_id),
            "source_revision_id": str(matched.source_revision_id),
            "source_head_cas": matched.authority.source_head.cas_version,
            "processing_admission_id": str(
                matched.authority.processing_admission.processing_admission_id
            ),
            "lifecycle_decision_id": str(
                matched.authority.lifecycle.current_decision_id
            ),
            "lifecycle_generation": matched.authority.lifecycle.generation,
        }
    )


def _digest(payload: object) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


__all__ = [
    "CrmRelationshipV2HandoffRejected",
    "CrmRelationshipV2PersistencePort",
    "CrmRelationshipV2PersistenceRequest",
    "CrmRelationshipV2PersistenceService",
    "CrmRelationshipV2VerifiedHandoff",
    "materialize_verified_observation",
]
