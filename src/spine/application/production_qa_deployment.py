"""Versioned, non-secret binding shared by deployable Q&A processes."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from spine.domain.common import DefinitionModel, VersionIdentifier


class ProductionQAAProfileMismatch(RuntimeError):
    """Safe startup failure for incompatible API, worker, or observer bindings."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class ProductionQAADeploymentProfile(DefinitionModel):
    """Immutable compatibility contract for the existing durable Ask graph.

    Values identify pre-provisioned deployment bindings. Credentials, URLs,
    request content, and protected identifiers do not belong in this profile.
    Process-specific credentials are deliberately held by adapter settings.
    """

    schema_version: Literal["spine-production-runtime-settings:v1"] = (
        "spine-production-runtime-settings:v1"
    )
    profile_id: VersionIdentifier
    profile_version: VersionIdentifier
    deployment_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    release_identity: VersionIdentifier
    commit_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    database_profile_id: VersionIdentifier
    migration_revision: VersionIdentifier
    authentication_profile_id: VersionIdentifier
    authentication_mode: Literal["oidc", "local_development"]
    scope_policy_id: VersionIdentifier
    temporal_cluster_profile_id: VersionIdentifier
    temporal_namespace: VersionIdentifier
    temporal_task_queue: VersionIdentifier
    workflow_contract_version: Literal["spine.qa.ask-admission:v1"]
    capture_service_profile_id: VersionIdentifier
    capture_worker_workload_id: VersionIdentifier
    capture_observer_workload_id: VersionIdentifier
    capture_contract_version: Literal["r1-qa-observer-capture:v1"]
    projection_profile_id: VersionIdentifier
    model_profile_id: VersionIdentifier

    @model_validator(mode="after")
    def validate_capture_workloads(self) -> "ProductionQAADeploymentProfile":
        if not self.capture_workload_ids_are_distinct:
            raise ValueError("capture workload identities must be distinct")
        return self

    @property
    def capture_workload_ids_are_distinct(self) -> bool:
        return self.capture_worker_workload_id != self.capture_observer_workload_id

    def assert_compatible_with(
        self, other: ProductionQAADeploymentProfile
    ) -> None:
        """Reject process drift with a bounded code that carries no profile values."""

        if type(other) is not ProductionQAADeploymentProfile:
            raise ProductionQAAProfileMismatch("PROFILE_MISMATCH")

        groups = (
            (
                "PROFILE_MISMATCH",
                (
                    "schema_version",
                    "profile_id",
                    "profile_version",
                    "deployment_fingerprint",
                    "release_identity",
                    "commit_sha",
                ),
            ),
            (
                "DATABASE_BINDING_MISMATCH",
                ("database_profile_id", "migration_revision"),
            ),
            (
                "AUTHORITY_BINDING_MISMATCH",
                ("authentication_profile_id", "authentication_mode", "scope_policy_id"),
            ),
            (
                "TEMPORAL_BINDING_MISMATCH",
                (
                    "temporal_cluster_profile_id",
                    "temporal_namespace",
                    "temporal_task_queue",
                    "workflow_contract_version",
                ),
            ),
            (
                "CAPTURE_BINDING_MISMATCH",
                (
                    "capture_service_profile_id",
                    "capture_worker_workload_id",
                    "capture_observer_workload_id",
                    "capture_contract_version",
                ),
            ),
            (
                "PROVIDER_BINDING_MISMATCH",
                ("projection_profile_id", "model_profile_id"),
            ),
        )
        for reason_code, fields in groups:
            if any(getattr(self, name) != getattr(other, name) for name in fields):
                raise ProductionQAAProfileMismatch(reason_code)


__all__ = ["ProductionQAADeploymentProfile", "ProductionQAAProfileMismatch"]
