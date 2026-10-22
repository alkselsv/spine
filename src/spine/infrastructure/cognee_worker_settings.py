"""Strict environment binding for the approved Cognee projection profile."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from spine.domain.common import DefinitionModel
from spine.memory.cognee.worker_profile import (
    CogneeWorkerProfile,
    ProfileApproval,
    ProfileConfigurationStatus,
    RuntimeIdentity,
    profile_configuration_status,
    runtime_identity_matches_profile,
)


class CogneeWorkerBindingConfiguration(DefinitionModel):
    """Non-secret deployment/profile identities consumed by the worker root."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["cognee-projection-binding:v1"]
    profile: CogneeWorkerProfile
    approval: ProfileApproval
    runtime_identity: RuntimeIdentity
    deployment_id: str = Field(min_length=1, max_length=128)
    release_id: str = Field(min_length=1, max_length=128)


class CogneeWorkerConfigurationError(RuntimeError):
    """Bounded startup/configuration failure without rejected input values."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class CogneeWorkerBindingSettings(BaseSettings):
    """Load one strict versioned binding document from worker configuration."""

    model_config = SettingsConfigDict(
        extra="forbid",
        frozen=True,
        env_file=None,
        case_sensitive=False,
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    binding_json: str | None = Field(
        default=None,
        repr=False,
        validation_alias="SPINE_COGNEE_WORKER_BINDING_JSON",
    )

    def load_configuration(self) -> CogneeWorkerBindingConfiguration:
        if self.binding_json is None:
            raise CogneeWorkerConfigurationError("profile_binding_missing")
        try:
            configuration = CogneeWorkerBindingConfiguration.model_validate_json(
                self.binding_json
            )
        except (ValidationError, ValueError):
            raise CogneeWorkerConfigurationError("profile_binding_invalid") from None

        status = profile_configuration_status(
            profile=configuration.profile,
            approval=configuration.approval,
            deployment_id=configuration.deployment_id,
            release_id=configuration.release_id,
        )
        if status is not ProfileConfigurationStatus.VALID:
            raise CogneeWorkerConfigurationError(status.value)
        if not runtime_identity_matches_profile(
            configuration.profile,
            configuration.runtime_identity,
        ):
            raise CogneeWorkerConfigurationError("runtime_identity_mismatch")
        return configuration


__all__ = [
    "CogneeWorkerBindingConfiguration",
    "CogneeWorkerBindingSettings",
    "CogneeWorkerConfigurationError",
]
