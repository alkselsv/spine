"""Pure contracts for repository-bound R1 qualification evidence."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from spine.domain.common import DefinitionModel, VersionIdentifier

Sha256Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
CommitSha = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{40}$")]


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
    tested_revision_count: int = Field(ge=1)
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


class R1RepositoryEvidence(DefinitionModel):
    """Canonical metadata-only artifact emitted by the R1 repository gate."""

    schema_version: Literal["r1-repository-evidence:v1"] = (
        "r1-repository-evidence:v1"
    )
    head_sha: CommitSha
    skip_policy_version: VersionIdentifier
    skip_policy_digest: Sha256Digest
    migration_targets: tuple[MigrationTargetReceipt, MigrationTargetReceipt]
    repository_tests: RepositoryTestReceipt

    @model_validator(mode="after")
    def validate_target_roles(self) -> "R1RepositoryEvidence":
        if {item.role for item in self.migration_targets} != {"clean", "upgrade"}:
            raise ValueError("clean and upgrade migration target receipts are required")
        return self


class ArtifactAttestation(DefinitionModel):
    """Trusted identity returned by an attestation verifier adapter."""

    artifact_digest: Sha256Digest
    head_sha: CommitSha
    repository: Literal["alkselsv/spine"]
    workflow_ref: str = Field(min_length=1, max_length=512)
    run_invocation_uri: str = Field(
        pattern=r"^https://github\.com/alkselsv/spine/actions/runs/[1-9][0-9]*/attempts/[1-9][0-9]*$"
    )


class R1RepositoryQualificationResult(DefinitionModel):
    """Fail-closed result for the repository and migration portion of R1."""

    schema_version: Literal["r1-repository-qualification-result:v1"] = (
        "r1-repository-qualification-result:v1"
    )
    status: Literal["pass", "fail"]
    reason_codes: tuple[VersionIdentifier, ...]
    artifact_digest: Sha256Digest
    head_sha: CommitSha
