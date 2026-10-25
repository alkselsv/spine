"""Immutable evidence for deterministic Q&A completeness evaluation."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from spine.domain.common import ValidatedDefinitionModel
from spine.domain.sources import RevisionKind


_DIGEST = r"^[0-9a-f]{64}$"
_VERSION = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"


def _require_id(value: UUID, field_name: str) -> UUID:
    if value.int == 0:
        raise ValueError(f"{field_name} must be a non-zero UUID")
    return value


def _unique_ids(values: tuple[UUID, ...], field_name: str) -> tuple[UUID, ...]:
    if any(value.int == 0 for value in values):
        raise ValueError(f"{field_name} must contain non-zero UUIDs")
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} must be unique")
    return values


class _EvaluationModel(ValidatedDefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")


class AttemptProjectionBoundary(_EvaluationModel):
    """Exact attempt and projection cut to which evaluation facts belong."""

    schema_version: Literal["attempt-projection-boundary:v1"] = (
        "attempt-projection-boundary:v1"
    )
    workspace_id: UUID
    environment_id: UUID
    run_id: UUID
    attempt_id: UUID
    projection_snapshot_id: UUID
    activation_generation: int = Field(ge=1)
    canonical_boundary_id: UUID

    @field_validator(
        "workspace_id",
        "environment_id",
        "run_id",
        "attempt_id",
        "projection_snapshot_id",
        "canonical_boundary_id",
    )
    @classmethod
    def validate_id(cls, value: UUID, info: Any) -> UUID:
        return _require_id(value, info.field_name)


class TargetScopeMember(_EvaluationModel):
    """One authorized current revision required by the target source scope."""

    schema_version: Literal["target-scope-member:v1"] = "target-scope-member:v1"
    source_object_id: UUID
    source_revision_id: UUID
    authorization_decision_reference: UUID

    @field_validator(
        "source_object_id", "source_revision_id", "authorization_decision_reference"
    )
    @classmethod
    def validate_id(cls, value: UUID, info: Any) -> UUID:
        return _require_id(value, info.field_name)


class CanonicalViewMember(_EvaluationModel):
    """One exact current-head fact used to derive the target scope."""

    schema_version: Literal["canonical-view-member:v1"] = "canonical-view-member:v1"
    source_object_id: UUID
    source_revision_id: UUID
    revision_kind: RevisionKind
    accepted_generation: int = Field(ge=1)
    cas_version: int = Field(ge=1)

    @field_validator("source_object_id", "source_revision_id")
    @classmethod
    def validate_id(cls, value: UUID, info: Any) -> UUID:
        return _require_id(value, info.field_name)


class TargetCompletenessScope(_EvaluationModel):
    """Server-derived authorized scope plus its exact canonical view reference."""

    schema_version: Literal["target-completeness-scope:v1"] = (
        "target-completeness-scope:v1"
    )
    target_scope_id: UUID
    boundary: AttemptProjectionBoundary
    derivation_authority: Literal["server_policy"] = "server_policy"
    derivation_reference: UUID
    canonical_view_reference: UUID
    canonical_view_digest: str = Field(pattern=_DIGEST)
    canonical_view_generation: int = Field(ge=0)
    canonical_view_basis: Literal["current_at_evaluation"] = "current_at_evaluation"
    canonical_view_members: tuple[CanonicalViewMember, ...] = ()
    source_constraint_digest: str = Field(pattern=_DIGEST)
    members: tuple[TargetScopeMember, ...] = ()

    @field_validator(
        "target_scope_id", "derivation_reference", "canonical_view_reference"
    )
    @classmethod
    def validate_id(cls, value: UUID, info: Any) -> UUID:
        return _require_id(value, info.field_name)

    @model_validator(mode="after")
    def validate_members(self) -> "TargetCompletenessScope":
        source_ids = tuple(member.source_object_id for member in self.members)
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("target scope members must be unique")
        canonical_source_ids = tuple(
            member.source_object_id for member in self.canonical_view_members
        )
        if len(set(canonical_source_ids)) != len(canonical_source_ids):
            raise ValueError("canonical view members must be unique")
        return self


class CoverageMemberStatus(str, Enum):
    REPRESENTED = "represented"
    UNCOVERED = "uncovered"
    PROJECTION_GAP = "projection_gap"
    LIFECYCLE_GAP = "lifecycle_gap"
    STALE = "stale"
    UNKNOWN = "unknown"


class ObservedCoverageMember(_EvaluationModel):
    """Proof state for one member of the target scope."""

    schema_version: Literal["observed-coverage-member:v1"] = (
        "observed-coverage-member:v1"
    )
    source_object_id: UUID
    source_revision_id: UUID
    status: CoverageMemberStatus
    proof_reference: UUID | None = None

    @field_validator("source_object_id", "source_revision_id", "proof_reference")
    @classmethod
    def validate_id(cls, value: UUID | None, info: Any) -> UUID | None:
        if value is not None:
            return _require_id(value, info.field_name)
        return value


class ObservedCoverage(_EvaluationModel):
    """Immutable observation of represented target scope at one pinned cut."""

    schema_version: Literal["observed-coverage:v1"] = "observed-coverage:v1"
    observed_coverage_id: UUID
    target_scope_id: UUID
    boundary: AttemptProjectionBoundary
    proof_reference: UUID | None = None
    members: tuple[ObservedCoverageMember, ...]

    @field_validator("observed_coverage_id", "target_scope_id", "proof_reference")
    @classmethod
    def validate_id(cls, value: UUID | None, info: Any) -> UUID | None:
        if value is not None:
            return _require_id(value, info.field_name)
        return value

    @model_validator(mode="after")
    def validate_members(self) -> "ObservedCoverage":
        identities = tuple(
            (member.source_object_id, member.source_revision_id) for member in self.members
        )
        if len(set(identities)) != len(identities):
            raise ValueError("observed coverage members must be unique")
        return self


class ClaimGroundingStatus(str, Enum):
    GROUNDED = "grounded"
    REJECTED = "rejected"


class ClaimAuthorizationStatus(str, Enum):
    CURRENT = "current"
    REJECTED = "rejected"


class PartialClaimUtility(str, Enum):
    INDEPENDENTLY_USEFUL = "independently_useful"
    DEPENDENT = "dependent"


class MissingCoverageImpact(str, Enum):
    IMMATERIAL = "immaterial"
    MATERIAL = "material"
    UNKNOWN = "unknown"


class ClaimEvidenceValidation(_EvaluationModel):
    """Boundary-pinned grounding facts for one cited evidence item."""

    schema_version: Literal["claim-evidence-validation:v1"] = (
        "claim-evidence-validation:v1"
    )
    citation_id: UUID
    citation_validation_reference: UUID
    semantic_support_validation_reference: UUID
    authorization_validation_reference: UUID

    @field_validator(
        "citation_id",
        "citation_validation_reference",
        "semantic_support_validation_reference",
        "authorization_validation_reference",
    )
    @classmethod
    def validate_id(cls, value: UUID, info: Any) -> UUID:
        return _require_id(value, info.field_name)


class GroundedClaimAssessment(_EvaluationModel):
    """Trusted boundary-pinned result of grounding and partial-safety checks."""

    schema_version: Literal["grounded-claim-assessment:v2"] = (
        "grounded-claim-assessment:v2"
    )
    boundary: AttemptProjectionBoundary
    claim_id: UUID
    grounding_status: ClaimGroundingStatus
    authorization_status: ClaimAuthorizationStatus
    partial_utility: PartialClaimUtility
    missing_coverage_impact: MissingCoverageImpact
    evidence_validations: tuple[ClaimEvidenceValidation, ...] = Field(min_length=1)
    partial_validation_reference: UUID

    @field_validator("claim_id", "partial_validation_reference")
    @classmethod
    def validate_id(cls, value: UUID, info: Any) -> UUID:
        return _require_id(value, info.field_name)

    @model_validator(mode="after")
    def validate_evidence(self) -> "GroundedClaimAssessment":
        citation_ids = tuple(item.citation_id for item in self.evidence_validations)
        if len(set(citation_ids)) != len(citation_ids):
            raise ValueError("claim evidence validations must be unique")
        return self


class PartialFallbackAuthority(str, Enum):
    CURRENT_REQUEST = "current_request"
    ACCEPTED_PREFERENCE = "accepted_preference"


class PartialFallbackAuthorization(_EvaluationModel):
    """Explicit authorization applicable to this exact current attempt."""

    schema_version: Literal["partial-fallback-authorization:v1"] = (
        "partial-fallback-authorization:v1"
    )
    boundary: AttemptProjectionBoundary
    authority: PartialFallbackAuthority
    authorization_reference: UUID
    policy_version: str = Field(pattern=_VERSION)
    applicability_reference: UUID
    current_request_precedence: Literal["satisfied"] = "satisfied"

    @field_validator("authorization_reference", "applicability_reference")
    @classmethod
    def validate_reference(cls, value: UUID, info: Any) -> UUID:
        return _require_id(value, info.field_name)


class GroundedConflict(_EvaluationModel):
    """Server-detected contradiction between current independently grounded claims."""

    schema_version: Literal["grounded-conflict:v1"] = "grounded-conflict:v1"
    conflict_id: UUID
    boundary: AttemptProjectionBoundary
    claim_ids: tuple[UUID, ...] = Field(min_length=2, max_length=2)
    relationship_ids: tuple[UUID, ...] = Field(min_length=1)
    detection_reference: UUID
    detection_policy_version: str = Field(pattern=_VERSION)

    @field_validator("conflict_id", "detection_reference")
    @classmethod
    def validate_id(cls, value: UUID, info: Any) -> UUID:
        return _require_id(value, info.field_name)

    @field_validator("claim_ids")
    @classmethod
    def validate_claim_ids(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        return _unique_ids(value, "claim_ids")

    @field_validator("relationship_ids")
    @classmethod
    def validate_relationship_ids(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        return _unique_ids(value, "relationship_ids")


class ApprovedPrecedenceRule(_EvaluationModel):
    """Server-approved deterministic winner for one detected conflict."""

    schema_version: Literal["approved-precedence-rule:v1"] = (
        "approved-precedence-rule:v1"
    )
    precedence_rule_id: UUID
    boundary: AttemptProjectionBoundary
    conflict_id: UUID
    winning_claim_id: UUID
    rule_version: str = Field(pattern=_VERSION)
    approval_reference: UUID

    @field_validator(
        "precedence_rule_id", "conflict_id", "winning_claim_id", "approval_reference"
    )
    @classmethod
    def validate_id(cls, value: UUID, info: Any) -> UUID:
        return _require_id(value, info.field_name)


class DerivationInput(_EvaluationModel):
    """One grounded citation consumed by a deterministic transformation."""

    schema_version: Literal["derivation-input:v1"] = "derivation-input:v1"
    citation_id: UUID
    grounding_validation_reference: UUID

    @field_validator("citation_id", "grounding_validation_reference")
    @classmethod
    def validate_id(cls, value: UUID, info: Any) -> UUID:
        return _require_id(value, info.field_name)


class DeterministicDerivation(_EvaluationModel):
    """Reproducible transformation lineage for one derived claim."""

    schema_version: Literal["deterministic-derivation:v1"] = (
        "deterministic-derivation:v1"
    )
    claim_id: UUID
    boundary: AttemptProjectionBoundary
    transformation_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    transformation_version: str = Field(pattern=_VERSION)
    transformation_digest: str = Field(pattern=_DIGEST)
    execution_reference: UUID
    inputs: tuple[DerivationInput, ...] = Field(min_length=1)

    @field_validator("claim_id", "execution_reference")
    @classmethod
    def validate_id(cls, value: UUID, info: Any) -> UUID:
        return _require_id(value, info.field_name)

    @model_validator(mode="after")
    def validate_inputs(self) -> "DeterministicDerivation":
        citation_ids = tuple(item.citation_id for item in self.inputs)
        if len(set(citation_ids)) != len(citation_ids):
            raise ValueError("derivation inputs must be unique")
        return self


class CoverageEvaluationStatus(str, Enum):
    COMPLETE = "complete"
    INSUFFICIENT = "insufficient"
    UNPROVEN = "unproven"


class AnswerDisclosureStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class AnswerPartialityStatus(str, Enum):
    EXHAUSTIVE = "exhaustive"
    PARTIAL = "partial"
    NONE = "none"


class GroundedConflictStatus(str, Enum):
    NONE = "none"
    PRESENTED = "presented"
    RESOLVED_BY_PRECEDENCE = "resolved_by_precedence"


class AnswerEvaluationSafeReason(str, Enum):
    COMPLETE = "complete"
    PARTIAL_AUTHORIZED = "partial_authorized"
    CLAIM_REJECTED = "claim_rejected"
    INCOMPLETE_AUTHORIZED_COVERAGE = "incomplete_authorized_coverage"
    COVERAGE_NOT_PROVEN = "coverage_not_proven"
    GROUNDED_CONFLICT = "grounded_conflict"
    PRECEDENCE_APPLIED = "precedence_applied"


class AnswerEvaluationDecision(_EvaluationModel):
    """Disclosure-safe decision; protected source diagnostics stay in inputs."""

    schema_version: Literal["answer-evaluation-decision:v2"] = (
        "answer-evaluation-decision:v2"
    )
    boundary: AttemptProjectionBoundary
    target_scope_id: UUID
    observed_coverage_id: UUID
    evaluation_lineage_reference: UUID
    disclosure_status: AnswerDisclosureStatus
    coverage_status: CoverageEvaluationStatus
    partiality_status: AnswerPartialityStatus
    conflict_status: GroundedConflictStatus
    safe_reason: AnswerEvaluationSafeReason
    disclosable_claim_ids: tuple[UUID, ...] = ()

    @field_validator(
        "target_scope_id", "observed_coverage_id", "evaluation_lineage_reference"
    )
    @classmethod
    def validate_id(cls, value: UUID, info: Any) -> UUID:
        return _require_id(value, info.field_name)

    @field_validator("disclosable_claim_ids")
    @classmethod
    def validate_claim_ids(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        return _unique_ids(value, "disclosable_claim_ids")

    @model_validator(mode="after")
    def validate_decision(self) -> "AnswerEvaluationDecision":
        if self.disclosure_status is AnswerDisclosureStatus.REJECTED:
            if (
                self.disclosable_claim_ids
                or self.partiality_status is not AnswerPartialityStatus.NONE
                or self.conflict_status is not GroundedConflictStatus.NONE
            ):
                raise ValueError("rejected evaluation cannot disclose claims")
            return self
        if not self.disclosable_claim_ids:
            raise ValueError("accepted evaluation requires disclosable claims")
        expected_partiality = (
            AnswerPartialityStatus.EXHAUSTIVE
            if self.coverage_status is CoverageEvaluationStatus.COMPLETE
            else AnswerPartialityStatus.PARTIAL
        )
        if self.partiality_status is not expected_partiality:
            raise ValueError("answer partiality does not match coverage")
        if self.coverage_status is CoverageEvaluationStatus.UNPROVEN:
            raise ValueError("unproven coverage cannot be disclosed")
        return self


__all__ = [
    "ApprovedPrecedenceRule",
    "AnswerEvaluationDecision",
    "AnswerEvaluationSafeReason",
    "AnswerDisclosureStatus",
    "AnswerPartialityStatus",
    "AttemptProjectionBoundary",
    "CoverageEvaluationStatus",
    "CoverageMemberStatus",
    "ClaimAuthorizationStatus",
    "CanonicalViewMember",
    "ClaimEvidenceValidation",
    "ClaimGroundingStatus",
    "DerivationInput",
    "DeterministicDerivation",
    "GroundedConflict",
    "GroundedConflictStatus",
    "GroundedClaimAssessment",
    "MissingCoverageImpact",
    "ObservedCoverage",
    "ObservedCoverageMember",
    "PartialFallbackAuthorization",
    "PartialFallbackAuthority",
    "PartialClaimUtility",
    "TargetCompletenessScope",
    "TargetScopeMember",
]
