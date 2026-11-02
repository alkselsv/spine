"""Pure contracts for live R2 deployment qualification evidence."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from spine.domain.common import DefinitionModel, EnvironmentKind, VersionIdentifier
from spine.domain.evaluations.artifact_attestation import CommitSha, Sha256Digest


R2Status = Literal["pass", "fail", "blocked", "not_verified"]
R2ComponentRole = Literal[
    "api",
    "web",
    "qa_worker",
    "outbox_dispatcher",
    "postgresql",
    "temporal",
    "original_storage",
    "selected_provider",
]


class R2ComponentObservation(DefinitionModel):
    """One metadata-only observation from the exact deployed composition."""

    role: R2ComponentRole
    component_id: VersionIdentifier
    version: str = Field(min_length=1, max_length=128)
    artifact_digest: Sha256Digest
    observation_digest: Sha256Digest
    deployment_fingerprint: Sha256Digest
    dependencies: tuple[R2ComponentRole, ...]
    configured: bool
    reachable: bool
    fallback_selected: bool


class R2LiveExecution(DefinitionModel):
    """Attributable execution that produced the live report."""

    execution_id: VersionIdentifier
    principal_id: VersionIdentifier
    environment: EnvironmentKind
    started_at: datetime
    completed_at: datetime

    @model_validator(mode="after")
    def validate_execution_window(self) -> "R2LiveExecution":
        if self.started_at.tzinfo is None or self.completed_at.tzinfo is None:
            raise ValueError("live execution timestamps must include a timezone")
        if self.completed_at < self.started_at:
            raise ValueError("live execution completion cannot precede its start")
        return self


class R2ReadinessObservation(DefinitionModel):
    """Scoped readiness result, kept separate from the application canary."""

    status: Literal["passed", "failed", "unavailable"]
    authenticated: bool
    scope_verified: bool
    authority_mutations: int = Field(ge=0)
    provider_calls: int = Field(ge=0)


class R2StartupObservation(DefinitionModel):
    """Observed startup side effects before readiness or canary execution."""

    status: Literal["passed", "failed", "unavailable"]
    authority_mutations: int = Field(ge=0)
    provider_calls: int = Field(ge=0)


class R2CanaryObservation(DefinitionModel):
    """Authenticated bounded application canary result."""

    status: Literal["passed", "failed", "not_run"]
    authenticated: bool
    scope_verified: bool


class R2PrivacyDeclaration(DefinitionModel):
    """Explicit export-safety facts for the committed evidence artifact."""

    metadata_only: bool
    credentials_recorded: bool
    network_locations_recorded: bool
    protected_content_recorded: bool
    provider_payloads_recorded: bool
    raw_errors_recorded: bool


class R2CollectionFailureEvidence(DefinitionModel):
    """Metadata-only record emitted when the trusted live collector refuses."""

    schema_version: Literal["r2-deployment-collection-failure:v1"] = (
        "r2-deployment-collection-failure:v1"
    )
    boundary: Literal["live"] = "live"
    collector_version: Literal["r2-compose-collector:v1"] = (
        "r2-compose-collector:v1"
    )
    target_commit: CommitSha
    deployment_fingerprint: Sha256Digest
    environment: EnvironmentKind
    captured_at: datetime
    live_execution: R2LiveExecution
    reason_code: VersionIdentifier
    privacy: R2PrivacyDeclaration

    @model_validator(mode="after")
    def validate_failure_window(self) -> "R2CollectionFailureEvidence":
        if self.captured_at.tzinfo is None:
            raise ValueError("failure capture time must include a timezone")
        if self.live_execution.completed_at > self.captured_at:
            raise ValueError("live execution cannot complete after evidence capture")
        if self.live_execution.environment is not self.environment:
            raise ValueError("live execution environment must match evidence")
        return self


class R2DeploymentEvidence(DefinitionModel):
    """Canonical metadata-only artifact emitted by a live R2 run."""

    schema_version: Literal["r2-deployment-evidence:v1"] = (
        "r2-deployment-evidence:v1"
    )
    boundary: Literal["live"] = "live"
    collector_version: Literal["r2-compose-collector:v1"] = (
        "r2-compose-collector:v1"
    )
    target_commit: CommitSha
    deployment_fingerprint: Sha256Digest
    environment: EnvironmentKind
    captured_at: datetime
    live_execution: R2LiveExecution | None
    components: tuple[R2ComponentObservation, ...]
    composition_digest: Sha256Digest
    startup: R2StartupObservation
    readiness: R2ReadinessObservation
    canary: R2CanaryObservation
    privacy: R2PrivacyDeclaration

    @model_validator(mode="after")
    def validate_observation_identity(self) -> "R2DeploymentEvidence":
        if self.captured_at.tzinfo is None:
            raise ValueError("evidence capture time must include a timezone")
        roles = [item.role for item in self.components]
        if len(roles) != len(set(roles)):
            raise ValueError("component roles must be unique")
        if (
            self.live_execution is not None
            and self.live_execution.completed_at > self.captured_at
        ):
            raise ValueError("live execution cannot complete after evidence capture")
        if (
            self.live_execution is not None
            and self.live_execution.environment is not self.environment
        ):
            raise ValueError("live execution environment must match evidence")
        return self


class R2DeploymentQualificationResult(DefinitionModel):
    """Fail-closed independent decision for criterion R2."""

    schema_version: Literal["r2-deployment-qualification-result:v1"] = (
        "r2-deployment-qualification-result:v1"
    )
    status: R2Status
    reason_codes: tuple[VersionIdentifier, ...]
    artifact_digest: Sha256Digest
    head_sha: CommitSha
    deployment_fingerprint: Sha256Digest
