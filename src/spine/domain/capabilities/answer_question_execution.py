"""Immutable server-resolved identities for one ``answer_question`` execution."""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from spine.domain.common import (
    EnvironmentKind,
    SchemaRef,
    ValidatedDefinitionModel,
    VersionIdentifier,
)
from spine.domain.context_profiles import ContextProfileRef

if TYPE_CHECKING:
    from spine.domain.agents.models import AgentVersion


ANSWER_QUESTION_EXECUTION_PIN_SCHEMA = "answer-question-execution-pin:v1"
_DIGEST = r"^[0-9a-f]{64}$"


def _require_id(value: UUID, field_name: str) -> UUID:
    if value.int == 0:
        raise ValueError(f"{field_name} must be a non-zero UUID")
    return value


def _json_default(value: Any) -> str:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"unsupported pin value: {type(value).__name__}")


def agent_version_digest(version: AgentVersion) -> str:
    """Return the canonical identity digest without exposing version internals."""

    encoded = json.dumps(
        {
            "schema": "agent-version-identity:v1",
            "payload": version.model_dump(mode="json"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class _PinModel(ValidatedDefinitionModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )


class _ScopedConfigurationIdentity(_PinModel):
    workspace_id: UUID
    environment_id: UUID
    configuration_generation: int = Field(ge=1)

    @field_validator("workspace_id", "environment_id")
    @classmethod
    def validate_scope_id(cls, value: UUID, info: Any) -> UUID:
        return _require_id(value, info.field_name)


class PinnedAgentVersion(_ScopedConfigurationIdentity):
    """Safe immutable identity of the selected Agent Version."""

    agent_version_id: UUID
    agent_id: UUID
    version: VersionIdentifier
    digest: str = Field(pattern=_DIGEST)

    @field_validator("agent_version_id", "agent_id")
    @classmethod
    def validate_agent_id(cls, value: UUID, info: Any) -> UUID:
        return _require_id(value, info.field_name)


class PinnedConfigurationVersion(_ScopedConfigurationIdentity):
    """Immutable prompt, model-profile, or policy identity."""

    identity_id: UUID
    binding_key: VersionIdentifier
    version: VersionIdentifier
    digest: str = Field(pattern=_DIGEST)

    @field_validator("identity_id")
    @classmethod
    def validate_identity_id(cls, value: UUID) -> UUID:
        return _require_id(value, "identity_id")


class PinnedContextProfile(_ScopedConfigurationIdentity):
    """Scoped Context Profile identity bound by an Agent Version key."""

    binding_key: VersionIdentifier
    profile: ContextProfileRef


class PinnedProjection(_ScopedConfigurationIdentity):
    """Exact active logical projection used by one Q&A attempt."""

    projection_snapshot_id: UUID
    snapshot_manifest_digest: str = Field(pattern=_DIGEST)
    projection_activation_id: UUID
    activation_generation: int = Field(ge=1)
    projection_config_version_id: UUID
    projection_config_digest: str = Field(pattern=_DIGEST)
    canonical_boundary_id: UUID
    canonical_boundary_digest: str = Field(pattern=_DIGEST)

    @field_validator(
        "projection_snapshot_id",
        "projection_activation_id",
        "projection_config_version_id",
        "canonical_boundary_id",
    )
    @classmethod
    def validate_projection_id(cls, value: UUID, info: Any) -> UUID:
        return _require_id(value, info.field_name)


class TrustedAnswerQuestionConfiguration(_PinModel):
    """One catalog-owned and Agent-Version-bound Q&A configuration generation."""

    configuration_generation: int = Field(ge=1)
    capability_id: UUID
    input_schema: SchemaRef
    output_schema: SchemaRef
    agent_version: PinnedAgentVersion
    prompt_version: PinnedConfigurationVersion
    model_profile: PinnedConfigurationVersion
    context_profile: PinnedContextProfile
    projection: PinnedProjection
    semantic_support_policy: PinnedConfigurationVersion
    approved: bool

    @property
    def identities(self) -> tuple[_ScopedConfigurationIdentity, ...]:
        return (
            self.agent_version,
            self.prompt_version,
            self.model_profile,
            self.context_profile,
            self.projection,
            self.semantic_support_policy,
        )

    @field_validator("capability_id")
    @classmethod
    def validate_capability_id(cls, value: UUID) -> UUID:
        return _require_id(value, "capability_id")

    @model_validator(mode="after")
    def validate_single_scope_and_generation(self) -> TrustedAnswerQuestionConfiguration:
        scopes = {(item.workspace_id, item.environment_id) for item in self.identities}
        generations = {item.configuration_generation for item in self.identities}
        if len(scopes) != 1:
            raise ValueError("Q&A configuration has a mixed workspace or environment")
        if generations != {self.configuration_generation}:
            raise ValueError("Q&A configuration generations are incompatible")
        return self


class AnswerQuestionExecutionPin(_PinModel):
    """Immutable configuration-lineage pin for one governed Q&A execution.

    Retrieval-owned ``as_of`` and Context Bundle lineage are attached later by
    their owning attempt boundary; they are deliberately not configuration
    selectors in this contract.
    """

    schema_version: Literal[ANSWER_QUESTION_EXECUTION_PIN_SCHEMA] = (
        ANSWER_QUESTION_EXECUTION_PIN_SCHEMA
    )
    workspace_id: UUID
    environment_id: UUID
    environment: EnvironmentKind
    configuration_generation: int = Field(ge=1)
    capability_id: UUID
    input_schema: SchemaRef
    output_schema: SchemaRef
    agent_version: PinnedAgentVersion
    prompt_version: PinnedConfigurationVersion
    model_profile: PinnedConfigurationVersion
    context_profile: PinnedContextProfile
    projection: PinnedProjection
    semantic_support_policy: PinnedConfigurationVersion
    pin_digest: str = Field(pattern=_DIGEST)

    @property
    def identities(self) -> tuple[_ScopedConfigurationIdentity, ...]:
        return (
            self.agent_version,
            self.prompt_version,
            self.model_profile,
            self.context_profile,
            self.projection,
            self.semantic_support_policy,
        )

    @classmethod
    def create(
        cls,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        environment: EnvironmentKind,
        configuration: TrustedAnswerQuestionConfiguration,
    ) -> AnswerQuestionExecutionPin:
        values = {
            "schema_version": ANSWER_QUESTION_EXECUTION_PIN_SCHEMA,
            "workspace_id": workspace_id,
            "environment_id": environment_id,
            "environment": environment,
            "configuration_generation": configuration.configuration_generation,
            "capability_id": configuration.capability_id,
            "input_schema": configuration.input_schema,
            "output_schema": configuration.output_schema,
            "agent_version": configuration.agent_version,
            "prompt_version": configuration.prompt_version,
            "model_profile": configuration.model_profile,
            "context_profile": configuration.context_profile,
            "projection": configuration.projection,
            "semantic_support_policy": configuration.semantic_support_policy,
        }
        values["pin_digest"] = cls._digest(values)
        return cls.model_validate(values)

    @staticmethod
    def _digest(values: dict[str, Any]) -> str:
        payload = {
            key: (
                value.model_dump(mode="json")
                if isinstance(value, ValidatedDefinitionModel)
                else value.value
                if isinstance(value, EnvironmentKind)
                else str(value)
                if isinstance(value, UUID)
                else value
            )
            for key, value in values.items()
            if key != "pin_digest"
        }
        encoded = json.dumps(
            {"schema": ANSWER_QUESTION_EXECUTION_PIN_SCHEMA, "payload": payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @model_validator(mode="after")
    def validate_pin(self) -> AnswerQuestionExecutionPin:
        _require_id(self.workspace_id, "workspace_id")
        _require_id(self.environment_id, "environment_id")
        _require_id(self.capability_id, "capability_id")
        if any(
            item.workspace_id != self.workspace_id
            or item.environment_id != self.environment_id
            for item in self.identities
        ):
            raise ValueError("Q&A execution pin has a mixed workspace or environment")
        if any(
            item.configuration_generation != self.configuration_generation
            for item in self.identities
        ):
            raise ValueError("Q&A execution pin has mixed configuration generations")
        values = self.model_dump(mode="python", exclude={"pin_digest"})
        if self.pin_digest != self._digest(values):
            raise ValueError("Q&A execution pin digest is invalid")
        return self


__all__ = [
    "ANSWER_QUESTION_EXECUTION_PIN_SCHEMA",
    "AnswerQuestionExecutionPin",
    "PinnedAgentVersion",
    "PinnedConfigurationVersion",
    "PinnedContextProfile",
    "PinnedProjection",
    "TrustedAnswerQuestionConfiguration",
    "agent_version_digest",
]
