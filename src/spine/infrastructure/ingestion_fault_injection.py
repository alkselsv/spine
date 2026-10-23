"""Bounded, qualification-only failures for ingestion lifecycle exercises.

The profile contains no payload data. Each rule is armed for one isolated
workspace/environment and one synthetic command identity; consuming a rule is
process-local and one-shot.
"""

from __future__ import annotations

from threading import Lock
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from spine.application.ingestion_faults import (
    IngestionFaultBoundary,
    IngestionFaultInjected,
    IngestionFaultReason,
)
from spine.domain.common import DefinitionModel, VersionIdentifier


_REASONS_BY_BOUNDARY = {
    IngestionFaultBoundary.PARSER: IngestionFaultReason.PARSER_RETRYABLE,
    IngestionFaultBoundary.ORIGINAL_STORAGE: IngestionFaultReason.ORIGINAL_STORAGE_UNAVAILABLE,
    IngestionFaultBoundary.PRE_ACCEPTANCE: IngestionFaultReason.PRE_ACCEPTANCE_RETRYABLE,
}


class IngestionFaultRule(DefinitionModel):
    """One deterministic fault, scoped to a synthetic ingestion command."""

    workspace_id: UUID
    environment_id: UUID
    command_identity: str = Field(
        pattern=(
            r"^api\.document\.(?:upload\.[a-f0-9]{32}|"
            r"batch-upload\.[a-f0-9]{24}\.item-[1-9][0-9]*)$"
        )
    )
    boundary: IngestionFaultBoundary
    reason_code: IngestionFaultReason

    @model_validator(mode="after")
    def validate_rule(self) -> "IngestionFaultRule":
        if self.workspace_id.int == 0 or self.environment_id.int == 0:
            raise ValueError("qualification fault scope must be non-zero")
        if _REASONS_BY_BOUNDARY[self.boundary] is not self.reason_code:
            raise ValueError("fault reason does not match boundary")
        return self


class IngestionFaultInjectionProfile(DefinitionModel):
    """Versioned fault set bound to one isolated deployment profile."""

    schema_version: Literal["spine-ingestion-fault-profile:v1"] = "spine-ingestion-fault-profile:v1"
    profile_id: str = Field(pattern=r"^qualification-[a-z0-9][a-z0-9_.:-]{0,63}$")
    profile_version: VersionIdentifier
    deployment_profile_id: str = Field(
        pattern=r"^qualification-[a-z0-9][a-z0-9_.:-]{0,63}$"
    )
    deployment_profile_version: VersionIdentifier
    deployment_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    rules: tuple[IngestionFaultRule, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_rules(self) -> "IngestionFaultInjectionProfile":
        identities = tuple(
            (rule.workspace_id, rule.environment_id, rule.command_identity)
            for rule in self.rules
        )
        if len(set(identities)) != len(identities):
            raise ValueError("qualification fault command identities must be unique")
        return self


class IngestionFaultController:
    """Consume the configured fault once after exact scope and identity match."""

    def __init__(self, profile: IngestionFaultInjectionProfile | None) -> None:
        self._profile = profile
        self._consumed: set[int] = set()
        self._lock = Lock()

    @property
    def enabled(self) -> bool:
        return self._profile is not None

    def assert_deployment_binding(
        self,
        *,
        profile_id: str,
        profile_version: str,
        deployment_fingerprint: str,
    ) -> None:
        profile = self._profile
        if profile is None:
            return
        if (
            profile.deployment_profile_id != profile_id
            or profile.deployment_profile_version != profile_version
            or profile.deployment_fingerprint != deployment_fingerprint
        ):
            raise ValueError("INGESTION_FAULT_PROFILE_MISMATCH")

    def inject_if_bound(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        command_identity: str,
        boundary: IngestionFaultBoundary,
    ) -> None:
        profile = self._profile
        if profile is None:
            return
        matching = next(
            (
                (index, rule)
                for index, rule in enumerate(profile.rules)
                if rule.workspace_id == workspace_id
                and rule.environment_id == environment_id
                and rule.command_identity == command_identity
                and rule.boundary is boundary
            ),
            None,
        )
        if matching is None:
            return
        index, rule = matching
        with self._lock:
            if index in self._consumed:
                return
            self._consumed.add(index)
        raise IngestionFaultInjected(rule.reason_code)


class IngestionFaultInjectionSettings(BaseSettings):
    """Load the opt-in qualification profile; absence keeps faults disabled."""

    model_config = SettingsConfigDict(
        extra="forbid",
        frozen=True,
        env_file=None,
        case_sensitive=False,
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    profile_json: str | None = Field(
        default=None,
        min_length=2,
        repr=False,
        validation_alias="SPINE_INGESTION_FAULT_PROFILE_JSON",
    )

    def load_profile(self) -> IngestionFaultInjectionProfile | None:
        if self.profile_json is None:
            return None
        try:
            return IngestionFaultInjectionProfile.model_validate_json(self.profile_json)
        except ValidationError:
            raise ValueError("INGESTION_FAULT_PROFILE_INVALID") from None


__all__ = [
    "IngestionFaultBoundary",
    "IngestionFaultController",
    "IngestionFaultInjected",
    "IngestionFaultInjectionProfile",
    "IngestionFaultRule",
    "IngestionFaultReason",
    "IngestionFaultInjectionSettings",
]
