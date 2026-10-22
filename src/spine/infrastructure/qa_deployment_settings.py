"""Environment binding for the shared, non-secret production Q&A profile."""

from __future__ import annotations

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from spine.application.production_qa_deployment import (
    ProductionQAADeploymentProfile,
    ProductionQAAProfileMismatch,
)


class ProductionQAADeploymentSettings(BaseSettings):
    """Load one versioned JSON profile document shared by API and worker."""

    model_config = SettingsConfigDict(
        extra="forbid",
        frozen=True,
        env_file=None,
        case_sensitive=False,
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    profile_json: str = Field(
        min_length=2,
        repr=False,
        validation_alias="SPINE_QA_DEPLOYMENT_SETTINGS_JSON",
    )

    def load_profile(self) -> ProductionQAADeploymentProfile:
        try:
            return ProductionQAADeploymentProfile.model_validate_json(self.profile_json)
        except ValidationError:
            raise ProductionQAAProfileMismatch("CONFIG_INVALID") from None


__all__ = ["ProductionQAADeploymentSettings"]
