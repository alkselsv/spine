"""Pure contracts for repository-bound R1 qualification evidence."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from spine.domain.common import DefinitionModel, VersionIdentifier
from spine.domain.evaluations.artifact_attestation import (
    ArtifactAttestation,
    CommitSha,
    Sha256Digest,
)



class MigrationTargetReceipt(DefinitionModel):
    """Safe result for one separately provisioned migration target."""

    schema_version: Literal["r1-migration-target-receipt:v1"] = (
        "r1-migration-target-receipt:v1"
    )
    role: Literal["clean", "upgrade"]
    target_fingerprint: Sha256Digest
    postgresql_major: Literal[17]
    head_revision: VersionIdentifier
    retained_revision_count: int = Field(ge=1)
    tested_revision_count: int = Field(ge=0)
    status: Literal["passed", "failed"]


class SkipDisposition(DefinitionModel):
    """Versioned applicability decision for one skipped repository test."""

    test_id: str = Field(min_length=1, max_length=512)
    reported_reason: str = Field(min_length=1, max_length=1024)
    reason_code: VersionIdentifier
    applicability: Literal["optional", "required", "unclassified"]
    policy_version: VersionIdentifier


class R1SkipPolicyEntry(DefinitionModel):
    """Approved applicability for one exact repository test identifier."""

    test_id: str = Field(min_length=1, max_length=512)
    expected_reason: str = Field(min_length=1, max_length=1024)
    reason_code: VersionIdentifier
    applicability: Literal["optional", "required"]


class R1SkipPolicy(DefinitionModel):
    """Versioned allowlist used to classify every observed repository skip."""

    schema_version: Literal["r1-repository-skip-policy:v1"] = (
        "r1-repository-skip-policy:v1"
    )
    policy_version: VersionIdentifier
    entries: tuple[R1SkipPolicyEntry, ...]

    @model_validator(mode="after")
    def validate_unique_test_ids(self) -> "R1SkipPolicy":
        identifiers = [item.test_id for item in self.entries]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("skip policy test identifiers must be unique")
        return self


class RepositoryTestReceipt(DefinitionModel):
    """Content-free aggregate and complete skip disposition for pytest."""

    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    errors: int = Field(ge=0)
    skipped: int = Field(ge=0)
    skip_dispositions: tuple[SkipDisposition, ...]

    @model_validator(mode="after")
    def validate_skip_coverage(self) -> "RepositoryTestReceipt":
        if self.skipped != len(self.skip_dispositions):
            raise ValueError("every repository skip requires one disposition")
        identifiers = [item.test_id for item in self.skip_dispositions]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("repository skip identifiers must be unique")
        return self


class R1WorkflowExecution(DefinitionModel):
    """Attributable GitHub Actions execution that collected the evidence."""

    workflow_name: Literal["test"]
    run_id: str = Field(pattern=r"^[1-9][0-9]*$")
    job_id: Literal["r1-regression-gate"]
    run_attempt: int = Field(ge=1)
    started_at: datetime
    completed_at: datetime

    @model_validator(mode="after")
    def validate_time_order(self) -> "R1WorkflowExecution":
        if self.completed_at < self.started_at:
            raise ValueError("workflow completion cannot precede its start")
        return self


class R1ExecutedCommand(DefinitionModel):
    """Safe, exact command identity included in the signed artifact."""

    boundary: Literal["clean_target", "upgrade_target", "repository_tests"]
    argv: tuple[str, ...] = Field(min_length=1)


class R1ComponentVersion(DefinitionModel):
    """Tool or component version used by the evidence collector."""

    component: VersionIdentifier
    version: str = Field(min_length=1, max_length=128)


class R1RepositoryEvidence(DefinitionModel):
    """Canonical metadata-only artifact emitted by the R1 repository gate."""

    schema_version: Literal["r1-repository-evidence:v1"] = (
        "r1-repository-evidence:v1"
    )
    head_sha: CommitSha
    workflow_execution: R1WorkflowExecution
    commands: tuple[R1ExecutedCommand, ...]
    component_versions: tuple[R1ComponentVersion, ...]
    skip_policy_version: VersionIdentifier
    skip_policy_digest: Sha256Digest
    migration_targets: tuple[MigrationTargetReceipt, MigrationTargetReceipt]
    repository_tests: RepositoryTestReceipt

    @model_validator(mode="after")
    def validate_target_roles(self) -> "R1RepositoryEvidence":
        if {item.role for item in self.migration_targets} != {"clean", "upgrade"}:
            raise ValueError("clean and upgrade migration target receipts are required")
        if {item.boundary for item in self.commands} != {
            "clean_target",
            "upgrade_target",
            "repository_tests",
        }:
            raise ValueError("all R1 execution commands are required")
        return self


class R1RepositoryQualificationResult(DefinitionModel):
    """Fail-closed result for the repository and migration portion of R1."""

    schema_version: Literal["r1-repository-qualification-result:v1"] = (
        "r1-repository-qualification-result:v1"
    )
    status: Literal["pass", "fail"]
    reason_codes: tuple[VersionIdentifier, ...]
    artifact_digest: Sha256Digest
    head_sha: CommitSha
