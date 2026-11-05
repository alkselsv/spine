"""Qualified deletion evidence and one-transition tombstone authority.

The records in this module are content-free authority artifacts.  They neither
create a tombstone nor advance ``CurrentSourceHead``; the canonical writer must
revalidate them in its own PostgreSQL unit of work.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, model_validator

from spine.domain.common import DefinitionModel


class DeletionEvidenceOutcome(str, Enum):
    CONFIRMED_QUALIFIED_DELETION = "confirmed_qualified_deletion"
    PROVIDER_OUTAGE = "provider_outage"
    PERMISSION_LOSS = "permission_loss"
    FILTERED_ABSENCE = "filtered_absence"
    INCOMPLETE_INVENTORY = "incomplete_inventory"
    MOVE_OR_TRASH_ONLY = "move_or_trash_only"
    IDENTITY_UNPROVEN = "identity_unproven"
    NOT_VERIFIED = "not_verified"


class DeletionEvidenceCompleteness(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    FILTERED = "filtered"
    UNPROVEN = "unproven"


class DeletionOrderingGuarantee(str, Enum):
    STABLE_ORDERED_DIGEST = "stable_ordered_digest"
    TRUSTED_SNAPSHOT = "trusted_snapshot"
    UNPROVEN = "unproven"


class DeletionIdentityContinuity(str, Enum):
    CONFIRMED = "confirmed"
    UNPROVEN = "unproven"


class QualifiedDeletionEvidence(DefinitionModel):
    """Immutable verifier result for one exact source and approved scope."""

    schema_version: Literal["qualified-deletion-evidence:v1"] = (
        "qualified-deletion-evidence:v1"
    )
    evidence_id: UUID
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    provider_kind: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    source_contract_id: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,127}$")
    source_contract_version: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,63}$")
    connection_id: UUID
    connection_version_id: UUID
    authorization_generation: int = Field(ge=1)
    configuration_generation: int = Field(ge=1)
    object_namespace: str = Field(min_length=1, max_length=128)
    object_identity: str = Field(min_length=1, max_length=512)
    resource_scope_reference: UUID
    scope_approval_version_id: UUID
    scope_approval_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    qualifier_service_principal_id: UUID
    qualification_authority_reference: UUID
    qualification_authority_generation: int = Field(ge=1)
    observation_id: UUID
    scan_identity: str = Field(min_length=1, max_length=255)
    profile_id: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,127}$")
    profile_version: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,63}$")
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_references: tuple[UUID, ...]
    outcome: DeletionEvidenceOutcome
    completeness: DeletionEvidenceCompleteness
    ordering: DeletionOrderingGuarantee
    identity_continuity: DeletionIdentityContinuity
    expected_count: int | None = Field(default=None, ge=0)
    observed_unique_count: int | None = Field(default=None, ge=0)
    ordered_inventory_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    observed_at: datetime
    fresh_until: datetime
    verified_at: datetime

    @model_validator(mode="after")
    def validate_evidence(self) -> "QualifiedDeletionEvidence":
        _nonzero(
            self.evidence_id,
            self.workspace_id,
            self.environment_id,
            self.source_object_id,
            self.connection_id,
            self.connection_version_id,
            self.resource_scope_reference,
            self.scope_approval_version_id,
            self.qualifier_service_principal_id,
            self.qualification_authority_reference,
            self.observation_id,
            *self.evidence_references,
        )
        if not self.evidence_references or len(set(self.evidence_references)) != len(
            self.evidence_references
        ):
            raise ValueError(
                "deletion evidence references must be non-empty and unique"
            )
        _aware(self.observed_at, self.fresh_until, self.verified_at)
        if not self.observed_at <= self.verified_at < self.fresh_until:
            raise ValueError("deletion evidence freshness interval is invalid")
        if self.outcome is DeletionEvidenceOutcome.CONFIRMED_QUALIFIED_DELETION:
            if self.provider_kind.startswith("bitrix24"):
                raise ValueError(
                    "Bitrix24 deletion guarantees are not verified for authorization"
                )
            if self.provider_kind == "yandex_disk" and (
                self.profile_id != "yandex.two-pass-removal"
                or self.ordering is not DeletionOrderingGuarantee.STABLE_ORDERED_DIGEST
            ):
                raise ValueError(
                    "Yandex qualified deletion requires the strict two-pass profile"
                )
            if (
                self.completeness is not DeletionEvidenceCompleteness.COMPLETE
                or self.ordering is DeletionOrderingGuarantee.UNPROVEN
                or self.identity_continuity is not DeletionIdentityContinuity.CONFIRMED
                or self.expected_count is None
                or self.observed_unique_count != self.expected_count
                or self.ordered_inventory_digest is None
            ):
                raise ValueError(
                    "confirmed deletion requires complete ordered exact-identity evidence"
                )
        return self

    def authorizes_issuance_at(self, evaluated_at: datetime) -> bool:
        _aware(evaluated_at)
        return (
            self.outcome is DeletionEvidenceOutcome.CONFIRMED_QUALIFIED_DELETION
            and self.completeness is DeletionEvidenceCompleteness.COMPLETE
            and self.ordering is not DeletionOrderingGuarantee.UNPROVEN
            and self.identity_continuity is DeletionIdentityContinuity.CONFIRMED
            and self.observed_at <= evaluated_at < self.fresh_until
        )


class SourceDeletionPolicyApproval(DefinitionModel):
    """A verified human approval for one exact deletion policy and scope."""

    schema_version: Literal["source-deletion-policy-approval:v1"] = (
        "source-deletion-policy-approval:v1"
    )
    approval_id: UUID
    workspace_id: UUID
    environment_id: UUID
    policy_id: UUID
    policy_version_id: UUID
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_contract_id: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,127}$")
    source_contract_version: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,63}$")
    connection_id: UUID
    resource_scope_reference: UUID
    approver_human_id: UUID
    approver_authority_reference: UUID
    approver_authority_generation: int = Field(ge=1)
    authority_generation: int = Field(ge=1)
    predecessor_approval_id: UUID | None = None
    revoked: bool = False
    approved_at: datetime
    valid_until: datetime
    audit_event_id: UUID

    @model_validator(mode="after")
    def validate_approval(self) -> "SourceDeletionPolicyApproval":
        _nonzero(
            self.approval_id,
            self.workspace_id,
            self.environment_id,
            self.policy_id,
            self.policy_version_id,
            self.connection_id,
            self.resource_scope_reference,
            self.approver_human_id,
            self.approver_authority_reference,
            self.audit_event_id,
        )
        _aware(self.approved_at, self.valid_until)
        if self.predecessor_approval_id is not None:
            _nonzero(self.predecessor_approval_id)
            if self.predecessor_approval_id == self.approval_id:
                raise ValueError("deletion policy approval cannot supersede itself")
        if self.valid_until <= self.approved_at:
            raise ValueError("deletion policy approval interval is invalid")
        return self

    def is_valid_at(self, evaluated_at: datetime) -> bool:
        _aware(evaluated_at)
        return not self.revoked and self.approved_at <= evaluated_at < self.valid_until


class TombstoneAuthorizationIssuer(DefinitionModel):
    """Current Spine service-principal authority allowed to issue artifacts."""

    schema_version: Literal["trusted-tombstone-authorization-issuer:v1"] = (
        "trusted-tombstone-authorization-issuer:v1"
    )
    issuer_authority_id: UUID
    workspace_id: UUID
    environment_id: UUID
    service_principal_id: UUID
    authorized_by_human_id: UUID
    authorized_by_authority_generation: int = Field(ge=1)
    authority_version: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,63}$")
    authority_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_generation: int = Field(ge=1)
    predecessor_issuer_authority_id: UUID | None = None
    revoked: bool = False
    valid_from: datetime
    valid_until: datetime
    audit_event_id: UUID

    @model_validator(mode="after")
    def validate_issuer(self) -> "TombstoneAuthorizationIssuer":
        _nonzero(
            self.issuer_authority_id,
            self.workspace_id,
            self.environment_id,
            self.service_principal_id,
            self.authorized_by_human_id,
            self.audit_event_id,
        )
        _aware(self.valid_from, self.valid_until)
        if self.predecessor_issuer_authority_id is not None:
            _nonzero(self.predecessor_issuer_authority_id)
            if self.predecessor_issuer_authority_id == self.issuer_authority_id:
                raise ValueError("tombstone issuer cannot supersede itself")
        if self.valid_until <= self.valid_from:
            raise ValueError("tombstone issuer interval is invalid")
        return self

    def is_valid_at(self, evaluated_at: datetime) -> bool:
        _aware(evaluated_at)
        return not self.revoked and self.valid_from <= evaluated_at < self.valid_until


class TombstoneAuthorization(DefinitionModel):
    """Immutable, single-transition authorization consumed by the v3 writer."""

    schema_version: Literal["tombstone-authorization:v1"] = "tombstone-authorization:v1"
    authorization_id: UUID
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    provider_kind: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    source_contract_id: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,127}$")
    source_contract_version: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,63}$")
    connection_id: UUID
    connection_version_id: UUID
    authorization_generation: int = Field(ge=1)
    configuration_generation: int = Field(ge=1)
    object_namespace: str = Field(min_length=1, max_length=128)
    object_identity: str = Field(min_length=1, max_length=512)
    resource_scope_reference: UUID
    scope_approval_version_id: UUID
    scope_approval_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    qualified_evidence_id: UUID
    qualified_evidence_profile_id: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,127}$")
    qualified_evidence_profile_version: str = Field(
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,63}$"
    )
    qualified_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_id: UUID
    deletion_policy_approval_id: UUID
    deletion_policy_version_id: UUID
    deletion_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    deletion_policy_authority_generation: int = Field(ge=1)
    approver_human_id: UUID
    approver_authority_generation: int = Field(ge=1)
    issuer_authority_id: UUID
    issuer_service_principal_id: UUID
    issuer_authority_generation: int = Field(ge=1)
    expected_current_revision_id: UUID
    expected_current_acceptance_id: UUID
    expected_current_cas_version: int = Field(ge=1)
    tombstone_revision_id: UUID
    transition_id: UUID
    retention_eligibility_reference: UUID
    retention_eligibility_generation: int = Field(ge=1)
    legal_hold_check_reference: UUID
    legal_hold_generation: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=255)
    command_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    audit_event_id: UUID
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_authorization(self) -> "TombstoneAuthorization":
        _nonzero(
            self.authorization_id,
            self.workspace_id,
            self.environment_id,
            self.source_object_id,
            self.connection_id,
            self.connection_version_id,
            self.resource_scope_reference,
            self.scope_approval_version_id,
            self.qualified_evidence_id,
            self.observation_id,
            self.deletion_policy_approval_id,
            self.deletion_policy_version_id,
            self.approver_human_id,
            self.issuer_authority_id,
            self.issuer_service_principal_id,
            self.expected_current_revision_id,
            self.expected_current_acceptance_id,
            self.tombstone_revision_id,
            self.transition_id,
            self.retention_eligibility_reference,
            self.legal_hold_check_reference,
            self.audit_event_id,
        )
        _aware(self.issued_at, self.expires_at)
        if self.expires_at <= self.issued_at:
            raise ValueError("tombstone authorization interval is invalid")
        if self.expected_current_revision_id == self.tombstone_revision_id:
            raise ValueError("tombstone authorization must advance one transition")
        return self

    @property
    def transition_binding(self) -> tuple[UUID, UUID, UUID, int, UUID, UUID]:
        return (
            self.source_object_id,
            self.expected_current_revision_id,
            self.expected_current_acceptance_id,
            self.expected_current_cas_version,
            self.tombstone_revision_id,
            self.transition_id,
        )

    def validate_expected_head(
        self, *, revision_id: UUID, acceptance_id: UUID, cas_version: int
    ) -> None:
        if (
            revision_id,
            acceptance_id,
            cas_version,
        ) != (
            self.expected_current_revision_id,
            self.expected_current_acceptance_id,
            self.expected_current_cas_version,
        ):
            raise ValueError("tombstone authorization head binding is unavailable")

    def validate_bindings(
        self,
        *,
        evidence: QualifiedDeletionEvidence,
        approval: SourceDeletionPolicyApproval,
        issuer: TombstoneAuthorizationIssuer,
        evaluated_at: datetime,
    ) -> None:
        _aware(evaluated_at)
        evidence_binding = (
            evidence.workspace_id,
            evidence.environment_id,
            evidence.source_object_id,
            evidence.provider_kind,
            evidence.source_contract_id,
            evidence.source_contract_version,
            evidence.connection_id,
            evidence.connection_version_id,
            evidence.authorization_generation,
            evidence.configuration_generation,
            evidence.object_namespace,
            evidence.object_identity,
            evidence.resource_scope_reference,
            evidence.scope_approval_version_id,
            evidence.scope_approval_digest,
            evidence.evidence_id,
            evidence.profile_id,
            evidence.profile_version,
            evidence.evidence_digest,
            evidence.observation_id,
        )
        authorization_evidence_binding = (
            self.workspace_id,
            self.environment_id,
            self.source_object_id,
            self.provider_kind,
            self.source_contract_id,
            self.source_contract_version,
            self.connection_id,
            self.connection_version_id,
            self.authorization_generation,
            self.configuration_generation,
            self.object_namespace,
            self.object_identity,
            self.resource_scope_reference,
            self.scope_approval_version_id,
            self.scope_approval_digest,
            self.qualified_evidence_id,
            self.qualified_evidence_profile_id,
            self.qualified_evidence_profile_version,
            self.qualified_evidence_digest,
            self.observation_id,
        )
        approval_binding = (
            approval.workspace_id,
            approval.environment_id,
            approval.source_contract_id,
            approval.source_contract_version,
            approval.connection_id,
            approval.resource_scope_reference,
            approval.approval_id,
            approval.policy_version_id,
            approval.policy_digest,
            approval.authority_generation,
            approval.approver_human_id,
            approval.approver_authority_generation,
        )
        authorization_approval_binding = (
            self.workspace_id,
            self.environment_id,
            self.source_contract_id,
            self.source_contract_version,
            self.connection_id,
            self.resource_scope_reference,
            self.deletion_policy_approval_id,
            self.deletion_policy_version_id,
            self.deletion_policy_digest,
            self.deletion_policy_authority_generation,
            self.approver_human_id,
            self.approver_authority_generation,
        )
        issuer_binding = (
            issuer.workspace_id,
            issuer.environment_id,
            issuer.issuer_authority_id,
            issuer.service_principal_id,
            issuer.authority_generation,
        )
        authorization_issuer_binding = (
            self.workspace_id,
            self.environment_id,
            self.issuer_authority_id,
            self.issuer_service_principal_id,
            self.issuer_authority_generation,
        )
        if (
            evidence_binding != authorization_evidence_binding
            or approval_binding != authorization_approval_binding
            or issuer_binding != authorization_issuer_binding
            or not evidence.authorizes_issuance_at(evaluated_at)
            or not approval.is_valid_at(evaluated_at)
            or not issuer.is_valid_at(evaluated_at)
        ):
            raise ValueError("tombstone authorization binding is unavailable")
        if self.approver_human_id == self.issuer_service_principal_id:
            raise ValueError("tombstone authority requires role separation")
        if evidence.qualifier_service_principal_id == self.issuer_service_principal_id:
            raise ValueError("tombstone authority requires qualifier separation")


class TombstoneAuthorizationDisposition(str, Enum):
    ISSUED = "issued"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"


class TombstoneAuthorizationDecision(DefinitionModel):
    """Append-only current-authority decision for an issued authorization."""

    schema_version: Literal["tombstone-authorization-decision:v1"] = (
        "tombstone-authorization-decision:v1"
    )
    decision_id: UUID
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    authorization_id: UUID
    disposition: TombstoneAuthorizationDisposition
    expected_current_decision_id: UUID | None
    expected_current_generation: int = Field(ge=0)
    generation: int = Field(ge=1)
    superseded_by_authorization_id: UUID | None = None

    @classmethod
    def issued(
        cls, authorization: TombstoneAuthorization
    ) -> "TombstoneAuthorizationDecision":
        """Build the deterministic baseline decision for a new authorization."""

        return cls(
            decision_id=uuid5(
                NAMESPACE_URL,
                "spine:tombstone-authorization-issued:v1:"
                f"{authorization.authorization_id}",
            ),
            workspace_id=authorization.workspace_id,
            environment_id=authorization.environment_id,
            source_object_id=authorization.source_object_id,
            authorization_id=authorization.authorization_id,
            disposition=TombstoneAuthorizationDisposition.ISSUED,
            expected_current_decision_id=None,
            expected_current_generation=0,
            generation=1,
            authority_reference=authorization.issuer_authority_id,
            authority_generation=authorization.issuer_authority_generation,
            audit_event_id=authorization.audit_event_id,
            decided_at=authorization.issued_at,
        )
    authority_reference: UUID
    authority_generation: int = Field(ge=1)
    audit_event_id: UUID
    decided_at: datetime

    @model_validator(mode="after")
    def validate_decision(self) -> "TombstoneAuthorizationDecision":
        _nonzero(
            self.decision_id,
            self.workspace_id,
            self.environment_id,
            self.source_object_id,
            self.authorization_id,
            self.authority_reference,
            self.audit_event_id,
        )
        if self.expected_current_decision_id is not None:
            _nonzero(self.expected_current_decision_id)
        if (self.expected_current_decision_id is None) != (
            self.expected_current_generation == 0
        ):
            raise ValueError("authorization decision expected head is invalid")
        if self.generation != self.expected_current_generation + 1:
            raise ValueError("authorization decision must advance one generation")
        if self.disposition is TombstoneAuthorizationDisposition.ISSUED:
            if self.generation != 1 or self.superseded_by_authorization_id is not None:
                raise ValueError("issued authorization decision must be the baseline")
        elif self.disposition is TombstoneAuthorizationDisposition.SUPERSEDED:
            if (
                self.superseded_by_authorization_id is None
                or self.superseded_by_authorization_id == self.authorization_id
            ):
                raise ValueError("supersession requires a distinct successor")
            _nonzero(self.superseded_by_authorization_id)
        elif self.superseded_by_authorization_id is not None:
            raise ValueError("only supersession can reference a successor")
        _aware(self.decided_at)
        return self


def _nonzero(*identifiers: UUID) -> None:
    if any(identifier.int == 0 for identifier in identifiers):
        raise ValueError("deletion authority identifiers must be non-zero")


def _aware(*timestamps: datetime) -> None:
    if any(value.tzinfo is None or value.utcoffset() is None for value in timestamps):
        raise ValueError("deletion authority timestamps must be timezone-aware")


__all__ = [
    "DeletionEvidenceCompleteness",
    "DeletionEvidenceOutcome",
    "DeletionIdentityContinuity",
    "DeletionOrderingGuarantee",
    "QualifiedDeletionEvidence",
    "SourceDeletionPolicyApproval",
    "TombstoneAuthorization",
    "TombstoneAuthorizationDecision",
    "TombstoneAuthorizationDisposition",
    "TombstoneAuthorizationIssuer",
]
