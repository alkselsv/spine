"""Immutable Access Policy definitions and activation evidence."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from spine.domain.common import DefinitionModel, VersionIdentifier


Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
PolicyIdentifier = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
]


class PrincipalKind(str, Enum):
    HUMAN = "human"
    TEAM = "team"
    SERVICE = "service"


class AccessPermission(str, Enum):
    READ_CONTENT = "read_content"
    PROCESS_CONTENT = "process_content"


class PolicyOriginKind(str, Enum):
    ELIGIBLE_TEMPLATE = "eligible_template"
    APPROVED_SOURCE_CONFIGURATION = "approved_source_configuration"
    APPROVED_PROPOSAL = "approved_proposal"


class PolicyBenefitClassification(str, Enum):
    SELF_BENEFITING = "self_benefiting"
    OTHER_ONLY = "other_only"
    REVOCATION = "revocation"


class PolicyApprovalDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class EligibilityDisposition(str, Enum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


class AccessGrant(DefinitionModel):
    """One normalized allow-only grant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    access_grant_id: UUID
    principal_kind: PrincipalKind
    principal_id: UUID
    permission: AccessPermission
    purpose: PolicyIdentifier | None = None
    operation: PolicyIdentifier | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def validate_authority_shape(self) -> "AccessGrant":
        if self.principal_kind in {PrincipalKind.HUMAN, PrincipalKind.TEAM}:
            if self.permission is not AccessPermission.READ_CONTENT:
                raise ValueError("human and team principals may only read content")
            if any(
                value is not None
                for value in (
                    self.purpose,
                    self.operation,
                    self.valid_from,
                    self.valid_until,
                )
            ):
                raise ValueError("human and team grants cannot carry service fields")
            return self
        if self.permission is not AccessPermission.PROCESS_CONTENT:
            raise ValueError("service principals may only process content")
        if self.purpose is None or self.operation is None:
            raise ValueError("service grants require purpose and operation")
        if (self.valid_from is None) != (self.valid_until is None):
            raise ValueError("service validity endpoints must both be present or absent")
        if self.valid_from is not None and self.valid_until is not None:
            _aware(self.valid_from, "valid_from")
            _aware(self.valid_until, "valid_until")
            if self.valid_until <= self.valid_from:
                raise ValueError("valid_until must be later than valid_from")
        return self


class AccessPolicyVersion(DefinitionModel):
    """One complete immutable policy; no grants means deny all."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    access_policy_version_id: UUID
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    version: int = Field(ge=1)
    predecessor_policy_version_id: UUID | None
    document_schema_version: VersionIdentifier
    document_hash: Digest
    origin_kind: PolicyOriginKind
    origin_reference: UUID
    origin_version: int = Field(ge=1)
    origin_hash: Digest
    created_by_canonical_human_id: UUID
    created_at: datetime
    grants: tuple[AccessGrant, ...] = ()

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return _aware(value, "created_at")

    @model_validator(mode="after")
    def validate_chain_and_grants(self) -> "AccessPolicyVersion":
        if (self.predecessor_policy_version_id is None) != (self.version == 1):
            raise ValueError("only policy version 1 may omit a predecessor")
        if self.predecessor_policy_version_id == self.access_policy_version_id:
            raise ValueError("a policy cannot precede itself")
        identities = {grant.access_grant_id for grant in self.grants}
        if len(identities) != len(self.grants):
            raise ValueError("grant identities must be unique")
        return self


class PolicyProposal(DefinitionModel):
    policy_proposal_id: UUID
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    proposal_version: int = Field(ge=1)
    predecessor_proposal_id: UUID | None = None
    proposed_policy_version_id: UUID
    proposed_policy_hash: Digest
    requester_canonical_human_id: UUID
    expected_policy_version_id: UUID | None
    expected_policy_generation: int = Field(ge=0)
    benefit_classification: PolicyBenefitClassification
    benefit_schema_version: VersionIdentifier
    membership_generation: int = Field(ge=1)
    authority_generation: int = Field(ge=1)
    service_authority_generation: int = Field(ge=1)
    submitted_at: datetime


class PolicyApproval(DefinitionModel):
    policy_approval_id: UUID
    workspace_id: UUID
    environment_id: UUID
    policy_proposal_id: UUID
    proposal_version: int = Field(ge=1)
    proposal_hash: Digest
    approver_canonical_human_id: UUID
    identity_proof_reference: UUID
    authority_snapshot_reference: UUID
    membership_generation: int = Field(ge=1)
    authority_generation: int = Field(ge=1)
    decision: PolicyApprovalDecision
    reason_code: PolicyIdentifier
    decided_at: datetime


class InitialPolicyEligibilityDecision(DefinitionModel):
    decision_id: UUID
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    policy_version_id: UUID
    policy_hash: Digest
    origin_kind: PolicyOriginKind
    origin_reference: UUID
    origin_version: int = Field(ge=1)
    origin_hash: Digest
    membership_generation: int = Field(ge=1)
    authority_generation: int = Field(ge=1)
    service_authority_generation: int = Field(ge=1)
    disposition: EligibilityDisposition
    reason_code: PolicyIdentifier
    evaluator_contract_version: VersionIdentifier
    authority_snapshot_reference: UUID
    audit_event_id: UUID
    decided_at: datetime


class PolicyActivation(DefinitionModel):
    policy_activation_id: UUID
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    predecessor_policy_version_id: UUID | None
    new_policy_version_id: UUID
    policy_generation: int = Field(ge=1)
    policy_proposal_id: UUID | None = None
    policy_approval_id: UUID | None = None
    initial_eligibility_decision_id: UUID | None = None
    requester_canonical_human_id: UUID
    activator_canonical_human_id: UUID
    membership_generation: int = Field(ge=1)
    authority_generation: int = Field(ge=1)
    service_authority_generation: int = Field(ge=1)
    idempotency_receipt_id: UUID
    audit_event_id: UUID
    outbox_event_id: UUID
    activated_at: datetime


class CurrentAccessPolicy(DefinitionModel):
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    access_policy_version_id: UUID
    policy_activation_id: UUID
    policy_generation: int = Field(ge=1)
    cas_version: int = Field(ge=1)


__all__ = [
    "AccessGrant",
    "AccessPermission",
    "AccessPolicyVersion",
    "CurrentAccessPolicy",
    "EligibilityDisposition",
    "InitialPolicyEligibilityDecision",
    "PolicyActivation",
    "PolicyApproval",
    "PolicyApprovalDecision",
    "PolicyBenefitClassification",
    "PolicyOriginKind",
    "PolicyProposal",
    "PrincipalKind",
]
