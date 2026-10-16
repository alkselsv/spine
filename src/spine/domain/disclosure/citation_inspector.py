"""Contracts for protected citation inspection.

The inspector is the only seam that may turn an immutable citation identity
into a source locator and excerpt.  The model deliberately carries the exact
evidence reference that was persisted with the answer; it never accepts a
replacement locator or cached excerpt as authority.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from spine.domain.common import DefinitionModel
from spine.domain.context_bundles import (
    ContextBundleEvidenceReference,
    ContextBundleLineageRef,
)


class CitationInspectionMode(str, Enum):
    CURRENT = "current"
    HISTORICAL = "historical"


class CitationInspectionStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class CitationInspectionReason(str, Enum):
    AVAILABLE = "available"
    CITATION_UNAVAILABLE = "citation_unavailable"


class CitationExcerpt(DefinitionModel):
    """Exact locator resolution returned by a source/parsed-artifact adapter."""

    schema_version: Literal["citation-excerpt:v1"] = "citation-excerpt:v1"
    source_object_id: UUID
    source_revision_id: UUID
    original_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision_number: int | None = Field(default=None, ge=1)
    locator_schema: str = Field(min_length=1, max_length=128)
    locator_reference: str = Field(min_length=1, max_length=512)
    locator_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    excerpt: str = Field(min_length=1, max_length=32_768)


class CitationInspectionEvidence(DefinitionModel):
    """Content released after all citation and current-authority checks pass."""

    schema_version: Literal["citation-inspection-evidence:v1"] = (
        "citation-inspection-evidence:v1"
    )
    citation_id: UUID
    answer_id: UUID
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    original_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision_number: int | None = Field(default=None, ge=1)
    locator_schema: str = Field(min_length=1, max_length=128)
    locator_reference: str = Field(min_length=1, max_length=512)
    locator_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_bundle: ContextBundleLineageRef
    evidence_reference: ContextBundleEvidenceReference
    excerpt: str = Field(min_length=1, max_length=32_768)
    historical: bool = False

    @model_validator(mode="after")
    def validate_against_citation(self) -> "CitationInspectionEvidence":
        evidence = self.evidence_reference
        if (
            evidence.workspace_id != self.workspace_id
            or evidence.environment_id != self.environment_id
            or evidence.source_object_id != self.source_object_id
            or evidence.source_revision_id != self.source_revision_id
            or evidence.original_sha256 != self.original_sha256
            or evidence.revision_digest != self.revision_digest
            or evidence.revision_number != self.revision_number
            or evidence.locator_contract_version != self.locator_schema
            or evidence.locator.value != self.locator_reference
            or evidence.locator_digest != self.locator_digest
        ):
            raise ValueError("citation inspection evidence does not match its lineage")
        if (
            self.context_bundle.workspace_id != self.workspace_id
            or self.context_bundle.environment_id != self.environment_id
            or evidence.status.value != "valid"
        ):
            raise ValueError("citation inspection evidence has a mixed scope")
        return self


class CitationInspectionResult(DefinitionModel):
    """Safe terminal result; unavailable results contain no source details."""

    schema_version: Literal["citation-inspection-result:v1"] = (
        "citation-inspection-result:v1"
    )
    status: CitationInspectionStatus
    reason: CitationInspectionReason
    evidence: CitationInspectionEvidence | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "CitationInspectionResult":
        if self.status is CitationInspectionStatus.AVAILABLE:
            if self.reason is not CitationInspectionReason.AVAILABLE or self.evidence is None:
                raise ValueError("available inspection requires evidence")
        elif self.reason is not CitationInspectionReason.CITATION_UNAVAILABLE or self.evidence is not None:
            raise ValueError("unavailable inspection must be safe and content-free")
        return self


__all__ = [
    "CitationExcerpt",
    "CitationInspectionEvidence",
    "CitationInspectionMode",
    "CitationInspectionReason",
    "CitationInspectionResult",
    "CitationInspectionStatus",
]
