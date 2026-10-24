"""Fail-closed qualification gate for composed projection publication."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from spine.domain.common import DefinitionModel

ProjectionPublicationStatus = Literal["pass", "blocked", "not_verified", "fail"]
ProjectionPublicationReason = Literal[
    "prerequisite_target_mismatch",
    "provider_compatibility_missing",
    "provider_compatibility_incompatible",
    "lifecycle_evidence_incomplete",
    "lifecycle_evidence_failed",
    "lifecycle_evidence_invalid",
]
ProjectionScenario = Literal[
    "canonical_uow_separation",
    "exact_revision_membership",
    "physical_validation_and_sealing",
    "activation_generation_cas",
    "failure_before_validation",
    "failure_before_activation",
    "stale_activation_cas",
    "changed_source_head",
    "tombstone_disclosure_fence",
    "rebuild_failure_preserves_active",
    "successor_rollback",
    "workspace_namespace_isolation",
    "safe_diagnostics",
]

_REQUIRED_SCENARIOS: tuple[ProjectionScenario, ...] = (
    "canonical_uow_separation",
    "exact_revision_membership",
    "physical_validation_and_sealing",
    "activation_generation_cas",
    "failure_before_validation",
    "failure_before_activation",
    "stale_activation_cas",
    "changed_source_head",
    "tombstone_disclosure_fence",
    "rebuild_failure_preserves_active",
    "successor_rollback",
    "workspace_namespace_isolation",
    "safe_diagnostics",
)
_REQUIRED_PHYSICAL_GATES = (
    "public_sdk",
    "structured_round_trip",
    "unsafe_evidence",
    "workspace_isolation",
    "namespace_isolation",
    "explicit_retrieval_context",
    "update_remove_tombstone",
    "rebuild_recovery_validation_diagnostics",
    "concurrency_failure_cleanup",
    "canonical_lifecycle_separation",
)


class ProjectionPublicationTarget(DefinitionModel):
    """Exact deployment and provider identity shared by Q16 evidence."""

    commit_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    deployment_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    workspace_id: UUID
    environment_id: UUID
    authorization_generation: int = Field(ge=1)
    provider_profile_id: str = Field(min_length=1, max_length=128)
    provider_profile_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class ProviderCompatibilityEvidence(DefinitionModel):
    """Attributable provider evidence for one exact qualification target."""

    schema_version: Literal["projection-provider-compatibility:v1"] = (
        "projection-provider-compatibility:v1"
    )
    source_run_id: str = Field(min_length=1, max_length=128)
    evidence_kind: Literal["existing_run_compatibility", "fresh_physical_run"]
    status: Literal["compatible", "incompatible", "not_verified"]
    target: ProjectionPublicationTarget
    approval_reference: str = Field(min_length=1, max_length=256)
    run_authorization_reference: str | None = Field(
        default=None, min_length=1, max_length=256
    )
    physical_report_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    passed_physical_gate_ids: tuple[str, ...] = ()
    production_approval: bool = False

    @model_validator(mode="after")
    def compatible_evidence_is_complete(self) -> "ProviderCompatibilityEvidence":
        complete = (
            self.run_authorization_reference is not None
            and self.physical_report_sha256 is not None
            and self.passed_physical_gate_ids == _REQUIRED_PHYSICAL_GATES
            and self.production_approval
        )
        if self.status == "compatible" and not complete:
            raise ValueError(
                "compatible evidence requires one complete pinned physical run"
            )
        return self


class ProjectionLifecycleObservation(DefinitionModel):
    """One bounded observation from the composed live R6 lifecycle."""

    scenario_id: ProjectionScenario
    status: Literal["passed", "failed", "not_verified"]
    evidence_references: tuple[str, ...] = Field(min_length=1)


class ProjectionPublicationGateResult(DefinitionModel):
    """Metadata-only decision for the Issue #562 R6 qualification."""

    schema_version: Literal["projection-publication-qualification:v1"] = (
        "projection-publication-qualification:v1"
    )
    status: ProjectionPublicationStatus
    target: ProjectionPublicationTarget
    reason_codes: tuple[ProjectionPublicationReason, ...]


class ProjectionPublicationQualificationReport(DefinitionModel):
    """Immutable evidence envelope whose decision is derived, never asserted."""

    schema_version: Literal["issue-562-projection-publication:v1"] = (
        "issue-562-projection-publication:v1"
    )
    ticket: Literal[562] = 562
    observed_at: datetime
    target: ProjectionPublicationTarget
    q16_01_target: ProjectionPublicationTarget
    r4_target: ProjectionPublicationTarget
    provider_compatibility: ProviderCompatibilityEvidence | None
    observations: tuple[ProjectionLifecycleObservation, ...]
    status: ProjectionPublicationStatus
    reason_codes: tuple[ProjectionPublicationReason, ...]

    @model_validator(mode="after")
    def decision_matches_evidence(self) -> "ProjectionPublicationQualificationReport":
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("qualification observation time must be timezone-aware")
        result = evaluate_projection_publication_gate(
            target=self.target,
            q16_01_target=self.q16_01_target,
            r4_target=self.r4_target,
            provider_compatibility=self.provider_compatibility,
            observations=self.observations,
        )
        if (self.status, self.reason_codes) != (
            result.status,
            result.reason_codes,
        ):
            raise ValueError("qualification decision does not match evidence")
        return self


def evaluate_projection_publication_gate(
    *,
    target: ProjectionPublicationTarget,
    q16_01_target: ProjectionPublicationTarget,
    r4_target: ProjectionPublicationTarget,
    provider_compatibility: ProviderCompatibilityEvidence | None,
    observations: tuple[ProjectionLifecycleObservation, ...],
) -> ProjectionPublicationGateResult:
    """Evaluate exact-target prerequisites before any lifecycle claim."""

    if q16_01_target != target or r4_target != target:
        return ProjectionPublicationGateResult(
            status="blocked",
            target=target,
            reason_codes=("prerequisite_target_mismatch",),
        )
    if provider_compatibility is None:
        return ProjectionPublicationGateResult(
            status="not_verified",
            target=target,
            reason_codes=("provider_compatibility_missing",),
        )
    if (
        provider_compatibility.status != "compatible"
        or provider_compatibility.target != target
    ):
        return ProjectionPublicationGateResult(
            status="not_verified",
            target=target,
            reason_codes=("provider_compatibility_incompatible",),
        )
    scenario_ids = tuple(item.scenario_id for item in observations)
    if len(scenario_ids) != len(set(scenario_ids)):
        return ProjectionPublicationGateResult(
            status="not_verified",
            target=target,
            reason_codes=("lifecycle_evidence_invalid",),
        )
    if any(item.status == "failed" for item in observations):
        return ProjectionPublicationGateResult(
            status="fail",
            target=target,
            reason_codes=("lifecycle_evidence_failed",),
        )
    if set(scenario_ids) != set(_REQUIRED_SCENARIOS):
        return ProjectionPublicationGateResult(
            status="not_verified",
            target=target,
            reason_codes=("lifecycle_evidence_incomplete",),
        )
    if any(item.status != "passed" for item in observations):
        return ProjectionPublicationGateResult(
            status="not_verified",
            target=target,
            reason_codes=("lifecycle_evidence_incomplete",),
        )
    return ProjectionPublicationGateResult(
        status="pass",
        target=target,
        reason_codes=(),
    )


def required_projection_publication_scenarios() -> tuple[ProjectionScenario, ...]:
    """Return the fixed Issue #562 live scenario inventory."""

    return _REQUIRED_SCENARIOS


__all__ = [
    "ProviderCompatibilityEvidence",
    "ProjectionLifecycleObservation",
    "ProjectionPublicationGateResult",
    "ProjectionPublicationQualificationReport",
    "ProjectionPublicationTarget",
    "evaluate_projection_publication_gate",
    "required_projection_publication_scenarios",
]
