"""Immutable processing authority linked to canonical content acceptance."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from spine.domain.common import ValidatedDefinitionModel, VersionIdentifier


class ProcessingAdmissionMode(str, Enum):
    PROCESSOR_REQUIRED = "processor_required"
    OPAQUE_ACCEPTANCE = "opaque_acceptance"


class QualifiedProcessorReference(ValidatedDefinitionModel):
    """Exact offline qualification used to admit parser work."""

    capability_id: str = Field(min_length=1, max_length=128)
    capability_version: VersionIdentifier
    processor_profile_version: VersionIdentifier
    qualification_evidence_reference: UUID
    qualification_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_reference(self) -> "QualifiedProcessorReference":
        if self.qualification_evidence_reference.int == 0:
            raise ValueError("processor qualification evidence must be non-zero")
        return self


class ExactOpaqueApprovalReference(ValidatedDefinitionModel):
    """Approval for retaining recognized content without ordinary processing."""

    approval_id: UUID
    approval_version: VersionIdentifier
    approval_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_purpose: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    security_controls_reference: UUID
    retention_applicability_reference: UUID

    @model_validator(mode="after")
    def validate_reference(self) -> "ExactOpaqueApprovalReference":
        if any(
            value.int == 0
            for value in (
                self.approval_id,
                self.security_controls_reference,
                self.retention_applicability_reference,
            )
        ):
            raise ValueError("opaque approval references must be non-zero")
        return self


class SourceProcessingAdmission(ValidatedDefinitionModel):
    """One exact, immutable processing disposition for accepted source content."""

    schema_version: Literal["source-processing-admission:v1"] = (
        "source-processing-admission:v1"
    )
    processing_admission_id: UUID
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    selector_id: UUID

    provider_connection_id: UUID
    provider_connection_version_id: UUID
    provider_approval_id: UUID
    resource_scope_reference: UUID
    authorization_generation: int = Field(ge=1)
    configuration_generation: int = Field(ge=1)
    grant_binding_id: UUID
    grant_token_epoch: int = Field(ge=1)
    grant_security_fence: int = Field(ge=0)

    root_policy_id: UUID
    root_policy_version: VersionIdentifier
    root_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_policy_id: UUID
    content_policy_version: VersionIdentifier
    content_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    receipt_association_id: UUID
    verified_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verified_byte_length: int = Field(ge=0)
    verifier_evidence_id: UUID
    verifier_profile_version: VersionIdentifier
    verifier_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    lifecycle_eligibility_reference: UUID
    lifecycle_eligibility_generation: int = Field(ge=1)
    retention_applicability_reference: UUID
    retention_applicability_generation: int = Field(ge=1)
    mode: ProcessingAdmissionMode
    processor: QualifiedProcessorReference | None = None
    opaque_approval: ExactOpaqueApprovalReference | None = None
    idempotency_receipt_id: UUID
    audit_event_id: UUID
    created_at: datetime

    @model_validator(mode="after")
    def validate_admission(self) -> "SourceProcessingAdmission":
        identifiers = (
            self.processing_admission_id,
            self.workspace_id,
            self.environment_id,
            self.source_object_id,
            self.source_revision_id,
            self.selector_id,
            self.provider_connection_id,
            self.provider_connection_version_id,
            self.provider_approval_id,
            self.resource_scope_reference,
            self.grant_binding_id,
            self.root_policy_id,
            self.content_policy_id,
            self.receipt_association_id,
            self.verifier_evidence_id,
            self.lifecycle_eligibility_reference,
            self.retention_applicability_reference,
            self.idempotency_receipt_id,
            self.audit_event_id,
        )
        if any(value.int == 0 for value in identifiers):
            raise ValueError("processing admission identifiers must be non-zero")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        if self.mode is ProcessingAdmissionMode.PROCESSOR_REQUIRED:
            if self.processor is None or self.opaque_approval is not None:
                raise ValueError(
                    "processor-required admission requires only qualified processor evidence"
                )
        elif self.processor is not None or self.opaque_approval is None:
            raise ValueError("opaque admission requires only an exact opaque approval")
        elif (
            self.opaque_approval.retention_applicability_reference
            != self.retention_applicability_reference
        ):
            raise ValueError(
                "opaque approval must bind the same retention applicability"
            )
        return self

    @property
    def exact_acceptance_binding(self) -> tuple[UUID, UUID, UUID, UUID, UUID]:
        return (
            self.workspace_id,
            self.environment_id,
            self.source_object_id,
            self.source_revision_id,
            self.receipt_association_id,
        )


__all__ = [
    "ExactOpaqueApprovalReference",
    "ProcessingAdmissionMode",
    "QualifiedProcessorReference",
    "SourceProcessingAdmission",
]
