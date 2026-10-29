"""Typed contract for proposal requirements extraction.

The contract is deliberately independent from an agent framework, provider or
workflow engine.  It carries only the normalized request and opaque,
server-authorized source identities; source content is resolved by the owning
workflow/context boundary.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from spine.domain.common import EnvironmentKind, ValidatedDefinitionModel
from spine.domain.workflows import AcceptedSourceRevisionRef, ClientRequestV1, Requirement


EXTRACT_REQUIREMENTS_INPUT_SCHEMA = "extract-requirements.input:v1"
EXTRACT_REQUIREMENTS_OUTPUT_SCHEMA = "extract-requirements.output:v1"
class RequirementsExtractionOutcome(str, Enum):
    """Closed outcomes returned by the requirements capability."""

    SUCCESS = "success"
    PARTIAL = "partial"
    ABSTAINED = "abstain"
    FAILED = "failed"


class RequirementsExtractionReasonCode(str, Enum):
    """Disclosure-safe reasons for a non-successful extraction."""

    INSUFFICIENT_INFORMATION = "insufficient_information"
    NO_USABLE_SOURCE = "no_usable_source"
    AUTHORIZATION_DENIED = "authorization_denied"
    INVALID_INPUT = "invalid_input"
    PROCESSING_FAILED = "processing_failed"


class RequirementsExtractionReason(ValidatedDefinitionModel):
    """A typed public reason, without provider or prompt diagnostics."""

    code: RequirementsExtractionReasonCode


class RequirementsClarification(ValidatedDefinitionModel):
    """One bounded clarification prompt produced by extraction."""

    clarification_id: UUID
    prompt: str = Field(min_length=1, max_length=4_096)
    missing_markers: tuple[str, ...] = ()

    @field_validator("clarification_id")
    @classmethod
    def reject_zero_id(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("clarification identity must be non-zero")
        return value

    @field_validator("prompt")
    @classmethod
    def reject_blank_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("clarification prompt must not be blank")
        return value


class RequirementsExecutionConfiguration(ValidatedDefinitionModel):
    """Bounded, caller-independent execution controls for one capability run."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    max_requirements: int = Field(default=32, ge=1, le=128)
    max_clarifications: int = Field(default=8, ge=0, le=32)

    def runtime_constraints(self) -> dict[str, int]:
        return {
            "max_requirements": self.max_requirements,
            "max_clarifications": self.max_clarifications,
        }


class ExtractRequirementsInput(ValidatedDefinitionModel):
    """Versioned input accepted by ``extract_requirements``."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    schema_version: Literal[EXTRACT_REQUIREMENTS_INPUT_SCHEMA] = EXTRACT_REQUIREMENTS_INPUT_SCHEMA
    capability: Literal["extract_requirements"] = "extract_requirements"
    workspace_id: UUID
    environment_id: UUID
    environment: EnvironmentKind
    workflow_run_id: UUID
    work_item_id: UUID
    client_request: ClientRequestV1
    authorized_source_refs: tuple[AcceptedSourceRevisionRef, ...] = ()
    execution: RequirementsExecutionConfiguration = RequirementsExecutionConfiguration()

    @field_validator(
        "workspace_id", "environment_id", "workflow_run_id", "work_item_id"
    )
    @classmethod
    def reject_zero_ids(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("requirements extraction identities must be non-zero")
        return value

    @model_validator(mode="after")
    def validate_authorized_sources(self) -> ExtractRequirementsInput:
        accepted = {
            (item.source_object_id, item.source_revision_id, item.provenance_id)
            for item in self.client_request.accepted_sources
        }
        authorized = tuple(
            (item.source_object_id, item.source_revision_id, item.provenance_id)
            for item in self.authorized_source_refs
        )
        if len(set(authorized)) != len(authorized):
            raise ValueError("authorized source references must be unique")
        if not set(authorized).issubset(accepted):
            raise ValueError("authorized sources must belong to the client request")
        return self


class RequirementsExtractionOutput(ValidatedDefinitionModel):
    """Versioned extraction result with explicit outcome semantics."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    schema_version: Literal[EXTRACT_REQUIREMENTS_OUTPUT_SCHEMA] = EXTRACT_REQUIREMENTS_OUTPUT_SCHEMA
    capability: Literal["extract_requirements"] = "extract_requirements"
    workspace_id: UUID
    environment_id: UUID
    workflow_run_id: UUID
    work_item_id: UUID
    outcome: RequirementsExtractionOutcome
    requirements: tuple[Requirement, ...] = ()
    clarifications: tuple[RequirementsClarification, ...] = ()
    reason: RequirementsExtractionReason | None = None

    @field_validator(
        "workspace_id", "environment_id", "workflow_run_id", "work_item_id"
    )
    @classmethod
    def reject_zero_ids(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("requirements extraction identities must be non-zero")
        return value

    @model_validator(mode="after")
    def validate_outcome_payload(self) -> RequirementsExtractionOutput:
        if len({item.requirement_id for item in self.requirements}) != len(self.requirements):
            raise ValueError("extracted requirement identities must be unique")
        if len({item.clarification_id for item in self.clarifications}) != len(self.clarifications):
            raise ValueError("clarification identities must be unique")

        if self.outcome is RequirementsExtractionOutcome.SUCCESS:
            if not self.requirements or self.clarifications or self.reason is not None:
                raise ValueError("successful extraction requires requirements only")
        elif self.outcome is RequirementsExtractionOutcome.PARTIAL:
            if not self.requirements and not self.clarifications:
                raise ValueError("partial extraction requires a candidate or clarification")
            if self.reason is not None:
                raise ValueError("partial extraction does not carry a failure reason")
        elif self.outcome is RequirementsExtractionOutcome.ABSTAINED:
            if self.requirements or self.clarifications or self.reason is None:
                raise ValueError("abstained extraction requires one reason and no candidate")
            if self.reason.code not in {
                RequirementsExtractionReasonCode.INSUFFICIENT_INFORMATION,
                RequirementsExtractionReasonCode.NO_USABLE_SOURCE,
                RequirementsExtractionReasonCode.AUTHORIZATION_DENIED,
            }:
                raise ValueError("abstention reason is incompatible")
        elif self.outcome is RequirementsExtractionOutcome.FAILED:
            if self.requirements or self.clarifications or self.reason is None:
                raise ValueError("failed extraction requires one reason and no candidate")
            if self.reason.code not in {
                RequirementsExtractionReasonCode.INVALID_INPUT,
                RequirementsExtractionReasonCode.PROCESSING_FAILED,
            }:
                raise ValueError("failure reason is incompatible")
        return self


# Descriptive aliases used by composition roots that name the boundary by its
# capability rather than its implementation detail.
ExtractRequirementsResult = RequirementsExtractionOutput
ExtractRequirementsOutcome = RequirementsExtractionOutcome


__all__ = [
    "EXTRACT_REQUIREMENTS_INPUT_SCHEMA",
    "EXTRACT_REQUIREMENTS_OUTPUT_SCHEMA",
    "ExtractRequirementsInput",
    "ExtractRequirementsOutcome",
    "ExtractRequirementsResult",
    "RequirementsClarification",
    "RequirementsExecutionConfiguration",
    "RequirementsExtractionOutcome",
    "RequirementsExtractionOutput",
    "RequirementsExtractionReason",
    "RequirementsExtractionReasonCode",
]
