"""Typed contract for proposal requirements extraction.

The contract is deliberately independent from an agent framework, provider or
workflow engine.  It carries only the normalized request and opaque,
server-authorized source identities; source content is resolved by the owning
workflow/context boundary.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from spine.domain.common import EnvironmentKind, ValidatedDefinitionModel
from spine.domain.workflows import (
    AcceptedSourceRevisionRef,
    ClientRequestV1,
    Requirement,
    RequirementEvidenceRef,
)


EXTRACT_REQUIREMENTS_INPUT_SCHEMA_V1 = "extract-requirements.input:v1"
EXTRACT_REQUIREMENTS_OUTPUT_SCHEMA_V1 = "extract-requirements.output:v1"
EXTRACT_REQUIREMENTS_INPUT_SCHEMA = "extract-requirements.input:v2"
EXTRACT_REQUIREMENTS_OUTPUT_SCHEMA = "extract-requirements.output:v2"


class TranscriptRecognitionQuality(str, Enum):
    """Versioned, provider-independent recognition assessment."""

    RELIABLE = "reliable"
    WEAK = "weak"
    UNKNOWN = "unknown"
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


class RequirementsEvidenceAvailability(str, Enum):
    """Current disclosure state of one evidence item at capability admission."""

    CURRENT = "current"
    DENIED = "denied"
    STALE = "stale"
    MISSING = "missing"
    FABRICATED = "fabricated"


class _RequirementsEvidence(ValidatedDefinitionModel):
    """Protected evidence content issued by an authorized server-side seam."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    availability: RequirementsEvidenceAvailability = RequirementsEvidenceAvailability.CURRENT
    reference: RequirementEvidenceRef
    text: str | None = Field(default=None, min_length=1, max_length=32_768)

    @model_validator(mode="after")
    def validate_disclosure(self) -> _RequirementsEvidence:
        if self.availability is RequirementsEvidenceAvailability.CURRENT:
            if self.text is None or not self.text.strip():
                raise ValueError("current requirements evidence requires protected text")
        elif self.text is not None:
            raise ValueError("unavailable requirements evidence cannot carry protected text")
        return self


class DocumentRequirementsEvidence(_RequirementsEvidence):
    """One authorized parsed-document segment and its exact stable locator."""

    kind: Literal["document"] = "document"

    @model_validator(mode="after")
    def validate_document_reference(self) -> DocumentRequirementsEvidence:
        if self.reference.parsed_artifact_ref is None:
            raise ValueError("document evidence requires a parsed-document reference")
        return self


class TranscriptRequirementsEvidence(_RequirementsEvidence):
    """One authorized transcript segment treated strictly as untrusted content."""

    kind: Literal["transcript"] = "transcript"
    language: str | None = Field(default=None, min_length=2, max_length=35)
    anonymous_speaker_label: str | None = Field(
        default=None, min_length=1, max_length=128
    )
    recognition_quality: TranscriptRecognitionQuality = (
        TranscriptRecognitionQuality.RELIABLE
    )

    @model_validator(mode="after")
    def validate_transcript_reference(self) -> TranscriptRequirementsEvidence:
        if self.reference.transcript_locator is None:
            raise ValueError("transcript evidence requires a time-range reference")
        return self


RequirementsEvidence = Annotated[
    DocumentRequirementsEvidence | TranscriptRequirementsEvidence,
    Field(discriminator="kind"),
]


class ExtractRequirementsInputV1(ValidatedDefinitionModel):
    """Frozen compatibility contract used by pre-transcript executions."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    schema_version: Literal[EXTRACT_REQUIREMENTS_INPUT_SCHEMA_V1] = (
        EXTRACT_REQUIREMENTS_INPUT_SCHEMA_V1
    )
    capability: Literal["extract_requirements"] = "extract_requirements"
    workspace_id: UUID
    environment_id: UUID
    environment: EnvironmentKind
    workflow_run_id: UUID
    work_item_id: UUID
    client_request: ClientRequestV1
    authorized_source_refs: tuple[AcceptedSourceRevisionRef, ...] = ()
    execution: RequirementsExecutionConfiguration = RequirementsExecutionConfiguration()

    @model_validator(mode="after")
    def validate_legacy_scope(self) -> "ExtractRequirementsInputV1":
        if any(value.int == 0 for value in (
            self.workspace_id, self.environment_id, self.workflow_run_id, self.work_item_id
        )):
            raise ValueError("requirements extraction identities must be non-zero")
        accepted = set(self.client_request.accepted_sources)
        if len(set(self.authorized_source_refs)) != len(self.authorized_source_refs):
            raise ValueError("authorized source references must be unique")
        if not set(self.authorized_source_refs).issubset(accepted):
            raise ValueError("authorized sources must belong to the client request")
        return self


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
    evidence: tuple[RequirementsEvidence, ...] = ()
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
        authorized_set = set(authorized)
        if len(set(authorized)) != len(authorized):
            raise ValueError("authorized source references must be unique")
        if not authorized_set.issubset(accepted):
            raise ValueError("authorized sources must belong to the client request")
        evidence_keys: list[tuple[str, UUID, UUID]] = []
        for item in self.evidence:
            source_key = (
                item.reference.source_ref.source_object_id,
                item.reference.source_ref.source_revision_id,
                item.reference.source_ref.provenance_id,
            )
            if source_key not in accepted:
                raise ValueError("requirements evidence must belong to the client request")
            if (
                item.availability is RequirementsEvidenceAvailability.CURRENT
                and source_key not in authorized_set
            ):
                raise ValueError("current requirements evidence must be authorized")
            artifact_id = (
                item.reference.parsed_artifact_ref.id
                if item.reference.parsed_artifact_ref is not None
                else item.reference.transcript_locator.transcript_artifact_id
            )
            evidence_keys.append((item.kind, artifact_id, item.reference.segment_id))
        if len(set(evidence_keys)) != len(evidence_keys):
            raise ValueError("requirements evidence items must be unique")
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


class RequirementsExtractionOutputV1(RequirementsExtractionOutput):
    """Frozen compatibility output for already persisted v1 artifacts."""

    schema_version: Literal[EXTRACT_REQUIREMENTS_OUTPUT_SCHEMA_V1] = (
        EXTRACT_REQUIREMENTS_OUTPUT_SCHEMA_V1
    )

    @model_validator(mode="after")
    def reject_new_requirement_contracts(self) -> "RequirementsExtractionOutputV1":
        if any(item.schema_version != "requirement:v1" for item in self.requirements):
            raise ValueError("v1 extraction output requires requirement:v1")
        return self


def upgrade_extract_requirements_input(
    value: ExtractRequirementsInputV1,
) -> ExtractRequirementsInput:
    """Upgrade a legacy input without fabricating newly required evidence."""

    payload = value.model_dump(mode="python")
    payload["schema_version"] = EXTRACT_REQUIREMENTS_INPUT_SCHEMA
    payload["evidence"] = ()
    return ExtractRequirementsInput.model_validate(payload)


# Descriptive aliases used by composition roots that name the boundary by its
# capability rather than its implementation detail.
ExtractRequirementsResult = RequirementsExtractionOutput
ExtractRequirementsOutcome = RequirementsExtractionOutcome


__all__ = [
    "EXTRACT_REQUIREMENTS_INPUT_SCHEMA",
    "EXTRACT_REQUIREMENTS_INPUT_SCHEMA_V1",
    "EXTRACT_REQUIREMENTS_OUTPUT_SCHEMA",
    "EXTRACT_REQUIREMENTS_OUTPUT_SCHEMA_V1",
    "DocumentRequirementsEvidence",
    "ExtractRequirementsInput",
    "ExtractRequirementsInputV1",
    "ExtractRequirementsOutcome",
    "ExtractRequirementsResult",
    "RequirementsClarification",
    "RequirementsEvidence",
    "RequirementsEvidenceAvailability",
    "RequirementsExecutionConfiguration",
    "RequirementsExtractionOutcome",
    "RequirementsExtractionOutput",
    "RequirementsExtractionOutputV1",
    "RequirementsExtractionReason",
    "RequirementsExtractionReasonCode",
    "TranscriptRequirementsEvidence",
    "TranscriptRecognitionQuality",
    "upgrade_extract_requirements_input",
]
