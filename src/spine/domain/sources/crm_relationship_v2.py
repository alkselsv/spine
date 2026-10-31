"""Versioned CRM relationship evidence contracts.

These records are deliberately separate from the historical v1 material
handoff. The module contains no provider or persistence behavior.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from spine.domain.common import DefinitionModel

CRM_RELATIONSHIP_V2 = "spine.crm-relationship-evidence:v2"


class CrmRelationshipKind(str, Enum):
    IDENTITY = "logical_relationship"
    CLAIM = "claim"
    RECEIPT = "observation_receipt"
    LIFECYCLE = "lifecycle_event"


class CrmEligibilityOutcome(str, Enum):
    ELIGIBLE_VERIFIED = "ELIGIBLE_VERIFIED"
    NOT_VERIFIED = "NOT_VERIFIED"
    INACCESSIBLE = "INACCESSIBLE"
    STALE_CONTEXT = "STALE_CONTEXT"
    GOVERNANCE_WITHDRAWN = "GOVERNANCE_WITHDRAWN"
    SOURCE_INELIGIBLE = "SOURCE_INELIGIBLE"
    AUTHORITY_UNAVAILABLE = "AUTHORITY_UNAVAILABLE"
    PROVIDER_REVOCATION_UNQUALIFIED = "PROVIDER_REVOCATION_UNQUALIFIED"


class CrmRelationshipNaturalIdentity(DefinitionModel):
    """Typed natural-key fields for an exact CRM relationship."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    environment_id: UUID
    provider_instance: str = Field(min_length=1, max_length=128)
    connection_id: UUID
    activity_namespace: str = Field(min_length=1, max_length=128)
    activity_type_id: str = Field(min_length=1, max_length=128)
    activity_id: str = Field(min_length=1, max_length=256)
    raw_entity_type_id: str = Field(min_length=1, max_length=128)
    normalizer_id: str = Field(min_length=1, max_length=128)
    normalizer_version: str = Field(min_length=1, max_length=64)
    entity_namespace: str = Field(min_length=1, max_length=128)
    entity_id: str = Field(min_length=1, max_length=256)
    files_namespace: str = Field(min_length=1, max_length=128)
    files_item_id: str = Field(min_length=1, max_length=256)
    mapping_profile_id: UUID

    @model_validator(mode="after")
    def validate_natural_identity(self) -> "CrmRelationshipNaturalIdentity":
        for name in ("workspace_id", "environment_id", "connection_id", "mapping_profile_id"):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be non-zero")
        return self


class CrmRelationshipIdentity(CrmRelationshipNaturalIdentity):
    """Stable identity for an exact activity/entity/FILES item association."""

    schema_version: Literal["spine.crm-relationship-evidence:v2"] = CRM_RELATIONSHIP_V2
    record_kind: Literal["logical_relationship"] = "logical_relationship"
    relationship_key: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def key_matches_fields(self) -> "CrmRelationshipIdentity":
        expected = relationship_key(self)
        if expected != self.relationship_key:
            raise ValueError("relationship_key does not match natural identity")
        return self


class CrmRelationshipMappingProfile(DefinitionModel):
    """Immutable, exact-flow mapping with retained approval references."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["bitrix24-crm-files-mapping-profile:v1"]
    record_kind: Literal["mapping_profile"]
    profile_id: UUID
    workspace_id: UUID
    environment_id: UUID
    profile_version: int = Field(ge=1)
    provider_instance: str = Field(min_length=1, max_length=128)
    connection_id: UUID
    connection_approval_id: UUID
    qualification_reference: UUID
    approval_reference: UUID
    activity_namespace: str = Field(min_length=1, max_length=128)
    activity_type_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    raw_entity_type_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    entity_namespace: str = Field(min_length=1, max_length=128)
    files_namespace: str = Field(min_length=1, max_length=128)
    normalizer_id: str = Field(min_length=1, max_length=128)
    normalizer_version: str = Field(min_length=1, max_length=64)
    read_methods: tuple[str, ...] = Field(min_length=1, max_length=16)
    mapping_strategy: Literal["direct_disk_object", "attached_object"]
    ownership_proof_reference: str | None = Field(default=None, max_length=255)
    disk_namespace: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_profile(self) -> "CrmRelationshipMappingProfile":
        for name in (
            "profile_id", "workspace_id", "environment_id", "connection_id",
            "connection_approval_id", "qualification_reference", "approval_reference",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be non-zero")
        if "crm.activity.binding.list" not in self.read_methods:
            raise ValueError("mapping profile must qualify exact activity binding reads")
        if len(set(self.read_methods)) != len(self.read_methods):
            raise ValueError("mapping profile read methods must be unique")
        if self.mapping_strategy == "attached_object" and not self.ownership_proof_reference:
            raise ValueError("attached-object mapping requires an ownership proof reference")
        return self


def relationship_key(identity: CrmRelationshipNaturalIdentity) -> str:
    """Return the ADR-0053 key from identity natural-key fields only."""
    names = (
        "workspace_id", "environment_id", "provider_instance", "connection_id",
        "activity_namespace", "activity_id", "raw_entity_type_id",
        "normalizer_id", "normalizer_version", "entity_namespace", "entity_id",
        "files_namespace", "files_item_id", "mapping_profile_id",
    )
    fields = {key: getattr(identity, key) for key in names}
    canonical = json.dumps(
        {key: str(value) for key, value in fields.items()},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(("spine.crm-relationship:v2\0" + canonical).encode()).hexdigest()


class CrmRelationshipClaim(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["spine.crm-relationship-evidence:v2"] = CRM_RELATIONSHIP_V2
    record_kind: Literal["claim"] = "claim"
    claim_id: UUID
    relationship_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    disk_namespace: str = Field(min_length=1, max_length=128)
    disk_object_id: str = Field(min_length=1, max_length=256)
    disk_version_evidence: str = Field(min_length=1, max_length=256)
    mapping_profile_id: UUID
    mapping_profile_version: int = Field(ge=1)
    acting_human_id: UUID
    authorization_generation: int = Field(ge=1)
    configuration_generation: int = Field(ge=1)
    idempotency_key: UUID
    payload_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @model_validator(mode="after")
    def validate_claim_scope(self) -> "CrmRelationshipClaim":
        for name in (
            "claim_id", "workspace_id", "environment_id", "connection_id",
            "connection_version_id", "source_object_id", "source_revision_id",
            "mapping_profile_id", "acting_human_id", "idempotency_key",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be non-zero")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return self


class CrmRelationshipObservationReceipt(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["spine.crm-relationship-evidence:v2"] = CRM_RELATIONSHIP_V2
    record_kind: Literal["observation_receipt"] = "observation_receipt"
    receipt_id: UUID
    claim_id: UUID
    workspace_id: UUID
    environment_id: UUID
    provider_instance: str = Field(min_length=1, max_length=128)
    connection_id: UUID
    connection_version_id: UUID
    activity_namespace: str = Field(min_length=1, max_length=128)
    activity_type_id: str = Field(min_length=1, max_length=128)
    activity_id: str = Field(min_length=1, max_length=256)
    raw_entity_type_id: str = Field(min_length=1, max_length=128)
    normalizer_id: str = Field(min_length=1, max_length=128)
    normalizer_version: str = Field(min_length=1, max_length=64)
    entity_namespace: str = Field(min_length=1, max_length=128)
    entity_id: str = Field(min_length=1, max_length=256)
    files_namespace: str = Field(min_length=1, max_length=128)
    files_item_id: str = Field(min_length=1, max_length=256)
    mapping_profile_id: UUID
    mapping_profile_version: int = Field(ge=1)
    binding_method: Literal["crm.activity.binding.list"]
    binding_page_count: int = Field(ge=1, le=100)
    binding_evidence_reference: str = Field(min_length=1, max_length=255)
    binding_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    positive_pair_observed: bool
    ownership_proof_reference: str | None = Field(default=None, max_length=255)
    ownership_proof_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    disk_namespace: str = Field(min_length=1, max_length=128)
    disk_object_id: str = Field(min_length=1, max_length=256)
    disk_version_evidence: str = Field(min_length=1, max_length=256)
    source_object_id: UUID
    source_revision_id: UUID
    acting_human_id: UUID
    authorization_generation: int = Field(ge=1)
    configuration_generation: int = Field(ge=1)
    verification_generation: int = Field(ge=1)
    outcome: CrmEligibilityOutcome
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    idempotency_key: UUID
    payload_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt_scope(self) -> "CrmRelationshipObservationReceipt":
        for name in (
            "receipt_id", "claim_id", "workspace_id", "environment_id",
            "connection_id", "connection_version_id", "mapping_profile_id", "acting_human_id",
            "source_object_id", "source_revision_id", "idempotency_key",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be non-zero")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        if (self.ownership_proof_reference is None) != (self.ownership_proof_digest is None):
            raise ValueError("ownership proof reference and digest must be provided together")
        if self.outcome is CrmEligibilityOutcome.ELIGIBLE_VERIFIED and not self.positive_pair_observed:
            raise ValueError("positive eligibility requires exact binding-pair evidence")
        return self


class CrmRelationshipLifecycleEvent(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["spine.crm-relationship-evidence:v2"] = CRM_RELATIONSHIP_V2
    record_kind: Literal["lifecycle_event"] = "lifecycle_event"
    event_id: UUID
    relationship_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    claim_id: UUID | None = None
    workspace_id: UUID
    environment_id: UUID
    generation: int = Field(ge=1)
    action: Literal["withdraw", "reapprove"]
    actor_id: UUID
    authority_id: UUID
    authority_version_id: UUID
    authorization_generation: int = Field(ge=1)
    expected_generation: int = Field(ge=0)
    predecessor_event_id: UUID | None = None
    reapproval_receipt_id: UUID | None = None
    reapproval_eligibility_id: UUID | None = None
    reason_code: str = Field(min_length=1, max_length=128)
    idempotency_key: UUID
    payload_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @model_validator(mode="after")
    def validate_lifecycle_transition(self) -> "CrmRelationshipLifecycleEvent":
        for name in (
            "event_id", "workspace_id", "environment_id", "actor_id",
            "authority_id", "authority_version_id", "idempotency_key",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be non-zero")
        if self.action == "reapprove" and (
            self.claim_id is None
            or self.predecessor_event_id is None
            or self.reapproval_receipt_id is None
            or self.reapproval_eligibility_id is None
        ):
            raise ValueError("reapproval requires an exact claim, predecessor event, and positive receipt")
        if self.action == "withdraw" and self.reapproval_receipt_id is not None:
            raise ValueError("withdrawal cannot carry a reapproval receipt")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return self


class CrmRelationshipEligibility(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["spine.crm-relationship-evidence:v2"] = CRM_RELATIONSHIP_V2
    record_kind: Literal["eligibility"] = "eligibility"
    eligibility_id: UUID
    claim_id: UUID
    workspace_id: UUID
    environment_id: UUID
    verification_generation: int = Field(ge=0)
    lifecycle_generation: int = Field(ge=0)
    outcome: CrmEligibilityOutcome
    evaluated_at: datetime

    @model_validator(mode="after")
    def validate_eligibility_scope(self) -> "CrmRelationshipEligibility":
        for name in ("eligibility_id", "claim_id", "workspace_id", "environment_id"):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be non-zero")
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("evaluated_at must be timezone-aware")
        return self


class CrmRelationshipReceiptAssessment(DefinitionModel):
    """One receipt and the eligibility outcome derived from that attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt: CrmRelationshipObservationReceipt
    eligibility: CrmRelationshipEligibility

    @model_validator(mode="after")
    def validate_assessment(self) -> "CrmRelationshipReceiptAssessment":
        receipt, eligibility = self.receipt, self.eligibility
        if (
            receipt.claim_id != eligibility.claim_id
            or receipt.workspace_id != eligibility.workspace_id
            or receipt.environment_id != eligibility.environment_id
            or receipt.verification_generation != eligibility.verification_generation
            or receipt.outcome != eligibility.outcome
        ):
            raise ValueError("receipt and eligibility must describe the same current observation")
        return self


class CrmRelationshipEligibilityHead(DefinitionModel):
    """Mutable pointer to the latest locally ordered eligibility projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["spine.crm-relationship-evidence:v2"] = CRM_RELATIONSHIP_V2
    record_kind: Literal["current_eligibility_head"] = "current_eligibility_head"
    workspace_id: UUID
    environment_id: UUID
    claim_id: UUID
    verification_generation: int = Field(ge=0)
    lifecycle_generation: int = Field(ge=0)
    outcome: CrmEligibilityOutcome
    receipt_id: UUID | None = None
    eligibility_id: UUID | None = None
    updated_at: datetime

    @model_validator(mode="after")
    def validate_head(self) -> "CrmRelationshipEligibilityHead":
        for name in ("workspace_id", "environment_id", "claim_id"):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be non-zero")
        if (self.receipt_id is None) != (self.verification_generation == 0):
            raise ValueError("receipt reference must match the verification generation")
        if self.receipt_id is None and self.eligibility_id is not None:
            raise ValueError("eligibility requires a receipt reference")
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("updated_at must be timezone-aware")
        return self


__all__ = [
    "CRM_RELATIONSHIP_V2", "CrmEligibilityOutcome", "CrmRelationshipClaim",
    "CrmRelationshipEligibility", "CrmRelationshipEligibilityHead",
    "CrmRelationshipIdentity", "CrmRelationshipKind",
    "CrmRelationshipNaturalIdentity",
    "CrmRelationshipLifecycleEvent", "CrmRelationshipMappingProfile",
    "CrmRelationshipObservationReceipt", "CrmRelationshipReceiptAssessment",
    "relationship_key",
]
