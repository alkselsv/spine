"""Fresh bounded revalidation for one exact CRM v2 relationship claim."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from spine.application.crm_relationship_v2_contracts import (
    Bitrix24CrmRelationshipV2ReadCommand,
    Bitrix24CrmRelationshipV2ReadResult,
    CrmRelationshipBindingOperationsProfile,
    CrmRelationshipBindingOutcome,
)
from spine.application.crm_relationship_v2_admission import (
    CrmRelationshipV2AdmissionDenied,
    CrmRelationshipV2DenialReason,
)
from spine.application.crm_relationship_v2_source_match import (
    CrmFilesItemIdentity,
    CrmMappedDiskRevisionEvidence,
    CrmRelationshipV2SourceMatchRequest,
    CrmRelationshipV2SourceMatchResult,
)
from spine.domain.common import DefinitionModel
from spine.domain.sources import LifecycleEligibilityStatus
from spine.domain.sources.crm_relationship_v2 import (
    CrmEligibilityOutcome,
    CrmRelationshipClaim,
    CrmRelationshipEligibility,
    CrmRelationshipEligibilityHead,
    CrmRelationshipIdentity,
    CrmRelationshipObservationReceipt,
    CrmRelationshipReceiptAssessment,
)


class CrmRelationshipV2RevalidationTarget(DefinitionModel):
    """The immutable claim plus the current locally ordered eligibility head."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    identity: CrmRelationshipIdentity
    claim: CrmRelationshipClaim
    eligibility_head: CrmRelationshipEligibilityHead

    @model_validator(mode="after")
    def validate_exact_target(self) -> "CrmRelationshipV2RevalidationTarget":
        identity, claim, head = self.identity, self.claim, self.eligibility_head
        if (
            claim.relationship_key != identity.relationship_key
            or (claim.workspace_id, claim.environment_id)
            != (identity.workspace_id, identity.environment_id)
            or (head.workspace_id, head.environment_id, head.claim_id)
            != (claim.workspace_id, claim.environment_id, claim.claim_id)
            or claim.connection_id != identity.connection_id
            or claim.mapping_profile_id != identity.mapping_profile_id
        ):
            raise ValueError("CRM revalidation target is inconsistent")
        return self


class CrmRelationshipV2RevalidationRequest(DefinitionModel):
    """One caller-pinned attempt; generations are CAS tokens, not timestamps."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt_id: UUID
    eligibility_id: UUID
    idempotency_key: UUID
    target: CrmRelationshipV2RevalidationTarget
    operations_profile: CrmRelationshipBindingOperationsProfile
    expected_verification_generation: int = Field(ge=0)
    expected_lifecycle_generation: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_attempt(self) -> "CrmRelationshipV2RevalidationRequest":
        if any(
            value.int == 0
            for value in (self.receipt_id, self.eligibility_id, self.idempotency_key)
        ):
            raise ValueError("CRM revalidation identifiers must be non-zero")
        if self.operations_profile.profile_kind != "synthetic":
            raise ValueError("CRM revalidation requires a synthetic Operations profile")
        head = self.target.eligibility_head
        if (
            self.expected_verification_generation != head.verification_generation
            or self.expected_lifecycle_generation != head.lifecycle_generation
        ):
            raise ValueError("CRM revalidation generations must pin the target head")
        return self


@dataclass(frozen=True, slots=True)
class CrmRelationshipV2BindingObservation:
    command: Bitrix24CrmRelationshipV2ReadCommand
    result: Bitrix24CrmRelationshipV2ReadResult


class CrmRelationshipV2MappingObservation(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: CrmEligibilityOutcome
    evidence: CrmMappedDiskRevisionEvidence | None = None

    @model_validator(mode="after")
    def validate_mapping(self) -> "CrmRelationshipV2MappingObservation":
        positive = self.outcome is CrmEligibilityOutcome.ELIGIBLE_VERIFIED
        if positive != (self.evidence is not None):
            raise ValueError("positive mapping outcome requires exact evidence")
        return self


class CrmRelationshipV2TargetPort(Protocol):
    async def resolve_current(
        self, requested: CrmRelationshipV2RevalidationRequest
    ) -> CrmRelationshipV2RevalidationTarget | None: ...


class CrmRelationshipV2BindingPort(Protocol):
    async def observe(
        self, requested: CrmRelationshipV2RevalidationRequest
    ) -> CrmRelationshipV2BindingObservation: ...


class CrmRelationshipV2MappingPort(Protocol):
    async def observe_exact(
        self,
        command: Bitrix24CrmRelationshipV2ReadCommand,
        current: CrmRelationshipV2RevalidationTarget,
    ) -> CrmRelationshipV2MappingObservation: ...


class CrmRelationshipV2SourcePort(Protocol):
    async def match(
        self, requested: CrmRelationshipV2SourceMatchRequest
    ) -> CrmRelationshipV2SourceMatchResult: ...


class CrmRelationshipV2RevalidationObservation(DefinitionModel):
    """Sanitized receipt draft awaiting persistence-owned generation allocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt_id: UUID
    eligibility_id: UUID
    idempotency_key: UUID
    target: CrmRelationshipV2RevalidationTarget
    expected_verification_generation: int = Field(ge=0)
    expected_lifecycle_generation: int = Field(ge=0)
    outcome: CrmEligibilityOutcome
    binding_method: str
    binding_page_count: int = Field(ge=0, le=100)
    binding_evidence_reference: str = Field(min_length=1, max_length=255)
    binding_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    positive_pair_observed: bool
    ownership_proof_reference: str | None = Field(default=None, max_length=255)
    ownership_proof_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    human_authorization_generation: int | None = Field(default=None, ge=1)
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime

    @model_validator(mode="after")
    def validate_observation(self) -> "CrmRelationshipV2RevalidationObservation":
        if self.binding_method != "crm.activity.binding.list":
            raise ValueError("CRM revalidation requires the exact binding method")
        if self.positive_pair_observed and self.binding_page_count == 0:
            raise ValueError("positive binding evidence requires an observed page")
        if (self.ownership_proof_reference is None) != (
            self.ownership_proof_digest is None
        ):
            raise ValueError("ownership proof reference and digest must be paired")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("CRM revalidation observation time must be timezone-aware")
        return self


class CrmRelationshipV2RevalidationResult(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: CrmEligibilityOutcome
    observation: CrmRelationshipV2RevalidationObservation

    @property
    def eligible(self) -> bool:
        return self.outcome is CrmEligibilityOutcome.ELIGIBLE_VERIFIED

    @property
    def grants_disclosure(self) -> bool:
        return False

    @property
    def grants_pricing_basis(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class CrmRelationshipV2RevalidationService:
    """Recheck T1-T5 and local generations without granting content access."""

    targets: CrmRelationshipV2TargetPort
    bindings: CrmRelationshipV2BindingPort
    mappings: CrmRelationshipV2MappingPort
    sources: CrmRelationshipV2SourcePort
    clock: Callable[[], datetime]

    async def revalidate(
        self, requested: CrmRelationshipV2RevalidationRequest
    ) -> CrmRelationshipV2RevalidationResult:
        try:
            current = await self.targets.resolve_current(requested)
        except Exception:
            return self._result(requested, CrmEligibilityOutcome.AUTHORITY_UNAVAILABLE)
        if current is None or current != requested.target:
            return self._result(requested, CrmEligibilityOutcome.STALE_CONTEXT)
        if (
            current.eligibility_head.outcome
            is CrmEligibilityOutcome.GOVERNANCE_WITHDRAWN
        ):
            return self._result(requested, CrmEligibilityOutcome.GOVERNANCE_WITHDRAWN)

        try:
            binding = await self.bindings.observe(requested)
        except CrmRelationshipV2AdmissionDenied as denial:
            return self._result(requested, _admission_outcome(denial.reason))
        except Exception:
            return self._result(requested, CrmEligibilityOutcome.AUTHORITY_UNAVAILABLE)
        if not _command_matches_target(binding.command, current, requested):
            return self._result(
                requested, CrmEligibilityOutcome.STALE_CONTEXT, binding=binding
            )
        if (
            binding.result.outcome
            is not CrmRelationshipBindingOutcome.POSITIVE_OBSERVED
        ):
            return self._result(
                requested, _binding_outcome(binding.result.outcome), binding=binding
            )
        if not _binding_matches_command(binding.result, binding.command):
            return self._result(
                requested, CrmEligibilityOutcome.NOT_VERIFIED, binding=binding
            )
        try:
            mapping = await self.mappings.observe_exact(binding.command, current)
        except Exception:
            return self._result(
                requested,
                CrmEligibilityOutcome.AUTHORITY_UNAVAILABLE,
                binding=binding,
            )
        if mapping.outcome is not CrmEligibilityOutcome.ELIGIBLE_VERIFIED:
            return self._result(
                requested, mapping.outcome, binding=binding, mapping=mapping
            )
        assert mapping.evidence is not None
        if not _mapping_matches_target(mapping.evidence, current, binding.command):
            return self._result(
                requested,
                CrmEligibilityOutcome.STALE_CONTEXT,
                binding=binding,
                mapping=mapping,
            )
        try:
            matched = await self.sources.match(
                CrmRelationshipV2SourceMatchRequest(
                    workspace_id=current.claim.workspace_id,
                    environment_id=current.claim.environment_id,
                    connection_id=current.claim.connection_id,
                    connection_version_id=current.claim.connection_version_id,
                    relationship_files_item=CrmFilesItemIdentity(
                        namespace=current.identity.files_namespace,
                        value=current.identity.files_item_id,
                    ),
                    mapping_evidence=mapping.evidence,
                )
            )
        except ValueError:
            return self._result(
                requested,
                CrmEligibilityOutcome.STALE_CONTEXT,
                binding=binding,
                mapping=mapping,
            )
        except Exception:
            return self._result(
                requested,
                CrmEligibilityOutcome.AUTHORITY_UNAVAILABLE,
                binding=binding,
                mapping=mapping,
            )
        if matched.outcome is not CrmEligibilityOutcome.ELIGIBLE_VERIFIED:
            return self._result(
                requested,
                matched.outcome,
                binding=binding,
                mapping=mapping,
                source=matched,
            )
        source_outcome = _source_mismatch_outcome(
            matched, current, mapping.evidence, binding.command
        )
        if source_outcome is not None:
            return self._result(
                requested,
                source_outcome,
                binding=binding,
                mapping=mapping,
                source=matched,
            )
        try:
            latest = await self.targets.resolve_current(requested)
        except Exception:
            return self._result(
                requested,
                CrmEligibilityOutcome.AUTHORITY_UNAVAILABLE,
                binding=binding,
                mapping=mapping,
                source=matched,
            )
        if (
            latest is not None
            and latest.eligibility_head.outcome
            is CrmEligibilityOutcome.GOVERNANCE_WITHDRAWN
        ):
            return self._result(
                requested,
                CrmEligibilityOutcome.GOVERNANCE_WITHDRAWN,
                binding=binding,
                mapping=mapping,
                source=matched,
            )
        if latest is None or latest != current:
            return self._result(
                requested,
                CrmEligibilityOutcome.STALE_CONTEXT,
                binding=binding,
                mapping=mapping,
                source=matched,
            )
        return self._result(
            requested,
            CrmEligibilityOutcome.ELIGIBLE_VERIFIED,
            binding=binding,
            mapping=mapping,
            source=matched,
        )

    def _result(
        self,
        requested: CrmRelationshipV2RevalidationRequest,
        outcome: CrmEligibilityOutcome,
        *,
        binding: CrmRelationshipV2BindingObservation | None = None,
        mapping: CrmRelationshipV2MappingObservation | None = None,
        source: CrmRelationshipV2SourceMatchResult | None = None,
    ) -> CrmRelationshipV2RevalidationResult:
        target = requested.target
        observed_at = self.clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("CRM revalidation clock must be timezone-aware")
        binding_result = None if binding is None else binding.result
        mapping_evidence = None if mapping is None else mapping.evidence
        observation = CrmRelationshipV2RevalidationObservation(
            receipt_id=requested.receipt_id,
            eligibility_id=requested.eligibility_id,
            idempotency_key=requested.idempotency_key,
            target=target,
            expected_verification_generation=requested.expected_verification_generation,
            expected_lifecycle_generation=requested.expected_lifecycle_generation,
            outcome=outcome,
            binding_method="crm.activity.binding.list",
            binding_page_count=(
                0 if binding_result is None else binding_result.page_count
            ),
            binding_evidence_reference=(
                "spine:crm-revalidation:unobserved"
                if binding_result is None
                else binding_result.binding_evidence_reference
            ),
            binding_evidence_digest=(
                _digest({"binding": "unobserved", "outcome": outcome.value})
                if binding_result is None
                else binding_result.binding_evidence_digest
            ),
            positive_pair_observed=(
                False
                if binding_result is None
                else binding_result.positive_pair_observed
            ),
            ownership_proof_reference=(
                None
                if mapping_evidence is None
                else mapping_evidence.ownership_proof_reference
            ),
            ownership_proof_digest=(
                None
                if mapping_evidence is None
                else mapping_evidence.ownership_proof_digest
            ),
            human_authorization_generation=(
                target.claim.human_authorization_generation
                if binding is None
                else binding.command.human_authorization_generation
            ),
            evidence_digest=_evidence_digest(
                target=target,
                outcome=outcome,
                binding=binding_result,
                mapping=mapping_evidence,
                source=source,
            ),
            observed_at=observed_at,
        )
        return CrmRelationshipV2RevalidationResult(
            outcome=outcome,
            observation=observation,
        )


def materialize_revalidation_observation(
    observation: CrmRelationshipV2RevalidationObservation,
    *,
    verification_generation: int,
    lifecycle_generation: int,
) -> CrmRelationshipReceiptAssessment:
    """Build persisted records only after the store allocates/fences generations."""

    if verification_generation != observation.expected_verification_generation + 1:
        raise ValueError(
            "CRM verification generation was not allocated from the expected head"
        )
    if lifecycle_generation != observation.expected_lifecycle_generation:
        raise ValueError(
            "CRM lifecycle generation changed before receipt materialization"
        )
    target = observation.target
    identity, claim = target.identity, target.claim
    receipt = CrmRelationshipObservationReceipt(
        receipt_id=observation.receipt_id,
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
        binding_method="crm.activity.binding.list",
        binding_page_count=observation.binding_page_count,
        binding_evidence_reference=observation.binding_evidence_reference,
        binding_evidence_digest=observation.binding_evidence_digest,
        positive_pair_observed=observation.positive_pair_observed,
        ownership_proof_reference=observation.ownership_proof_reference,
        ownership_proof_digest=observation.ownership_proof_digest,
        disk_namespace=claim.disk_namespace,
        disk_object_id=claim.disk_object_id,
        disk_version_evidence=claim.disk_version_evidence,
        source_object_id=claim.source_object_id,
        source_revision_id=claim.source_revision_id,
        acting_human_id=claim.acting_human_id,
        human_authorization_generation=observation.human_authorization_generation,
        authorization_generation=claim.authorization_generation,
        configuration_generation=claim.configuration_generation,
        verification_generation=verification_generation,
        outcome=observation.outcome,
        evidence_digest=observation.evidence_digest,
        observed_at=observation.observed_at,
        idempotency_key=observation.idempotency_key,
        payload_digest="0" * 64,
    )
    receipt = receipt.model_copy(update={"payload_digest": _record_digest(receipt)})
    eligibility = CrmRelationshipEligibility(
        eligibility_id=observation.eligibility_id,
        claim_id=claim.claim_id,
        workspace_id=claim.workspace_id,
        environment_id=claim.environment_id,
        verification_generation=verification_generation,
        lifecycle_generation=lifecycle_generation,
        outcome=observation.outcome,
        evaluated_at=observation.observed_at,
    )
    return CrmRelationshipReceiptAssessment(receipt=receipt, eligibility=eligibility)


def _binding_outcome(outcome: CrmRelationshipBindingOutcome) -> CrmEligibilityOutcome:
    return {
        CrmRelationshipBindingOutcome.NOT_VERIFIED: CrmEligibilityOutcome.NOT_VERIFIED,
        CrmRelationshipBindingOutcome.INACCESSIBLE: CrmEligibilityOutcome.INACCESSIBLE,
        CrmRelationshipBindingOutcome.AUTHORITY_UNAVAILABLE: CrmEligibilityOutcome.AUTHORITY_UNAVAILABLE,
        CrmRelationshipBindingOutcome.POSITIVE_OBSERVED: CrmEligibilityOutcome.ELIGIBLE_VERIFIED,
    }[outcome]


def _command_matches_target(
    command: Bitrix24CrmRelationshipV2ReadCommand,
    target: CrmRelationshipV2RevalidationTarget,
    requested: CrmRelationshipV2RevalidationRequest,
) -> bool:
    identity, claim = target.identity, target.claim
    profile = command.mapping_profile
    return (
        command.operations_profile == requested.operations_profile
        and (
            command.workspace_id,
            command.environment_id,
            command.connection_id,
            command.connection_version_id,
            command.authorization_generation,
            command.configuration_generation,
            command.acting_human_id,
            command.human_authorization_generation,
        )
        == (
            claim.workspace_id,
            claim.environment_id,
            claim.connection_id,
            claim.connection_version_id,
            claim.authorization_generation,
            claim.configuration_generation,
            claim.acting_human_id,
            claim.human_authorization_generation,
        )
        and (
            command.provider_instance,
            command.activity_namespace,
            command.activity_type_id,
            command.activity_id,
            command.raw_entity_type_id,
            command.entity_namespace,
            command.entity_id,
        )
        == (
            identity.provider_instance,
            identity.activity_namespace,
            identity.activity_type_id,
            identity.activity_id,
            identity.raw_entity_type_id,
            identity.entity_namespace,
            identity.entity_id,
        )
        and profile.profile_id == claim.mapping_profile_id
        and profile.profile_version == claim.mapping_profile_version
        and profile.workspace_id == claim.workspace_id
        and profile.environment_id == claim.environment_id
        and profile.connection_id == claim.connection_id
        and profile.provider_instance == identity.provider_instance
        and profile.activity_namespace == identity.activity_namespace
        and identity.activity_type_id in profile.activity_type_ids
        and identity.raw_entity_type_id in profile.raw_entity_type_ids
        and profile.entity_namespace == identity.entity_namespace
        and profile.normalizer_id == identity.normalizer_id
        and profile.normalizer_version == identity.normalizer_version
        and profile.files_namespace == identity.files_namespace
        and profile.disk_namespace == claim.disk_namespace
    )


def _binding_matches_command(
    binding: Bitrix24CrmRelationshipV2ReadResult,
    command: Bitrix24CrmRelationshipV2ReadCommand,
) -> bool:
    profile = command.mapping_profile
    return (
        binding.binding_method == "crm.activity.binding.list"
        and binding.positive_pair_observed
        and binding.page_count >= 1
        and binding.raw_entity_type_id == command.raw_entity_type_id
        and binding.entity_id == command.entity_id
        and binding.normalizer_id == profile.normalizer_id
        and binding.normalizer_version == profile.normalizer_version
    )


def _mapping_matches_target(
    evidence: CrmMappedDiskRevisionEvidence,
    target: CrmRelationshipV2RevalidationTarget,
    command: Bitrix24CrmRelationshipV2ReadCommand,
) -> bool:
    identity, claim = target.identity, target.claim
    profile = command.mapping_profile
    return (
        (
            evidence.workspace_id,
            evidence.environment_id,
            evidence.connection_id,
            evidence.connection_version_id,
        )
        == (
            claim.workspace_id,
            claim.environment_id,
            claim.connection_id,
            claim.connection_version_id,
        )
        and evidence.mapping_profile_id == claim.mapping_profile_id
        and evidence.mapping_profile_version == claim.mapping_profile_version
        and evidence.mapping_strategy == profile.mapping_strategy
        and evidence.files_item
        == CrmFilesItemIdentity(
            namespace=identity.files_namespace, value=identity.files_item_id
        )
        and evidence.disk_revision.namespace == claim.disk_namespace
        and evidence.disk_revision.namespace == profile.disk_namespace
        and evidence.disk_revision.object_id == claim.disk_object_id
        and evidence.disk_revision.provider_revision == claim.disk_version_evidence
        and (
            profile.mapping_strategy == "direct_disk_object"
            or evidence.ownership_proof_reference == profile.ownership_proof_reference
        )
    )


def _source_mismatch_outcome(
    matched: CrmRelationshipV2SourceMatchResult,
    target: CrmRelationshipV2RevalidationTarget,
    evidence: CrmMappedDiskRevisionEvidence,
    command: Bitrix24CrmRelationshipV2ReadCommand,
) -> CrmEligibilityOutcome | None:
    claim = target.claim
    authority = matched.authority
    if authority is None:
        return CrmEligibilityOutcome.STALE_CONTEXT
    lifecycle = authority.lifecycle
    if lifecycle.status is LifecycleEligibilityStatus.READ_WITHDRAWN:
        return CrmEligibilityOutcome.GOVERNANCE_WITHDRAWN
    if lifecycle.status is LifecycleEligibilityStatus.AUTHORITY_UNAVAILABLE:
        return CrmEligibilityOutcome.AUTHORITY_UNAVAILABLE
    if lifecycle.status is not LifecycleEligibilityStatus.ELIGIBLE:
        return CrmEligibilityOutcome.SOURCE_INELIGIBLE
    head = authority.source_head
    admission = authority.processing_admission
    exact = (
        matched.source_object_id == claim.source_object_id
        and matched.source_revision_id == claim.source_revision_id
        and matched.mapping_evidence == evidence
        and head.source_object_id == claim.source_object_id
        and head.current_revision_id == claim.source_revision_id
        and (head.workspace_id, head.environment_id)
        == (claim.workspace_id, claim.environment_id)
        and admission.provider_connection_id == command.connection_id
        and admission.provider_connection_version_id == command.connection_version_id
        and (admission.workspace_id, admission.environment_id)
        == (claim.workspace_id, claim.environment_id)
        and admission.source_object_id == claim.source_object_id
        and admission.source_revision_id == claim.source_revision_id
        and admission.authorization_generation == command.authorization_generation
        and admission.configuration_generation == command.configuration_generation
        and (lifecycle.workspace_id, lifecycle.environment_id)
        == (claim.workspace_id, claim.environment_id)
        and lifecycle.source_object_id == claim.source_object_id
        and lifecycle.connection_id == command.connection_id
        and lifecycle.connection_version_id == command.connection_version_id
        and lifecycle.current_decision_id == admission.lifecycle_eligibility_reference
        and lifecycle.generation == admission.lifecycle_eligibility_generation
    )
    return None if exact else CrmEligibilityOutcome.STALE_CONTEXT


def _admission_outcome(reason: CrmRelationshipV2DenialReason) -> CrmEligibilityOutcome:
    if reason in {
        CrmRelationshipV2DenialReason.HUMAN_AUTHORITY_UNAVAILABLE,
        CrmRelationshipV2DenialReason.GRANT_UNAVAILABLE,
    }:
        return CrmEligibilityOutcome.AUTHORITY_UNAVAILABLE
    if reason in {
        CrmRelationshipV2DenialReason.SCOPE_UNAPPROVED,
        CrmRelationshipV2DenialReason.SERVICE_PRINCIPAL_SUBSTITUTION,
    }:
        return CrmEligibilityOutcome.INACCESSIBLE
    return CrmEligibilityOutcome.STALE_CONTEXT


def _evidence_digest(
    *,
    target: CrmRelationshipV2RevalidationTarget,
    outcome: CrmEligibilityOutcome,
    binding: Bitrix24CrmRelationshipV2ReadResult | None,
    mapping: CrmMappedDiskRevisionEvidence | None,
    source: CrmRelationshipV2SourceMatchResult | None,
) -> str:
    return _digest(
        {
            "claim_id": str(target.claim.claim_id),
            "relationship_key": target.identity.relationship_key,
            "outcome": outcome.value,
            "binding_digest": (
                None if binding is None else binding.binding_evidence_digest
            ),
            "mapping_profile_id": str(target.claim.mapping_profile_id),
            "mapping_profile_version": target.claim.mapping_profile_version,
            "ownership_digest": (
                None if mapping is None else mapping.ownership_proof_digest
            ),
            "disk_namespace": target.claim.disk_namespace,
            "disk_object_id": target.claim.disk_object_id,
            "disk_version": target.claim.disk_version_evidence,
            "source_object_id": str(target.claim.source_object_id),
            "source_revision_id": str(target.claim.source_revision_id),
            "source_outcome": None if source is None else source.outcome.value,
            "expected_verification_generation": target.eligibility_head.verification_generation,
            "expected_lifecycle_generation": target.eligibility_head.lifecycle_generation,
        }
    )


def _record_digest(record: CrmRelationshipObservationReceipt) -> str:
    return _digest(record.model_dump(mode="json", exclude={"payload_digest"}))


def _digest(payload: object) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


__all__ = [
    "CrmRelationshipV2BindingObservation",
    "CrmRelationshipV2BindingPort",
    "CrmRelationshipV2MappingObservation",
    "CrmRelationshipV2MappingPort",
    "CrmRelationshipV2RevalidationRequest",
    "CrmRelationshipV2RevalidationObservation",
    "CrmRelationshipV2RevalidationResult",
    "CrmRelationshipV2RevalidationService",
    "CrmRelationshipV2RevalidationTarget",
    "CrmRelationshipV2SourcePort",
    "CrmRelationshipV2TargetPort",
    "materialize_revalidation_observation",
]
